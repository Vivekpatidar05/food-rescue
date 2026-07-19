"""
social_routes.py — the human layer around a rescue:

  Chat      per-batch coordination thread between the donor, the receiving
            NGO, and the volunteer. Nobody else can read or write it.
  Ratings   post-completion peer reviews (1-5 stars) that build a public
            reputation next to the trust score.
  Tracking  live courier position: the volunteer's browser pings GPS while
            in transit; the donor and NGO watch progress + ETA.

All three share the same access rule: you must be a party of the batch.
"""

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify, request
from pymongo.errors import DuplicateKeyError

import db
import zones
from auth import require_auth, require_role

social_bp = Blueprint("social", __name__, url_prefix="/social")

MAX_CHAT_LENGTH = 500
MAX_CHAT_MESSAGES = 200
MAX_COMMENT_LENGTH = 300
COURIER_SPEED_KMH = 22.0  # conservative urban two-wheeler average for ETA


def _parse_object_id(value):
    # ObjectId(None) GENERATES a fresh id instead of raising — an absent
    # "ratee_id"/"after" field must parse to None, never to a random id.
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def _batch_parties(batch):
    """Users allowed into a batch's chat/tracking: donor, receiving NGO, and
    whoever currently holds it (NGO or volunteer)."""
    return {
        party_id
        for party_id in (
            batch.get("donor_id"),
            batch.get("receiver_id"),
            batch.get("accepted_by"),
        )
        if party_id is not None
    }


def _load_party_batch(batch_id):
    """(batch, error_response) — batch the current user is a party of."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return None, (jsonify({"error": "Invalid batch id"}), 400)
    batch = db.food_batches.find_one({"_id": object_id})
    if batch is None:
        return None, (jsonify({"error": "Batch not found"}), 404)
    if g.current_user["_id"] not in _batch_parties(batch):
        return None, (jsonify({"error": "You are not part of this rescue"}), 403)
    return batch, None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@social_bp.get("/chat/<batch_id>")
@require_auth
def chat_history(batch_id):
    """Thread for a batch (parties only). ?after=<message id> returns only
    newer messages, which keeps the 5-second poll payload tiny."""
    batch, error = _load_party_batch(batch_id)
    if error:
        return error

    query = {"batch_id": batch["_id"]}
    after = _parse_object_id(request.args.get("after"))
    if after is not None:
        query["_id"] = {"$gt": after}

    cursor = db.chat_messages.find(query).sort("_id", 1).limit(MAX_CHAT_MESSAGES)
    return jsonify({"messages": [db.serialize(m) for m in cursor]})


@social_bp.post("/chat/<batch_id>")
@require_auth
def chat_send(batch_id):
    batch, error = _load_party_batch(batch_id)
    if error:
        return error

    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Message text is required"}), 400
    if len(text) > MAX_CHAT_LENGTH:
        return jsonify({"error": f"Message too long (max {MAX_CHAT_LENGTH} chars)"}), 400

    sender = g.current_user
    message = {
        "batch_id": batch["_id"],
        "sender_id": sender["_id"],
        "sender_name": sender["name"],
        "sender_role": sender.get("role", ""),
        "text": text,
        "created_at": db.utcnow(),
    }
    db.chat_messages.insert_one(message)

    # Ping the other parties' bells (deduped per batch by db.notify's upsert).
    for party_id in _batch_parties(batch) - {sender["_id"]}:
        db.notify(
            party_id,
            "chat_message",
            f"💬 {sender['name']}: {text[:80]}{'…' if len(text) > 80 else ''}",
            batch["_id"],
        )

    return jsonify({"message": db.serialize(message)}), 201


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

def _default_ratee(batch, rater_id):
    """Who a rater most plausibly wants to review: the volunteer (if another
    party ran the delivery), otherwise the counterpart donor/NGO."""
    volunteer_id = (
        batch.get("accepted_by") if batch.get("delivery_mode") == "volunteer" else None
    )
    if volunteer_id is not None and volunteer_id != rater_id:
        return volunteer_id
    if batch.get("donor_id") != rater_id:
        return batch.get("donor_id")
    return batch.get("receiver_id")


@social_bp.post("/rate/<batch_id>")
@require_auth
def rate(batch_id):
    """Leave a 1-5 star review for another party of a completed rescue.
    One review per (batch, rater, ratee) — enforced by a unique index."""
    batch, error = _load_party_batch(batch_id)
    if error:
        return error
    if batch.get("status") != "completed":
        return jsonify({"error": "You can only rate a completed rescue"}), 409

    data = request.get_json(silent=True) or {}
    try:
        stars = int(data.get("stars"))
    except (TypeError, ValueError):
        return jsonify({"error": "stars must be a number from 1 to 5"}), 400
    if not 1 <= stars <= 5:
        return jsonify({"error": "stars must be between 1 and 5"}), 400
    comment = str(data.get("comment") or "").strip()[:MAX_COMMENT_LENGTH]

    rater = g.current_user
    ratee_id = _parse_object_id(data.get("ratee_id")) or _default_ratee(batch, rater["_id"])
    if ratee_id is None or ratee_id == rater["_id"]:
        return jsonify({"error": "Nobody to rate on this batch"}), 400
    if ratee_id not in _batch_parties(batch):
        return jsonify({"error": "That user was not part of this rescue"}), 400
    ratee = db.users.find_one({"_id": ratee_id})
    if ratee is None:
        return jsonify({"error": "That user no longer exists"}), 404

    rating = {
        "batch_id": batch["_id"],
        "rater_id": rater["_id"],
        "ratee_id": ratee_id,
        "rater_name": rater["name"],
        "ratee_name": ratee["name"],
        "stars": stars,
        "comment": comment,
        "created_at": db.utcnow(),
    }
    try:
        db.ratings.insert_one(rating)
    except DuplicateKeyError:
        return jsonify({"error": "You already rated this person for this rescue"}), 409

    # Aggregate mirror on the user document — a leaderboard query must not
    # re-average the whole ratings collection on every hit.
    db.users.update_one(
        {"_id": ratee_id}, {"$inc": {"rating_sum": stars, "rating_count": 1}}
    )
    db.notify(
        ratee_id,
        "rating_received",
        f"⭐ {rater['name']} rated you {stars}/5"
        + (f': "{comment[:60]}"' if comment else "!"),
        batch["_id"],
    )
    return jsonify({"rating": db.serialize(rating)}), 201


@social_bp.get("/ratings/me")
@require_auth
def my_ratings():
    """My reputation: average stars, recent reviews received, and which
    batches I already reviewed (so the UI can hide the button)."""
    user_id = g.current_user["_id"]
    user = g.current_user
    count = int(user.get("rating_count") or 0)
    total = int(user.get("rating_sum") or 0)
    received = (
        db.ratings.find({"ratee_id": user_id}).sort("created_at", -1).limit(10)
    )
    given = db.ratings.find({"rater_id": user_id}, {"batch_id": 1})
    return jsonify(
        {
            "average": round(total / count, 2) if count else None,
            "count": count,
            "recent": [db.serialize(r) for r in received],
            "rated_batches": [str(r["batch_id"]) for r in given],
        }
    )


# ---------------------------------------------------------------------------
# Live delivery tracking
# ---------------------------------------------------------------------------

@social_bp.post("/track/<batch_id>")
@require_role("volunteer")
def track_ping(batch_id):
    """The courier's browser reports GPS while driving. Only the volunteer
    holding the in-transit batch may ping; positions are validated GeoJSON."""
    object_id = _parse_object_id(batch_id)
    if object_id is None:
        return jsonify({"error": "Invalid batch id"}), 400

    data = request.get_json(silent=True) or {}
    try:
        position = db.geo_point(data.get("longitude"), data.get("latitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "valid latitude and longitude are required"}), 400

    result = db.food_batches.update_one(
        {"_id": object_id, "accepted_by": g.current_user["_id"], "status": "in_transit"},
        {
            "$set": {"tracking.position": position, "tracking.updated_at": db.utcnow()},
            # Breadcrumb trail, capped so a chatty client can't grow the doc.
            "$push": {"tracking.points": {"$each": [position["coordinates"]], "$slice": -60}},
        },
    )
    if result.matched_count == 0:
        return jsonify({"error": "No active in-transit delivery for this batch"}), 409
    return jsonify({"ok": True})


@social_bp.get("/track/<batch_id>")
@require_auth
def track_status(batch_id):
    """Where the food is right now: courier position, % of the delivery leg
    covered, straight-line km remaining, and a conservative ETA."""
    batch, error = _load_party_batch(batch_id)
    if error:
        return error

    tracking = batch.get("tracking") or {}
    position = tracking.get("position")
    payload = {
        "status": batch.get("status"),
        "position": db.serialize(position),
        "updated_at": db.serialize(tracking.get("updated_at")),
        "points": tracking.get("points") or [],
        "progress_pct": None,
        "remaining_km": None,
        "eta_min": None,
    }
    if position is None:
        return jsonify({"tracking": payload})

    receiver = (
        db.users.find_one({"_id": batch["receiver_id"]})
        if batch.get("receiver_id")
        else None
    )
    dest = (receiver or {}).get("location")
    if dest:
        cur_lng, cur_lat = position["coordinates"]
        dest_lng, dest_lat = dest["coordinates"]
        pick_lng, pick_lat = batch["pickup_location"]["coordinates"]
        remaining = zones.haversine_km(cur_lng, cur_lat, dest_lng, dest_lat)
        leg = zones.haversine_km(pick_lng, pick_lat, dest_lng, dest_lat)
        payload["remaining_km"] = round(remaining, 2)
        payload["eta_min"] = int(round(remaining / COURIER_SPEED_KMH * 60)) if remaining else 0
        if leg > 0.05:
            payload["progress_pct"] = int(round(max(0.0, min(1.0, 1 - remaining / leg)) * 100))
    return jsonify({"tracking": payload})
