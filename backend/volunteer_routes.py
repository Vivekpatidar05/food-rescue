"""
volunteer_routes.py — volunteer-facing endpoints: discover routes that need a
driver, claim them atomically, complete or cancel deliveries.
"""

import secrets
import threading

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify, request
from pymongo import ReturnDocument

import db
import engine
import logistics
import media_routes
import zones
from auth import require_role

volunteer_bp = Blueprint("volunteer", __name__, url_prefix="/volunteer")

DELIVERY_REWARD = 10
CANCEL_PENALTY = -15

# Multi-stop trips: a courier already carrying one batch may add ONE more
# when it's genuinely "on the way" — pickup near their current pickup and
# drop-off near (or identical to) their current drop-off.
MAX_ACTIVE_DELIVERIES = 2
ADDON_PICKUP_KM = 3.0
ADDON_DROP_KM = 3.0


def _active_deliveries(volunteer_id):
    return list(
        db.food_batches.find({"accepted_by": volunteer_id, "status": "in_transit"})
        .sort("claimed_at", 1)
    )


def _km_between(point_a, point_b):
    try:
        lng1, lat1 = point_a["coordinates"]
        lng2, lat2 = point_b["coordinates"]
        return zones.haversine_km(lng1, lat1, lng2, lat2)
    except (KeyError, TypeError, ValueError):
        return None


def _receiver_location(batch):
    if batch.get("receiver_id") is None:
        return None
    receiver = db.users.find_one({"_id": batch["receiver_id"]}, {"location": 1})
    return (receiver or {}).get("location")


def _sync_active_pointer(volunteer_id):
    """After completing/cancelling a leg, point active_batch_id at the next
    still-in-transit leg of the trip (or clear it)."""
    remaining = db.food_batches.find_one(
        {"accepted_by": volunteer_id, "status": "in_transit"},
        sort=[("claimed_at", 1)],
    )
    db.users.update_one(
        {"_id": volunteer_id},
        {"$set": {"active_batch_id": remaining["_id"] if remaining else None}},
    )


def _addon_compatible(primary, candidate):
    """(ok, reason) — may `candidate` join a trip already carrying `primary`?"""
    pickup_km = _km_between(primary.get("pickup_location"), candidate.get("pickup_location"))
    if pickup_km is None or pickup_km > ADDON_PICKUP_KM:
        return False, (
            f"Add-on pickups must be within {ADDON_PICKUP_KM:g} km of your current "
            f"pickup ({'unknown' if pickup_km is None else f'{pickup_km:.1f} km'})"
        )
    if primary.get("receiver_id") and candidate.get("receiver_id") \
            and primary["receiver_id"] == candidate["receiver_id"]:
        return True, None  # same NGO — the ideal add-on
    drop_a, drop_b = _receiver_location(primary), _receiver_location(candidate)
    drop_km = _km_between(drop_a, drop_b) if drop_a and drop_b else None
    if drop_km is None or drop_km > ADDON_DROP_KM:
        return False, (
            f"Add-on drop-offs must be within {ADDON_DROP_KM:g} km of your current "
            "drop-off"
        )
    return True, None


def _parse_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _attach_people(batch):
    """Serialize a batch with donor and receiver (drop-off NGO) summaries."""
    serialized = db.public_batch(batch)
    donor = db.users.find_one({"_id": batch["donor_id"]})
    if donor is not None:
        serialized["donor"] = {"name": donor["name"], "trust_score": donor["trust_score"]}
    if batch.get("receiver_id") is not None:
        receiver = db.users.find_one({"_id": batch["receiver_id"]})
        if receiver is not None:
            serialized["receiver"] = {
                "name": receiver["name"],
                "address": receiver.get("address", ""),
            }
    return serialized


@volunteer_bp.get("/available-batches")
@require_role("volunteer")
def available_batches():
    """volunteer_needed batches sorted by proximity to the volunteer, with
    pickup distance, donor trust, and the receiving NGO's drop-off address."""
    volunteer = g.current_user
    if not volunteer.get("location"):
        return jsonify({"error": "Your profile has no location set"}), 400

    pipeline = [
        {
            "$geoNear": {
                "near": volunteer["location"],
                "distanceField": "distance_meters",
                # Surge mode (flood relief, festival overload) widens discovery.
                "maxDistance": db.surge_radius(engine.TIER2_RADIUS_M),
                "query": {"status": "volunteer_needed"},
                "key": "pickup_location",
                "spherical": True,
            }
        },
        {"$sort": {"distance_meters": 1}},
        {"$limit": 30},
        {
            "$lookup": {
                "from": "users",
                "localField": "donor_id",
                "foreignField": "_id",
                "as": "donor",
            }
        },
        {"$unwind": {"path": "$donor", "preserveNullAndEmptyArrays": True}},
        {
            "$lookup": {
                "from": "users",
                "localField": "receiver_id",
                "foreignField": "_id",
                "as": "receiver",
            }
        },
        {"$unwind": {"path": "$receiver", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "donor": {"name": "$donor.name", "trust_score": "$donor.trust_score"},
                "receiver": {"name": "$receiver.name", "address": "$receiver.address"},
            }
        },
    ]
    batches = [db.public_batch(batch) for batch in db.food_batches.aggregate(pipeline)]
    return jsonify({"batches": batches, "reward": DELIVERY_REWARD})


# ---------------------------------------------------------------------------
# Availability shifts — volunteers declare when they're usually free, and the
# matching engine boosts on-shift volunteers (an available driver 2 km away
# beats an off-shift one next door).
# ---------------------------------------------------------------------------

VALID_PERIODS = ("morning", "afternoon", "evening")
MAX_SLOTS = 21  # 7 days x 3 periods


@volunteer_bp.get("/availability")
@require_role("volunteer")
def get_availability():
    return jsonify(
        {
            "slots": g.current_user.get("availability") or [],
            "tz_offset_min": g.current_user.get("tz_offset_min", 0),
            "periods": list(VALID_PERIODS),
        }
    )


@volunteer_bp.post("/availability")
@require_role("volunteer")
def set_availability():
    data = request.get_json(silent=True) or {}
    slots = data.get("slots")
    if not isinstance(slots, list) or len(slots) > MAX_SLOTS:
        return jsonify({"error": f"slots must be a list of at most {MAX_SLOTS} entries"}), 400

    cleaned = []
    for slot in slots:
        parts = str(slot).split("-", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return jsonify({"error": f"invalid slot \"{slot}\" — use \"<0-6>-<morning|afternoon|evening>\""}), 400
        day, period = int(parts[0]), parts[1]
        if day > 6 or period not in VALID_PERIODS:
            return jsonify({"error": f"invalid slot \"{slot}\""}), 400
        cleaned.append(f"{day}-{period}")

    try:
        tz_offset_min = int(data.get("tz_offset_min", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "tz_offset_min must be an integer"}), 400
    if not -14 * 60 <= tz_offset_min <= 14 * 60:
        return jsonify({"error": "tz_offset_min out of range"}), 400

    db.users.update_one(
        {"_id": g.current_user["_id"]},
        {"$set": {"availability": sorted(set(cleaned)), "tz_offset_min": tz_offset_min}},
    )
    return jsonify({"slots": sorted(set(cleaned)), "tz_offset_min": tz_offset_min})


@volunteer_bp.get("/active-batch")
@require_role("volunteer")
def active_batch():
    """The courier's current deliveries. `batch` stays the primary (backward
    compatible); `batches` lists every in-transit leg of a multi-stop trip."""
    actives = _active_deliveries(g.current_user["_id"])
    if not actives:
        return jsonify({"batch": None, "batches": []})
    serialized = [_attach_people(b) for b in actives]
    return jsonify({"batch": serialized[0], "batches": serialized})


@volunteer_bp.get("/route-addons")
@require_role("volunteer")
def route_addons():
    """Multi-stop suggestions: volunteer_needed batches picked up near my
    current pickup and dropped near my current drop-off — one extra rescue
    for barely any extra distance."""
    actives = _active_deliveries(g.current_user["_id"])
    if not actives:
        return jsonify({"addons": [], "reason": "No active delivery"})
    if len(actives) >= MAX_ACTIVE_DELIVERIES:
        return jsonify({"addons": [], "reason": "Trip is full (2 batches max)"})

    primary = actives[0]
    pipeline = [
        {
            "$geoNear": {
                "near": primary["pickup_location"],
                "distanceField": "distance_meters",
                "maxDistance": int(ADDON_PICKUP_KM * 1000),
                "query": {"status": "volunteer_needed"},
                "key": "pickup_location",
                "spherical": True,
            }
        },
        {"$limit": 10},
    ]
    addons = []
    for candidate in db.food_batches.aggregate(pipeline):
        ok, _reason = _addon_compatible(primary, candidate)
        if not ok:
            continue
        row = _attach_people(candidate)
        row["pickup_detour_km"] = round(candidate.get("distance_meters", 0) / 1000.0, 2)
        row["same_receiver"] = candidate.get("receiver_id") == primary.get("receiver_id")
        addons.append(row)
    return jsonify({"addons": addons[:5], "primary_batch_id": str(primary["_id"])})


@volunteer_bp.post("/claim/<batch_id>")
@require_role("volunteer")
def claim(batch_id):
    """ATOMIC claim: the {"status": "volunteer_needed"} filter guarantees only
    one volunteer wins the route."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    volunteer = g.current_user
    # Multi-stop trips: one active delivery is the norm; a SECOND is allowed
    # only when it's genuinely on the same route (nearby pickup + drop-off).
    actives = _active_deliveries(volunteer["_id"])
    is_addon = False
    if len(actives) >= MAX_ACTIVE_DELIVERIES:
        return jsonify({"error": f"Trip is full — {MAX_ACTIVE_DELIVERIES} deliveries max"}), 409
    if actives:
        candidate = db.food_batches.find_one({"_id": object_id, "status": "volunteer_needed"})
        if candidate is None:
            return jsonify({"error": "Route already claimed"}), 409
        ok, reason = _addon_compatible(actives[0], candidate)
        if not ok:
            return jsonify({"error": reason}), 409
        is_addon = True

    result = db.food_batches.find_one_and_update(
        {"_id": object_id, "status": "volunteer_needed"},  # filter ensures atomicity
        {
            "$set": {
                "status": "in_transit",
                "accepted_by": volunteer["_id"],
                "delivery_mode": "volunteer",
                "claimed_at": db.utcnow(),
                "trip_addon": is_addon,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "Route already claimed"}), 409

    # active_batch_id tracks the PRIMARY leg; an add-on leaves it untouched.
    db.users.update_one(
        {"_id": volunteer["_id"], "active_batch_id": None},
        {"$set": {"active_batch_id": object_id}},
    )

    route_info = logistics.get_delivery_eta(str(object_id), str(volunteer["_id"]))
    result["route_info"] = route_info

    db.notify(
        result["donor_id"],
        "volunteer_assigned",
        f"{volunteer['name']} is on the way to pick up \"{result['food_description']}\".",
        object_id,
    )
    if result.get("receiver_id") is not None:
        db.notify(
            result["receiver_id"],
            "volunteer_assigned",
            f"{volunteer['name']} claimed the delivery of \"{result['food_description']}\".",
            object_id,
        )

    return jsonify({"batch": _attach_people(result)})


@volunteer_bp.post("/complete/<batch_id>")
@require_role("volunteer")
def complete(batch_id):
    """Mark the delivery done: requires a proof-of-delivery photo, awards
    +10 trust, and records a permanent Match document."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    # Validate the proof BEFORE touching batch state — an invalid photo must
    # leave the delivery in_transit so the volunteer can retry.
    data = request.get_json(silent=True) or {}
    proof_photo = data.get("proof_photo")
    if not proof_photo:
        return jsonify({"error": "A proof-of-delivery photo is required to complete"}), 400
    try:
        media_routes.validate_proof_photo(proof_photo)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    volunteer_id = g.current_user["_id"]

    # Handoff OTP: the donor reads the 6-digit code to the volunteer at the
    # pickup point. Without it, "completions" that never happened are trivial.
    active = db.food_batches.find_one(
        {"_id": object_id, "accepted_by": volunteer_id, "status": "in_transit"}
    )
    if active is None:
        return jsonify({"error": "No active delivery to complete"}), 409
    expected_code = active.get("pickup_code")
    if expected_code:  # legacy batches without a code stay completable
        supplied = str(data.get("pickup_code") or "").strip()
        if not supplied:
            return jsonify(
                {"error": "pickup_code is required — ask the donor for the 6-digit handoff code"}
            ), 400
        if not secrets.compare_digest(supplied, expected_code):
            return jsonify({"error": "Incorrect pickup code"}), 403

    result = db.food_batches.find_one_and_update(
        {"_id": object_id, "accepted_by": volunteer_id, "status": "in_transit"},
        {"$set": {"status": "completed", "completed_at": db.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "No active delivery to complete"}), 409

    media_routes.save_proof(result, volunteer_id, proof_photo)
    db.adjust_trust(volunteer_id, DELIVERY_REWARD, "Completed delivery", object_id)
    _sync_active_pointer(volunteer_id)
    if result.get("receiver_id") is not None:
        db.users.update_one(
            {"_id": result["receiver_id"], "active_batch_id": object_id},
            {"$set": {"active_batch_id": None}},
        )

    db.record_match(
        result,
        trust_score_changes=[
            {"user_id": volunteer_id, "delta": DELIVERY_REWARD, "reason": "Completed delivery"}
        ],
    )

    db.notify(
        result["donor_id"],
        "delivery_completed",
        f"\"{result['food_description']}\" was delivered. Thank you for donating!",
        object_id,
    )
    if result.get("receiver_id") is not None:
        db.notify(
            result["receiver_id"],
            "delivery_completed",
            f"\"{result['food_description']}\" has arrived.",
            object_id,
        )

    return jsonify({"batch": db.public_batch(result), "trust_awarded": DELIVERY_REWARD})


@volunteer_bp.post("/cancel/<batch_id>")
@require_role("volunteer")
def cancel(batch_id):
    """Volunteer backs out after claiming: -15 trust, route reopens."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    volunteer_id = g.current_user["_id"]
    # receiver_id is immutable while in_transit, so reading it first and
    # folding it into the single atomic update keeps this race-free.
    batch = db.food_batches.find_one(
        {"_id": object_id, "accepted_by": volunteer_id, "status": "in_transit"}
    )
    if batch is None:
        return jsonify({"error": "No active delivery to cancel"}), 409

    result = db.food_batches.find_one_and_update(
        {"_id": object_id, "accepted_by": volunteer_id, "status": "in_transit"},
        {
            "$set": {
                "status": "volunteer_needed",
                "delivery_mode": "volunteer",
                "accepted_by": batch.get("receiver_id"),
                "claimed_at": None,
                "route_info": None,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "No active delivery to cancel"}), 409

    db.adjust_trust(volunteer_id, CANCEL_PENALTY, "Cancelled after claiming a route", object_id)
    _sync_active_pointer(volunteer_id)

    threading.Thread(
        target=engine.run_matching, args=(str(object_id),), daemon=True
    ).start()

    return jsonify({"batch": db.public_batch(result), "trust_penalty": CANCEL_PENALTY})
