"""
engine.py — 3-tier geospatial matching engine.

Tier 1 (t = 0)       : notify the 3 nearest compatible NGOs within 10 km.
       (t = 30 min)  : if the batch is waiting on a volunteer and none has
                       claimed it, notify the 5 nearest volunteers within 10 km.
Tier 2 (t = 60 min)  : widen the search to 25 km and pull in secondary
                       partners (NGOs with partner_type "animal_shelter").
Expiry (t = 90 min)  : still unmatched -> mark the batch "expired", tell donor.

All timed escalation uses threading.Timer (a scheduled callback) — never
time.sleep — so no request thread is ever blocked. Every stage re-reads the
batch status before acting, so stale timers are harmless no-ops.
"""

import threading
from datetime import timedelta

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument

import db
import sms

TIER1_RADIUS_M = 10_000
TIER2_RADIUS_M = 25_000
NGO_NOTIFY_LIMIT = 3
VOLUNTEER_NOTIFY_LIMIT = 5

STAGE1_DELAY_S = 30 * 60   # volunteer escalation check
STAGE2_DELAY_S = 60 * 60   # tier-2 fallback (25 km + animal shelters)
STAGE3_DELAY_S = 90 * 60   # expiry

UNMATCHED_STATUSES = ("pending", "volunteer_needed")


# ---------------------------------------------------------------------------
# Geospatial queries
# ---------------------------------------------------------------------------

def _dietary_filter(batch):
    """Vegetarian-first matching: fully vegetarian batches are compatible with
    every NGO; anything else only matches NGOs that opted in to non-veg."""
    if "vegetarian" in batch.get("dietary_tags", []):
        return {}
    return {"accepts_non_veg": True}


# Candidate ranking: nearest is not always best. A reliable partner slightly
# farther away beats a flaky one next door. 20 trust points ≈ 1 km advantage.
TRUST_KM_WEIGHT = 0.05
# Availability-shift boost: a volunteer who told us they're free right now is
# worth 1.5 km of distance advantage over one who is probably at work.
ON_SHIFT_KM_BONUS = 1.5


def _current_period(tz_offset_min):
    """(local weekday 0-6, period name) for a user's declared timezone."""
    local = db.utcnow() + timedelta(minutes=int(tz_offset_min or 0))
    hour = local.hour
    if 6 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 18:
        period = "afternoon"
    elif 18 <= hour < 23:
        period = "evening"
    else:
        return local.weekday(), None  # dead of night — no shift bonus
    return local.weekday(), period


def _is_on_shift(candidate):
    availability = candidate.get("availability")
    if not availability:
        return False
    day, period = _current_period(candidate.get("tz_offset_min", 0))
    return period is not None and f"{day}-{period}" in availability


def _ranked_candidates(role_query, location, max_distance_meters):
    """Users matching role_query near location, ranked by a weighted score of
    distance, trust, and (for volunteers) declared availability:
    score = distance_km − (trust − 100) × 0.05 − (1.5 if on shift)."""
    # Synthetic accounts (seed_data.py) exist only to train the ML models —
    # they must never be notified or crowd out real partners. Unverified and
    # suspended accounts can't accept anyway, so never route food to them.
    query = {
        "is_synthetic": {"$ne": True},
        "verification_status": {"$nin": ["pending", "rejected"]},
        "suspended": {"$ne": True},
        **role_query,
    }
    pipeline = [
        {
            "$geoNear": {
                "near": location,
                "distanceField": "distance_meters",
                # Surge mode widens every search ring.
                "maxDistance": db.surge_radius(max_distance_meters),
                "query": query,
                "key": "location",
                "spherical": True,
            }
        },
        {"$limit": 50},
    ]
    candidates = list(db.users.aggregate(pipeline))
    for candidate in candidates:
        distance_km = candidate.get("distance_meters", 0) / 1000.0
        trust = candidate.get("trust_score", 100)
        score = distance_km - (trust - 100) * TRUST_KM_WEIGHT
        if _is_on_shift(candidate):
            score -= ON_SHIFT_KM_BONUS
            candidate["on_shift"] = True
        candidate["match_score"] = score
    candidates.sort(key=lambda c: c["match_score"])
    return candidates


def find_ngos_near(location, max_distance_meters, extra_filter=None):
    """Compatible NGOs, ranked by distance + trust (best first)."""
    query = {"role": "ngo"}
    if extra_filter:
        query.update(extra_filter)
    return _ranked_candidates(query, location, max_distance_meters)


def find_volunteers_near(location, max_distance_meters):
    """Idle volunteers, ranked by distance + trust (best first)."""
    return _ranked_candidates(
        {"role": "volunteer", "active_batch_id": None}, location, max_distance_meters
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _get_batch(batch_id):
    try:
        return db.food_batches.find_one({"_id": ObjectId(str(batch_id))})
    except InvalidId:
        return None


def _schedule(delay_seconds, callback, batch_id):
    timer = threading.Timer(delay_seconds, callback, args=(str(batch_id),))
    timer.daemon = True
    timer.start()


def _capability_filter(batch):
    """Extra candidate constraints implied by the batch itself: cold-chain
    food only ever routes to NGOs with cold storage."""
    if batch.get("cold_chain"):
        return {"has_cold_storage": True}
    return {}


def _notify_ngos(batch, ngos, limit):
    cold = "❄ (cold storage) " if batch.get("cold_chain") else ""
    message = (
        f"New rescue nearby: {cold}{batch['food_description']} "
        f"({batch['quantity_kg']} kg) at {batch['pickup_address']}"
    )
    for ngo in ngos[:limit]:
        db.notify(ngo["_id"], "batch_nearby", message, batch["_id"])
        sms.send_alert(ngo, message)


def _notify_volunteers(batch, radius_m):
    volunteers = find_volunteers_near(batch["pickup_location"], radius_m)
    cold = "❄ insulated bag needed — " if batch.get("cold_chain") else ""
    message = (
        f"Delivery route available: {cold}{batch['food_description']} "
        f"from {batch['pickup_address']}"
    )
    for volunteer in volunteers[:VOLUNTEER_NOTIFY_LIMIT]:
        db.notify(volunteer["_id"], "route_available", message, batch["_id"])
        sms.send_alert(volunteer, message)
    return volunteers


def _notify_zone_watchers(batch):
    """Watch zones: NGOs subscribed to the batch's city zone hear about it no
    matter how far away they are — a food bank can monitor the wholesale
    market across town. Dietary compatibility still applies."""
    zone = batch.get("zone")
    if not zone:
        return
    query = {
        "role": "ngo",
        "watch_zones": zone,
        "is_synthetic": {"$ne": True},
        "verification_status": {"$nin": ["pending", "rejected"]},
        "suspended": {"$ne": True},
        **_dietary_filter(batch),
        **_capability_filter(batch),
    }
    message = (
        f"👁 Watched zone \"{zone}\": {batch['food_description']} "
        f"({batch['quantity_kg']} kg) at {batch['pickup_address']}"
    )
    for watcher in db.users.find(query).limit(25):
        db.notify(watcher["_id"], "watch_zone", message, batch["_id"])


# ---------------------------------------------------------------------------
# Escalation stages
# ---------------------------------------------------------------------------

def _stage1_check(batch_id):
    """t+30 min — if the NGO asked for a volunteer and nobody claimed the
    route yet, push it to nearby volunteers (again)."""
    batch = _get_batch(batch_id)
    if batch is not None and batch["status"] == "volunteer_needed":
        _notify_volunteers(batch, TIER1_RADIUS_M)
        db.notify(
            batch["donor_id"],
            "self_delivery_offer",
            "No volunteer found nearby yet. Deliver it yourself and earn 5x trust points (+50)!",
            batch["_id"],
        )


def _stage2_check(batch_id):
    """t+60 min — Tier 2 fallback: widen to 25 km and include secondary
    partners (animal shelters)."""
    batch = _get_batch(batch_id)
    if batch is None or batch["status"] not in UNMATCHED_STATUSES:
        return

    db.food_batches.update_one({"_id": batch["_id"]}, {"$set": {"match_tier": 2}})

    if batch["status"] == "pending":
        wider_ngos = find_ngos_near(
            batch["pickup_location"], TIER2_RADIUS_M,
            {**_dietary_filter(batch), **_capability_filter(batch)},
        )
        shelters = find_ngos_near(
            batch["pickup_location"], TIER2_RADIUS_M,
            {"partner_type": "animal_shelter", **_capability_filter(batch)},
        )
        seen, targets = set(), []
        for candidate in wider_ngos + shelters:
            if candidate["_id"] not in seen:
                seen.add(candidate["_id"])
                targets.append(candidate)
        _notify_ngos(batch, targets, NGO_NOTIFY_LIMIT * 2)
    else:
        _notify_volunteers(batch, TIER2_RADIUS_M)


def _stage3_check(batch_id):
    """t+90 min — still unmatched: expire the batch and inform the donor.
    Atomic filter guarantees an in-flight acceptance always wins."""
    try:
        object_id = ObjectId(str(batch_id))
    except InvalidId:
        return
    expired = db.food_batches.find_one_and_update(
        {"_id": object_id, "status": {"$in": list(UNMATCHED_STATUSES)}},
        {"$set": {"status": "expired"}},
        return_document=ReturnDocument.AFTER,
    )
    if expired is not None:
        db.notify(
            expired["donor_id"],
            "batch_expired",
            f"No match was found in time — your batch \"{expired['food_description']}\" has expired.",
            expired["_id"],
        )
        _release_receiver_slot(expired)


def _release_receiver_slot(batch):
    """A volunteer_needed batch that dies still occupies the receiving NGO's
    active_batch_id slot — free it, or the NGO can never accept again."""
    receiver_id = batch.get("receiver_id")
    if receiver_id is None:
        return
    result = db.users.update_one(
        {"_id": receiver_id, "active_batch_id": batch["_id"]},
        {"$set": {"active_batch_id": None}},
    )
    if result.modified_count:
        db.notify(
            receiver_id,
            "batch_expired",
            f"\"{batch['food_description']}\" expired before delivery — "
            "you can accept new batches again.",
            batch["_id"],
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_matching(batch_id):
    """
    3-Tier geospatial matching engine.
    Tier 1: Find NGOs within 10km.
    Tier 2: If volunteer_needed, find Volunteers within 10km.
    Tier 3 (Fallback): Expand to 25km OR route to animal shelters.

    Called in a background thread whenever a batch enters "pending" or
    "volunteer_needed". Safe to call repeatedly.
    """
    batch = _get_batch(batch_id)
    if batch is None:
        return

    if batch["status"] == "pending":
        ngos = find_ngos_near(
            batch["pickup_location"], TIER1_RADIUS_M,
            {**_dietary_filter(batch), **_capability_filter(batch)},
        )
        _notify_ngos(batch, ngos, NGO_NOTIFY_LIMIT)
        _notify_zone_watchers(batch)
        db.food_batches.update_one({"_id": batch["_id"]}, {"$set": {"match_tier": 1}})
        _schedule(STAGE1_DELAY_S, _stage1_check, batch["_id"])
        _schedule(STAGE2_DELAY_S, _stage2_check, batch["_id"])
        _schedule(STAGE3_DELAY_S, _stage3_check, batch["_id"])

    elif batch["status"] == "volunteer_needed":
        _notify_volunteers(batch, TIER1_RADIUS_M)
        _schedule(STAGE1_DELAY_S, _stage1_check, batch["_id"])
        _schedule(STAGE2_DELAY_S, _stage2_check, batch["_id"])


def expire_stale_batches():
    """Safety-net sweep (run every 5 minutes by the background scheduler):
    expire any unmatched batch whose expires_at has passed, even if the
    in-memory escalation timers were lost to a process restart."""
    now = db.utcnow()
    stale = db.food_batches.find(
        {"status": {"$in": list(UNMATCHED_STATUSES)}, "expires_at": {"$lt": now}}
    )
    for batch in stale:
        _stage3_check(batch["_id"])


# ---------------------------------------------------------------------------
# Abandoned-delivery enforcement (runs in the same 5-minute scheduler)
# ---------------------------------------------------------------------------

DELIVERY_WINDOW_HOURS = 2      # volunteer must deliver within 2h of claiming
MISSED_DELIVERY_PENALTY = -20
SELF_DELIVERY_WINDOW_HOURS = 4 # donor self-delivery auto-closes after 4h


def enforce_delivery_windows():
    """Volunteers who claim a route and vanish: after 2 hours the route
    reopens (-20 trust) so the food is not lost to a ghost courier."""
    cutoff = db.utcnow() - timedelta(hours=DELIVERY_WINDOW_HOURS)
    overdue = db.food_batches.find({"status": "in_transit", "claimed_at": {"$lt": cutoff}})
    for batch in overdue:
        volunteer_id = batch.get("accepted_by")
        # Atomic re-check: a delivery completed mid-sweep is never punished.
        result = db.food_batches.find_one_and_update(
            {"_id": batch["_id"], "status": "in_transit", "claimed_at": {"$lt": cutoff}},
            {
                "$set": {
                    "status": "volunteer_needed",
                    "accepted_by": batch.get("receiver_id"),
                    "claimed_at": None,
                    "route_info": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            continue
        if volunteer_id is not None:
            db.adjust_trust(
                volunteer_id, MISSED_DELIVERY_PENALTY,
                "Abandoned delivery (>2 hours after claiming)", batch["_id"],
            )
            db.users.update_one(
                {"_id": volunteer_id, "active_batch_id": batch["_id"]},
                {"$set": {"active_batch_id": None}},
            )
            db.notify(
                volunteer_id,
                "delivery_missed",
                f"You didn't deliver \"{batch['food_description']}\" within 2 hours "
                "— the route was reopened (-20 trust).",
                batch["_id"],
            )
        db.notify(
            batch["donor_id"],
            "batch_rebroadcast",
            f"The volunteer went quiet — \"{batch['food_description']}\" needs a new driver.",
            batch["_id"],
        )
        threading.Thread(
            target=run_matching, args=(str(batch["_id"]),), daemon=True
        ).start()


def close_stale_self_deliveries():
    """Donor self-deliveries never confirmed: auto-close after 4 hours so the
    receiving NGO's active slot is not held hostage forever."""
    cutoff = db.utcnow() - timedelta(hours=SELF_DELIVERY_WINDOW_HOURS)
    stale = db.food_batches.find(
        {"status": "donor_delivering", "self_delivery_at": {"$lt": cutoff}}
    )
    for batch in stale:
        result = db.food_batches.find_one_and_update(
            {"_id": batch["_id"], "status": "donor_delivering",
             "self_delivery_at": {"$lt": cutoff}},
            {"$set": {"status": "expired"}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            continue
        db.notify(
            batch["donor_id"],
            "batch_expired",
            f"Your self-delivery of \"{batch['food_description']}\" was never "
            "confirmed and has been closed.",
            batch["_id"],
        )
        _release_receiver_slot(result)
