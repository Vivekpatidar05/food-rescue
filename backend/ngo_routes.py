"""
ngo_routes.py — NGO-facing endpoints: discover nearby pending batches, accept
them atomically, request volunteer delivery, confirm pickup, or cancel.

Also hosts the pickup-window enforcement job: NGOs that accept a batch but do
not confirm pickup within 2 hours lose 20 trust points and the batch returns
to the pending pool.
"""

import secrets
import threading
import time
from datetime import timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify, request
from pymongo import ReturnDocument

import db
import engine
import logistics
import zones
from auth import require_role

ngo_bp = Blueprint("ngo", __name__, url_prefix="/ngo")

PICKUP_WINDOW_HOURS = 2
ENFORCEMENT_INTERVAL_SECONDS = 5 * 60
MISSED_PICKUP_PENALTY = -20
CANCEL_PENALTY = -15

ACTIVE_NGO_STATUSES = ("ngo_pickup", "volunteer_needed", "in_transit", "donor_delivering")


def _parse_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _rerun_matching(batch_id):
    threading.Thread(
        target=engine.run_matching, args=(str(batch_id),), daemon=True
    ).start()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@ngo_bp.get("/available-batches")
@require_role("ngo")
def available_batches():
    """Pending batches sorted by proximity to the NGO, with real distances
    from a $geoNear aggregation and the donor's name/trust score attached."""
    ngo = g.current_user
    if not ngo.get("location"):
        return jsonify({"error": "Your profile has no location set"}), 400

    # Vegetarian-first compatibility: NGOs only see fully vegetarian batches
    # unless they explicitly opted in to non-veg food.
    dietary_query = {} if ngo.get("accepts_non_veg") else {"dietary_tags": "vegetarian"}
    # Cold chain: refrigeration-dependent batches are invisible to NGOs
    # without cold storage — they could never accept them anyway.
    if not ngo.get("has_cold_storage"):
        dietary_query["cold_chain"] = {"$ne": True}

    pipeline = [
        {
            "$geoNear": {
                "near": ngo["location"],
                "distanceField": "distance_meters",
                # Surge mode (flood relief, festival overload) widens discovery.
                "maxDistance": db.surge_radius(engine.TIER2_RADIUS_M),
                "query": {"status": "pending", **dietary_query},
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
            "$addFields": {
                "donor": {"name": "$donor.name", "trust_score": "$donor.trust_score"}
            }
        },
    ]
    batches = [db.public_batch(batch) for batch in db.food_batches.aggregate(pipeline)]
    return jsonify({"batches": batches})


@ngo_bp.get("/active-batch")
@require_role("ngo")
def active_batch():
    batch = db.food_batches.find_one(
        {"receiver_id": g.current_user["_id"], "status": {"$in": list(ACTIVE_NGO_STATUSES)}}
    )
    if batch is None:
        return jsonify({"batch": None})
    donor = db.users.find_one({"_id": batch["donor_id"]})
    serialized = db.public_batch(batch)
    if donor is not None:
        serialized["donor"] = {"name": donor["name"], "trust_score": donor["trust_score"]}
    return jsonify({"batch": serialized})


@ngo_bp.get("/watch-zones")
@require_role("ngo")
def get_watch_zones():
    """City zones this NGO subscribed to. A new batch in a watched zone
    always notifies the NGO — even outside the normal matching radius."""
    return jsonify(
        {
            "zones": [z["name"] for z in zones.get_zones()],
            "watching": g.current_user.get("watch_zones") or [],
        }
    )


@ngo_bp.post("/watch-zones")
@require_role("ngo")
def set_watch_zones():
    data = request.get_json(silent=True) or {}
    requested = data.get("zones")
    if not isinstance(requested, list):
        return jsonify({"error": "zones must be a list of zone names"}), 400
    valid = {z["name"] for z in zones.get_zones()}
    watching = [str(z) for z in requested if str(z) in valid]
    db.users.update_one(
        {"_id": g.current_user["_id"]}, {"$set": {"watch_zones": watching}}
    )
    return jsonify({"watching": watching})


@ngo_bp.post("/cold-storage")
@require_role("ngo")
def set_cold_storage():
    """Declare (or retract) cold-storage capability. Cold-chain batches only
    match and route to NGOs with this flag set."""
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    db.users.update_one(
        {"_id": g.current_user["_id"]}, {"$set": {"has_cold_storage": enabled}}
    )
    return jsonify({"has_cold_storage": enabled})


@ngo_bp.get("/trust-history")
@require_role("ngo")
def trust_history():
    cursor = (
        db.trust_log.find({"user_id": g.current_user["_id"]})
        .sort("created_at", -1)
        .limit(20)
    )
    return jsonify({"history": [db.serialize(entry) for entry in cursor]})


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------

@ngo_bp.post("/accept/<batch_id>")
@require_role("ngo")
def accept(batch_id):
    """ATOMIC claim: the {"status": "pending"} filter inside
    find_one_and_update guarantees only one NGO can win the batch."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    ngo = g.current_user
    if ngo.get("active_batch_id"):
        return jsonify({"error": "You already have an active batch"}), 409

    # Cold chain: refrigeration-dependent food only goes to NGOs that told us
    # they have cold storage — a well-meaning acceptance would still spoil it.
    pre_cold = db.food_batches.find_one({"_id": object_id}, {"cold_chain": 1})
    if pre_cold and pre_cold.get("cold_chain") and not ngo.get("has_cold_storage"):
        return jsonify(
            {"error": "❄ This batch needs cold storage. Enable \"We have cold storage\" "
                      "in your watch-zone panel if your facility is equipped."}
        ), 409

    # Food-safety hard stop: food past its 4-hour safe window must never be
    # served, no matter how well-meaning the acceptance is.
    pre = db.food_batches.find_one({"_id": object_id}, {"safe_until": 1})
    if pre and pre.get("safe_until") is not None:
        safe_until = pre["safe_until"]
        if safe_until.tzinfo is None:
            safe_until = safe_until.replace(tzinfo=db.timezone.utc)
        if safe_until < db.utcnow():
            return jsonify(
                {"error": "This food is past its safe consumption window and can no longer be accepted"}
            ), 409

    result = db.food_batches.find_one_and_update(
        {"_id": object_id, "status": "pending"},  # filter ensures atomicity
        {
            "$set": {
                "status": "ngo_pickup",
                "accepted_by": ngo["_id"],
                "receiver_id": ngo["_id"],
                "delivery_mode": "ngo_self",
                "accepted_at": db.utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "Batch already taken"}), 409

    db.users.update_one({"_id": ngo["_id"]}, {"$set": {"active_batch_id": object_id}})
    db.notify(
        result["donor_id"],
        "batch_accepted",
        f"{ngo['name']} accepted your batch \"{result['food_description']}\".",
        object_id,
    )

    # Compute the NGO -> pickup route in the background; the client polls it.
    threading.Thread(
        target=logistics.get_delivery_eta,
        args=(str(object_id), str(ngo["_id"])),
        daemon=True,
    ).start()

    return jsonify({"batch": db.public_batch(result)})


@ngo_bp.post("/request-volunteer/<batch_id>")
@require_role("ngo")
def request_volunteer(batch_id):
    """NGO keeps the batch but asks for volunteer delivery — atomically flips
    the batch to volunteer_needed and re-runs the matching engine."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    result = db.food_batches.find_one_and_update(
        {"_id": object_id, "receiver_id": g.current_user["_id"], "status": "ngo_pickup"},
        {"$set": {"status": "volunteer_needed", "delivery_mode": "volunteer"}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "Batch is not yours or not awaiting pickup"}), 409

    _rerun_matching(object_id)
    return jsonify({"batch": db.public_batch(result)})


@ngo_bp.post("/confirm-pickup/<batch_id>")
@require_role("ngo")
def confirm_pickup(batch_id):
    """NGO confirms it collected the food itself — completes the rescue.
    Requires the donor's 6-digit handoff code: proof the NGO was physically
    at the pickup point, not confirming from a couch across town."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    ngo_id = g.current_user["_id"]
    batch = db.food_batches.find_one(
        {
            "_id": object_id,
            "receiver_id": ngo_id,
            "status": "ngo_pickup",
            "delivery_mode": "ngo_self",
        }
    )
    if batch is None:
        return jsonify({"error": "No active self-pickup batch to confirm"}), 409

    expected_code = batch.get("pickup_code")
    if expected_code:  # legacy batches without a code stay confirmable
        supplied = str((request.get_json(silent=True) or {}).get("pickup_code") or "").strip()
        if not supplied:
            return jsonify(
                {"error": "pickup_code is required — ask the donor for the 6-digit handoff code"}
            ), 400
        if not secrets.compare_digest(supplied, expected_code):
            return jsonify({"error": "Incorrect pickup code"}), 403

    result = db.food_batches.find_one_and_update(
        {
            "_id": object_id,
            "receiver_id": ngo_id,
            "status": "ngo_pickup",
            "delivery_mode": "ngo_self",
        },
        {"$set": {"status": "completed", "completed_at": db.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "No active self-pickup batch to confirm"}), 409

    db.users.update_one({"_id": ngo_id}, {"$set": {"active_batch_id": None}})
    db.record_match(result)
    db.notify(
        result["donor_id"],
        "delivery_completed",
        f"\"{result['food_description']}\" was picked up successfully. Thank you!",
        object_id,
    )
    return jsonify({"batch": db.public_batch(result)})


@ngo_bp.post("/cancel/<batch_id>")
@require_role("ngo")
def cancel(batch_id):
    """NGO backs out after accepting: -15 trust, batch returns to the pool."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    ngo_id = g.current_user["_id"]
    result = db.food_batches.find_one_and_update(
        {
            "_id": object_id,
            "receiver_id": ngo_id,
            "status": {"$in": ["ngo_pickup", "volunteer_needed"]},
        },
        {
            "$set": {
                "status": "pending",
                "accepted_by": None,
                "receiver_id": None,
                "delivery_mode": None,
                "accepted_at": None,
                "route_info": None,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return jsonify({"error": "No cancellable batch found"}), 409

    db.adjust_trust(ngo_id, CANCEL_PENALTY, "Cancelled after accepting a batch", object_id)
    db.users.update_one({"_id": ngo_id}, {"$set": {"active_batch_id": None}})
    _rerun_matching(object_id)

    return jsonify({"batch": db.public_batch(result), "trust_penalty": CANCEL_PENALTY})


# ---------------------------------------------------------------------------
# Background enforcement job
# ---------------------------------------------------------------------------

def enforce_pickup_windows():
    """NGOs that accepted a batch but did not confirm pickup within 2 hours:
    -20 trust, batch resets to pending and re-enters matching."""
    cutoff = db.utcnow() - timedelta(hours=PICKUP_WINDOW_HOURS)
    overdue = db.food_batches.find(
        {"status": "ngo_pickup", "accepted_at": {"$lt": cutoff}}
    )
    for batch in overdue:
        # Atomic re-check so a pickup confirmed mid-sweep is never punished.
        result = db.food_batches.find_one_and_update(
            {"_id": batch["_id"], "status": "ngo_pickup", "accepted_at": {"$lt": cutoff}},
            {
                "$set": {
                    "status": "pending",
                    "accepted_by": None,
                    "receiver_id": None,
                    "delivery_mode": None,
                    "accepted_at": None,
                    "route_info": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            continue

        ngo_id = batch.get("receiver_id") or batch.get("accepted_by")
        if ngo_id is not None:
            db.adjust_trust(
                ngo_id, MISSED_PICKUP_PENALTY, "Missed pickup window (>2 hours)", batch["_id"]
            )
            db.users.update_one(
                {"_id": ngo_id, "active_batch_id": batch["_id"]},
                {"$set": {"active_batch_id": None}},
            )
            db.notify(
                ngo_id,
                "pickup_missed",
                f"You missed the 2-hour pickup window for \"{batch['food_description']}\" (-20 trust).",
                batch["_id"],
            )
        db.notify(
            batch["donor_id"],
            "batch_rebroadcast",
            f"The NGO missed its pickup window — \"{batch['food_description']}\" is being rematched.",
            batch["_id"],
        )
        _rerun_matching(batch["_id"])


def start_enforcement_scheduler():
    """Started once from app.py: runs pickup-window enforcement and the
    batch-expiry sweep every 5 minutes in a daemon thread."""

    def _loop():
        import donor_routes  # deferred: avoids a circular import at module load

        while True:
            try:
                enforce_pickup_windows()
                engine.enforce_delivery_windows()
                engine.close_stale_self_deliveries()
                engine.expire_stale_batches()
                donor_routes.process_due_recurring()
            except Exception as exc:  # never let the watchdog die
                print(f"[enforcement] sweep error: {exc}")
            time.sleep(ENFORCEMENT_INTERVAL_SECONDS)

    thread = threading.Thread(target=_loop, name="trust-enforcement", daemon=True)
    thread.start()
    return thread
