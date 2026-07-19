"""
donor_routes.py — donor-facing endpoints: broadcast surplus food, track own
batches, and opt into self-delivery when no volunteer is found.
"""

import secrets
import threading
from datetime import timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify, request
from pymongo import ReturnDocument

import db
import engine
import logistics
import nlp_engine
import zones
from auth import require_role

donor_bp = Blueprint("donor", __name__, url_prefix="/donor")

BATCH_TTL_HOURS = 3            # perishable food: strict 3-hour window
NON_PERISHABLE_TTL_HOURS = 12  # sealed/dry goods survive far longer
COLD_CHAIN_TTL_HOURS = 2       # refrigeration-dependent food moves fastest
SELF_DELIVERY_BONUS = 50  # 5x multiplier event

# Cold chain: dairy/cream-based and other refrigeration-dependent foods spoil
# fastest and must only go to NGOs with cold storage. Detected from the
# description (or set explicitly by the donor with cold_chain: true).
COLD_CHAIN_TERMS = {
    "paneer", "milk", "curd", "dahi", "yogurt", "yoghurt", "lassi", "cream",
    "malai", "khoya", "mawa", "cheese", "butter", "ghee residue", "custard",
    "kheer", "rasmalai", "rasgulla", "shrikhand", "ice cream", "icecream",
    "kulfi", "mousse", "pudding", "mayonnaise", "raita",
}


def _detect_cold_chain(food_description, parsed):
    """True when the description names refrigeration-dependent food."""
    text = f" {food_description.lower()} "
    if any(f" {term} " in text or f" {term}," in text or f" {term}." in text
           for term in COLD_CHAIN_TERMS):
        return True
    items = {str(item).lower() for item in (parsed.get("food_items") or [])}
    return bool(items & COLD_CHAIN_TERMS)

# Food safety (simplified FSSAI 2h/4h rule): cooked perishable food should be
# eaten within 4 hours of preparation. A batch whose safe window has under 30
# minutes left cannot realistically be collected AND served in time.
SAFE_WINDOW_HOURS = 4.0
MIN_REMAINING_SAFE_HOURS = 0.5
MAX_PREPARED_HOURS_AGO = 48.0

MAX_TEMPLATES_PER_DONOR = 12
MAX_RECURRING_PER_DONOR = 8


def _parse_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


class BatchError(ValueError):
    """A donor-facing validation problem while building a batch."""


def create_batch(donor, food_description, quantity_kg, address, pickup_location,
                 dietary_tags=None, prepared_hours_ago=None, source=None,
                 cold_chain=None):
    """Shared batch factory used by /broadcast, templates, donate-again, and
    the recurring-donation scheduler. Validates, runs the NLP layer, applies
    the food-safety window, inserts the batch and kicks off matching.

    Raises BatchError with a human-readable message on any validation failure.
    """
    food_description = (food_description or "").strip()
    address = (address or "").strip()
    if not food_description or not address:
        raise BatchError("food_description and address are required")

    # NLP categorization: a donor can type one raw sentence ("We have 15 kg
    # of cooked paneer left over") and the engine extracts the structure.
    parsed = nlp_engine.parse_food_description(food_description)

    if quantity_kg in (None, ""):
        quantity_kg = parsed["quantity_kg"]
        if quantity_kg is None:
            raise BatchError(
                "quantity_kg is required — send the field or mention "
                "it in the description (e.g. \"15 kg of cooked rice\")"
            )
    try:
        quantity_kg = float(quantity_kg)
    except (TypeError, ValueError):
        raise BatchError("quantity_kg must be a number")
    if quantity_kg <= 0:
        raise BatchError("quantity_kg must be greater than zero")

    if dietary_tags is not None and not isinstance(dietary_tags, list):
        raise BatchError("dietary_tags must be a list")
    if not dietary_tags:
        dietary_tags = list(parsed["dietary_tags"])  # NLP vegetarian-first default
    dietary_tags = [str(tag).strip().lower() for tag in dietary_tags if str(tag).strip()]
    if not dietary_tags:
        dietary_tags = list(parsed["dietary_tags"])

    # Dietary safety override: an explicit meat/egg term in the description
    # always beats a "vegetarian" claim — mislabeled food must never route
    # to a strictly vegetarian NGO.
    nlp_dietary_override = False
    if parsed["food_type"] != "Vegetarian" and "vegetarian" in dietary_tags:
        dietary_tags = list(parsed["dietary_tags"])
        nlp_dietary_override = True

    now = db.utcnow()
    # Perishability-aware TTL: cooked food must move in 3 hours, but sealed
    # dry ration is still perfectly good 12 hours later.
    ttl_hours = BATCH_TTL_HOURS if parsed["perishable"] else NON_PERISHABLE_TTL_HOURS

    # Cold chain: explicit donor flag wins; otherwise auto-detect dairy/cream
    # terms. Cold-chain food gets the tightest window of all and (in
    # ngo_routes/engine) only routes to NGOs with cold storage.
    if cold_chain is None:
        cold_chain = _detect_cold_chain(food_description, parsed)
    cold_chain = bool(cold_chain)
    if cold_chain:
        ttl_hours = min(ttl_hours, COLD_CHAIN_TTL_HOURS)

    expires_at = now + timedelta(hours=ttl_hours)

    # Food-safety window: when the donor tells us how old the food is, the
    # 4-hour rule becomes a hard stop that overrides the broadcast TTL.
    safe_until = None
    if prepared_hours_ago not in (None, ""):
        try:
            prepared_hours_ago = float(prepared_hours_ago)
        except (TypeError, ValueError):
            raise BatchError("prepared_hours_ago must be a number of hours")
        if not 0 <= prepared_hours_ago <= MAX_PREPARED_HOURS_AGO:
            raise BatchError(f"prepared_hours_ago must be 0-{MAX_PREPARED_HOURS_AGO:.0f}")
        if parsed["perishable"]:
            remaining_hours = SAFE_WINDOW_HOURS - prepared_hours_ago
            if remaining_hours < MIN_REMAINING_SAFE_HOURS:
                raise BatchError(
                    "This food was prepared too long ago to redistribute safely "
                    f"(4-hour rule). Please compost it instead — nobody should "
                    "risk eating it."
                )
            safe_until = now + timedelta(hours=remaining_hours)
            expires_at = min(expires_at, safe_until)
    else:
        prepared_hours_ago = None

    batch = {
        "donor_id": donor["_id"],
        "food_description": food_description,
        "quantity_kg": quantity_kg,
        "dietary_tags": dietary_tags,
        "pickup_location": pickup_location,
        "pickup_address": address,
        "status": "pending",
        "accepted_by": None,
        "receiver_id": None,
        "delivery_mode": None,
        "expires_at": expires_at,
        "ttl_hours": ttl_hours,
        # Handoff OTP: only the donor ever sees this code. Whoever collects
        # the food must read it off the donor in person — a completion
        # without the code is a completion that never physically happened.
        "pickup_code": f"{secrets.randbelow(1_000_000):06d}",
        "created_at": now,
        "accepted_at": None,
        "claimed_at": None,
        "completed_at": None,
        "match_tier": None,
        "route_info": None,
        "zone": zones.assign_zone(*pickup_location["coordinates"]),
        "perishable": parsed["perishable"],
        "cold_chain": cold_chain,
        "prepared_hours_ago": prepared_hours_ago,
        "safe_until": safe_until,
        "source": source,  # None | "template" | "rebroadcast" | "recurring"
        "nlp": {
            "food_type": parsed["food_type"],
            "food_items": parsed["food_items"],
            "quantity_source": parsed["quantity_source"],
            "confidence": parsed["confidence"],
            "engine": parsed["engine"],
            "dietary_override": nlp_dietary_override,
        },
    }
    result = db.food_batches.insert_one(batch)
    batch["_id"] = result.inserted_id

    threading.Thread(
        target=engine.run_matching, args=(str(result.inserted_id),), daemon=True
    ).start()
    return batch


@donor_bp.post("/broadcast")
@require_role("donor")
def broadcast():
    """Create a food batch and immediately kick off the matching engine in a
    background thread."""
    data = request.get_json(silent=True) or {}

    try:
        pickup_location = db.geo_point(data.get("longitude"), data.get("latitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "valid latitude and longitude are required"}), 400

    try:
        batch = create_batch(
            g.current_user,
            data.get("food_description"),
            data.get("quantity_kg"),
            data.get("address"),
            pickup_location,
            dietary_tags=data.get("dietary_tags"),
            prepared_hours_ago=data.get("prepared_hours_ago"),
            cold_chain=data.get("cold_chain"),
        )
    except BatchError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"batch": db.serialize(batch)}), 201


@donor_bp.get("/my-batches")
@require_role("donor")
def my_batches():
    cursor = (
        db.food_batches.find({"donor_id": g.current_user["_id"]})
        .sort("created_at", -1)
        .limit(50)
    )
    return jsonify({"batches": [db.serialize(batch) for batch in cursor]})


@donor_bp.post("/cancel/<batch_id>")
@require_role("donor")
def cancel_broadcast(batch_id):
    """Retract my own broadcast while it is still pending (nobody accepted).
    Once an NGO accepts, the batch is theirs to complete or cancel — a donor
    pulling food out from under an NGO already driving over is not allowed.
    No trust penalty: cancelling a typo'd or no-longer-available broadcast
    is honest housekeeping, not a broken commitment."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    batch = db.food_batches.find_one_and_update(
        {"_id": object_id, "donor_id": g.current_user["_id"], "status": "pending"},
        {"$set": {"status": "cancelled", "cancelled_at": db.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if batch is None:
        return jsonify(
            {"error": "Only your own still-pending broadcasts can be cancelled"}
        ), 409
    return jsonify({"batch": db.serialize(batch)})


@donor_bp.post("/self-deliver/<batch_id>")
@require_role("donor")
def self_deliver(batch_id):
    """Donor opts to deliver the batch themselves after no volunteer was
    found. Only valid on their own batch while it is in volunteer_needed."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    donor_id = g.current_user["_id"]
    batch = db.food_batches.find_one_and_update(
        {"_id": object_id, "donor_id": donor_id, "status": "volunteer_needed"},
        {"$set": {
            "status": "donor_delivering",
            "delivery_mode": "donor_self",
            "self_delivery_at": db.utcnow(),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if batch is None:
        return jsonify({"error": "Batch is not eligible for self-delivery"}), 409

    db.adjust_trust(
        donor_id,
        SELF_DELIVERY_BONUS,
        "Self-delivered after no volunteer found (5x bonus)",
        object_id,
    )

    route_info = logistics.get_delivery_eta(str(object_id), str(donor_id))
    batch["route_info"] = route_info

    if batch.get("receiver_id") is not None:
        db.notify(
            batch["receiver_id"],
            "donor_delivering",
            f"The donor is delivering \"{batch['food_description']}\" to you directly.",
            object_id,
        )

    return jsonify({"batch": db.serialize(batch), "trust_awarded": SELF_DELIVERY_BONUS})


@donor_bp.post("/complete/<batch_id>")
@require_role("donor")
def complete_self_delivery(batch_id):
    """Donor confirms the self-delivery arrived; closes the loop and records
    the permanent Match document."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    donor_id = g.current_user["_id"]
    batch = db.food_batches.find_one_and_update(
        {"_id": object_id, "donor_id": donor_id, "status": "donor_delivering"},
        {"$set": {"status": "completed", "completed_at": db.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if batch is None:
        return jsonify({"error": "Batch is not an active self-delivery"}), 409

    db.record_match(
        batch,
        trust_score_changes=[
            {
                "user_id": donor_id,
                "delta": SELF_DELIVERY_BONUS,
                "reason": "Self-delivered after no volunteer found (5x bonus)",
            }
        ],
    )

    if batch.get("receiver_id") is not None:
        db.users.update_one(
            {"_id": batch["receiver_id"], "active_batch_id": object_id},
            {"$set": {"active_batch_id": None}},
        )
        db.notify(
            batch["receiver_id"],
            "delivery_completed",
            f"\"{batch['food_description']}\" was delivered by the donor.",
            object_id,
        )

    return jsonify({"batch": db.serialize(batch)})


# ---------------------------------------------------------------------------
# Batch templates + one-click "donate again"
# ---------------------------------------------------------------------------

@donor_bp.get("/templates")
@require_role("donor")
def list_templates():
    cursor = (
        db.batch_templates.find({"donor_id": g.current_user["_id"]})
        .sort("times_used", -1)
        .limit(MAX_TEMPLATES_PER_DONOR)
    )
    return jsonify({"templates": [db.serialize(t) for t in cursor]})


@donor_bp.post("/templates")
@require_role("donor")
def save_template():
    """Save a broadcast preset (e.g. \"Friday buffet leftovers\") for reuse."""
    data = request.get_json(silent=True) or {}
    donor_id = g.current_user["_id"]

    if db.batch_templates.count_documents({"donor_id": donor_id}) >= MAX_TEMPLATES_PER_DONOR:
        return jsonify({"error": f"Template limit reached ({MAX_TEMPLATES_PER_DONOR}) — delete one first"}), 409

    food_description = str(data.get("food_description") or "").strip()
    address = str(data.get("address") or "").strip()
    if not food_description or not address:
        return jsonify({"error": "food_description and address are required"}), 400
    try:
        quantity_kg = float(data.get("quantity_kg"))
        if quantity_kg <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "quantity_kg must be a positive number"}), 400
    try:
        location = db.geo_point(data.get("longitude"), data.get("latitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "valid latitude and longitude are required"}), 400

    dietary_tags = data.get("dietary_tags") or ["vegetarian"]
    if not isinstance(dietary_tags, list):
        return jsonify({"error": "dietary_tags must be a list"}), 400

    template = {
        "donor_id": donor_id,
        "name": str(data.get("name") or food_description[:40]).strip()[:60],
        "food_description": food_description,
        "quantity_kg": quantity_kg,
        "dietary_tags": [str(t).strip().lower() for t in dietary_tags if str(t).strip()],
        "address": address,
        "location": location,
        "times_used": 0,
        "created_at": db.utcnow(),
    }
    result = db.batch_templates.insert_one(template)
    template["_id"] = result.inserted_id
    return jsonify({"template": db.serialize(template)}), 201


@donor_bp.delete("/templates/<template_id>")
@require_role("donor")
def delete_template(template_id):
    object_id = _parse_object_id(template_id)
    if object_id is None:
        return jsonify({"error": "Invalid template id"}), 400
    result = db.batch_templates.delete_one(
        {"_id": object_id, "donor_id": g.current_user["_id"]}
    )
    if result.deleted_count == 0:
        return jsonify({"error": "Template not found"}), 404
    return jsonify({"deleted": True})


@donor_bp.post("/templates/<template_id>/donate")
@require_role("donor")
def donate_from_template(template_id):
    """One-click broadcast straight from a saved template."""
    object_id = _parse_object_id(template_id)
    if object_id is None:
        return jsonify({"error": "Invalid template id"}), 400
    template = db.batch_templates.find_one(
        {"_id": object_id, "donor_id": g.current_user["_id"]}
    )
    if template is None:
        return jsonify({"error": "Template not found"}), 404

    try:
        batch = create_batch(
            g.current_user,
            template["food_description"],
            template["quantity_kg"],
            template["address"],
            template["location"],
            dietary_tags=list(template.get("dietary_tags") or []),
            source="template",
        )
    except BatchError as exc:
        return jsonify({"error": str(exc)}), 400

    db.batch_templates.update_one({"_id": object_id}, {"$inc": {"times_used": 1}})
    return jsonify({"batch": db.serialize(batch)}), 201


@donor_bp.post("/rebroadcast/<batch_id>")
@require_role("donor")
def rebroadcast(batch_id):
    """\"Donate again\": clone one of my past batches as a fresh broadcast."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400
    old = db.food_batches.find_one({"_id": object_id, "donor_id": g.current_user["_id"]})
    if old is None:
        return jsonify({"error": "Batch not found"}), 404

    try:
        batch = create_batch(
            g.current_user,
            old["food_description"],
            old["quantity_kg"],
            old["pickup_address"],
            old["pickup_location"],
            dietary_tags=list(old.get("dietary_tags") or []),
            source="rebroadcast",
        )
    except BatchError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"batch": db.serialize(batch)}), 201


# ---------------------------------------------------------------------------
# Recurring donations — "every Friday 21:30, 20 kg of buffet leftovers"
# ---------------------------------------------------------------------------

VALID_TZ_OFFSET_RANGE = (-14 * 60, 14 * 60)


def _next_run_at(days, time_str, tz_offset_min, after_utc):
    """First UTC datetime after `after_utc` that lands on one of `days`
    (local weekday numbers, 0=Monday) at `time_str` (local \"HH:MM\")."""
    hour, minute = (int(part) for part in time_str.split(":"))
    local_after = after_utc + timedelta(minutes=tz_offset_min)
    for day_offset in range(0, 8):
        candidate_day = local_after + timedelta(days=day_offset)
        if candidate_day.weekday() not in days:
            continue
        candidate_local = candidate_day.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate_local <= local_after:
            continue
        return candidate_local - timedelta(minutes=tz_offset_min)
    # Unreachable with a non-empty days list, but never return None.
    return after_utc + timedelta(days=7)


def _validate_schedule_payload(data):
    """Returns (fields, error). Shared by create."""
    food_description = str(data.get("food_description") or "").strip()
    address = str(data.get("address") or "").strip()
    if not food_description or not address:
        return None, "food_description and address are required"
    try:
        quantity_kg = float(data.get("quantity_kg"))
        if quantity_kg <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return None, "quantity_kg must be a positive number"
    try:
        location = db.geo_point(data.get("longitude"), data.get("latitude"))
    except (TypeError, ValueError):
        return None, "valid latitude and longitude are required"

    days = data.get("days")
    if not isinstance(days, list) or not days:
        return None, "days is required — a list of weekday numbers (0=Monday)"
    try:
        days = sorted({int(d) for d in days})
    except (TypeError, ValueError):
        return None, "days must be weekday numbers 0-6"
    if any(d < 0 or d > 6 for d in days):
        return None, "days must be weekday numbers 0-6"

    time_str = str(data.get("time") or "").strip()
    try:
        hour, minute = (int(part) for part in time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (TypeError, ValueError):
        return None, "time must be \"HH:MM\" (24-hour)"

    try:
        tz_offset_min = int(data.get("tz_offset_min", 0))
    except (TypeError, ValueError):
        return None, "tz_offset_min must be an integer (minutes east of UTC)"
    if not VALID_TZ_OFFSET_RANGE[0] <= tz_offset_min <= VALID_TZ_OFFSET_RANGE[1]:
        return None, "tz_offset_min out of range"

    dietary_tags = data.get("dietary_tags") or ["vegetarian"]
    if not isinstance(dietary_tags, list):
        return None, "dietary_tags must be a list"

    return {
        "food_description": food_description,
        "quantity_kg": quantity_kg,
        "dietary_tags": [str(t).strip().lower() for t in dietary_tags if str(t).strip()],
        "address": address,
        "location": location,
        "days": days,
        "time": f"{hour:02d}:{minute:02d}",
        "tz_offset_min": tz_offset_min,
    }, None


@donor_bp.get("/recurring")
@require_role("donor")
def list_recurring():
    cursor = (
        db.recurring_donations.find({"donor_id": g.current_user["_id"]})
        .sort("created_at", -1)
        .limit(MAX_RECURRING_PER_DONOR)
    )
    return jsonify({"schedules": [db.serialize(s) for s in cursor]})


@donor_bp.post("/recurring")
@require_role("donor")
def create_recurring():
    donor_id = g.current_user["_id"]
    if db.recurring_donations.count_documents({"donor_id": donor_id}) >= MAX_RECURRING_PER_DONOR:
        return jsonify({"error": f"Schedule limit reached ({MAX_RECURRING_PER_DONOR}) — delete one first"}), 409

    fields, error = _validate_schedule_payload(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400

    now = db.utcnow()
    schedule = {
        "donor_id": donor_id,
        **fields,
        "active": True,
        "next_run_at": _next_run_at(fields["days"], fields["time"], fields["tz_offset_min"], now),
        "last_run_at": None,
        "runs": 0,
        "created_at": now,
    }
    result = db.recurring_donations.insert_one(schedule)
    schedule["_id"] = result.inserted_id
    return jsonify({"schedule": db.serialize(schedule)}), 201


@donor_bp.post("/recurring/<schedule_id>/toggle")
@require_role("donor")
def toggle_recurring(schedule_id):
    object_id = _parse_object_id(schedule_id)
    if object_id is None:
        return jsonify({"error": "Invalid schedule id"}), 400
    schedule = db.recurring_donations.find_one(
        {"_id": object_id, "donor_id": g.current_user["_id"]}
    )
    if schedule is None:
        return jsonify({"error": "Schedule not found"}), 404

    active = not schedule.get("active", False)
    updates = {"active": active}
    if active:  # re-arm from now so a long-paused schedule doesn't back-fire
        updates["next_run_at"] = _next_run_at(
            schedule["days"], schedule["time"], schedule.get("tz_offset_min", 0), db.utcnow()
        )
    db.recurring_donations.update_one({"_id": object_id}, {"$set": updates})
    schedule.update(updates)
    return jsonify({"schedule": db.serialize(schedule)})


@donor_bp.delete("/recurring/<schedule_id>")
@require_role("donor")
def delete_recurring(schedule_id):
    object_id = _parse_object_id(schedule_id)
    if object_id is None:
        return jsonify({"error": "Invalid schedule id"}), 400
    result = db.recurring_donations.delete_one(
        {"_id": object_id, "donor_id": g.current_user["_id"]}
    )
    if result.deleted_count == 0:
        return jsonify({"error": "Schedule not found"}), 404
    return jsonify({"deleted": True})


@donor_bp.post("/recurring/<schedule_id>/run-now")
@require_role("donor")
def run_recurring_now(schedule_id):
    """Fire a schedule immediately (also what the e2e suite exercises)."""
    object_id = _parse_object_id(schedule_id)
    if object_id is None:
        return jsonify({"error": "Invalid schedule id"}), 400
    schedule = db.recurring_donations.find_one(
        {"_id": object_id, "donor_id": g.current_user["_id"]}
    )
    if schedule is None:
        return jsonify({"error": "Schedule not found"}), 404

    batch, error = _run_schedule(schedule, g.current_user)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"batch": db.serialize(batch)}), 201


def _run_schedule(schedule, donor):
    """Broadcast one occurrence of a recurring schedule. Returns (batch, err)."""
    try:
        batch = create_batch(
            donor,
            schedule["food_description"],
            schedule["quantity_kg"],
            schedule["address"],
            schedule["location"],
            dietary_tags=list(schedule.get("dietary_tags") or []),
            source="recurring",
        )
    except BatchError as exc:
        return None, str(exc)

    db.recurring_donations.update_one(
        {"_id": schedule["_id"]},
        {
            "$set": {
                "last_run_at": db.utcnow(),
                "next_run_at": _next_run_at(
                    schedule["days"], schedule["time"],
                    schedule.get("tz_offset_min", 0), db.utcnow(),
                ),
            },
            "$inc": {"runs": 1},
        },
    )
    db.notify(
        donor["_id"],
        "recurring_broadcast",
        f"🔁 Recurring donation went live: \"{schedule['food_description']}\" "
        f"({schedule['quantity_kg']} kg) — matching NGOs now.",
        batch["_id"],
    )
    return batch, None


def process_due_recurring():
    """Called by the 5-minute background sweep: broadcast every active
    schedule whose next_run_at has passed."""
    now = db.utcnow()
    due = db.recurring_donations.find({"active": True, "next_run_at": {"$lte": now}})
    for schedule in due:
        donor = db.users.find_one({"_id": schedule["donor_id"]})
        if donor is None or donor.get("suspended"):
            # Dead/suspended owner: park the schedule instead of retrying forever.
            db.recurring_donations.update_one(
                {"_id": schedule["_id"]}, {"$set": {"active": False}}
            )
            continue
        _, error = _run_schedule(schedule, donor)
        if error:
            print(f"[recurring] schedule {schedule['_id']} failed: {error}")
            db.recurring_donations.update_one(
                {"_id": schedule["_id"]},
                {"$set": {"next_run_at": _next_run_at(
                    schedule["days"], schedule["time"],
                    schedule.get("tz_offset_min", 0), now,
                )}},
            )
