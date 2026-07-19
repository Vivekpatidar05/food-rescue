"""
db.py — MongoDB connection, indexes, and shared data helpers for FoodRescue.

GEOJSON STRICTNESS
------------------
Every geographic coordinate stored anywhere in this database MUST be a strict
GeoJSON Point — LONGITUDE FIRST:

    {"type": "Point", "coordinates": [longitude, latitude]}

Any deviation from this format silently breaks the 2dsphere indexes that the
matching engine depends on. Always build points via geo_point().

COLLECTION SCHEMAS (enforced by application code)
-------------------------------------------------

users
{
  _id:             ObjectId,
  name:            str,
  email:           str (unique),
  password_hash:   str,
  role:            "donor" | "ngo" | "volunteer",
  location:        {type: "Point", coordinates: [lng, lat]},
  address:         str,
  trust_score:     int (default 100),
  created_at:      datetime (UTC),
  active_batch_id: ObjectId | None,          # prevents double-booking
  partner_type:    "food_charity" | "animal_shelter" (optional, NGOs only),
  accepts_non_veg: bool (optional, NGOs only — dietary matching filter)
}

food_batches
{
  _id:              ObjectId,
  donor_id:         ObjectId,
  food_description: str,
  quantity_kg:      float,
  dietary_tags:     ["vegetarian", ...],      # vegetarian-first default
  pickup_location:  {type: "Point", coordinates: [lng, lat]},
  pickup_address:   str,
  status:           "pending" | "ngo_pickup" | "volunteer_needed" |
                    "in_transit" | "donor_delivering" | "completed" | "expired" |
                    "cancelled",             # donor retracted while still pending
  accepted_by:      ObjectId | None,   # NGO or volunteer currently responsible
  receiver_id:      ObjectId | None,   # NGO that will receive the food
  delivery_mode:    "ngo_self" | "volunteer" | "donor_self" | None,
  expires_at:       datetime,          # auto-expire: 3h perishable / 12h dry
  ttl_hours:        3 | 12,            # perishability-aware time-to-live
  pickup_code:      str,               # 6-digit handoff OTP — DONOR-ONLY field,
                                       # stripped from NGO/volunteer responses
  self_delivery_at: datetime | None,   # anchor for stale self-delivery sweep
  created_at:       datetime,
  accepted_at:      datetime | None,   # anchor for 2-hour pickup enforcement
  claimed_at:       datetime | None,   # when a volunteer claimed the route
  completed_at:     datetime | None,
  match_tier:       1 | 2 | None,
  route_info:       {distance_km: float, duration_min: int, status: str} | None
}

matches
{
  _id:          ObjectId,
  batch_id:     ObjectId,
  donor_id:     ObjectId,
  receiver_id:  ObjectId | None,
  volunteer_id: ObjectId | None,
  matched_at:   datetime,
  completed_at: datetime | None,
  trust_score_changes: [{user_id: ObjectId, delta: int, reason: str}]
}

notifications
{
  _id:        ObjectId,
  user_id:    ObjectId,
  batch_id:   ObjectId | None,
  kind:       str,          # e.g. "batch_nearby", "batch_expired"
  message:    str,
  read:       bool,
  created_at: datetime
}

trust_log
{
  _id:        ObjectId,
  user_id:    ObjectId,
  batch_id:   ObjectId | None,
  delta:      int,
  reason:     str,
  created_at: datetime
}

delivery_proofs
{
  _id:          ObjectId,
  batch_id:     ObjectId (unique — one proof per batch),
  volunteer_id: ObjectId,
  content_type: "image/jpeg" | "image/png" | "image/webp",
  image_base64: str,          # compact base64 payload (no data: prefix)
  size_bytes:   int,
  uploaded_at:  datetime
}

chat_messages            # per-batch coordination thread (donor/NGO/volunteer)
{
  _id: ObjectId, batch_id: ObjectId, sender_id: ObjectId,
  sender_name: str, sender_role: str, text: str (<=500), created_at: datetime
}

ratings                  # post-completion peer reviews, one per (batch, rater, ratee)
{
  _id: ObjectId, batch_id: ObjectId, rater_id: ObjectId, ratee_id: ObjectId,
  rater_name: str, ratee_name: str, stars: 1-5, comment: str (<=300),
  created_at: datetime
}                        # aggregate mirrors on users: rating_sum, rating_count

recurring_donations      # donor-defined weekly schedules, auto-broadcast by the sweep
{
  _id: ObjectId, donor_id: ObjectId, food_description: str, quantity_kg: float,
  dietary_tags: [str], address: str, location: GeoJSON Point,
  days: [0-6 local weekdays], time: "HH:MM" (local), tz_offset_min: int,
  active: bool, next_run_at: datetime (UTC), last_run_at: datetime | None,
  runs: int, created_at: datetime
}

batch_templates          # saved broadcast presets for one-click re-donation
{
  _id: ObjectId, donor_id: ObjectId, name: str, food_description: str,
  quantity_kg: float, dietary_tags: [str], address: str,
  location: GeoJSON Point, times_used: int, created_at: datetime
}

platform_settings        # singleton config docs, e.g. _id "surge":
{ _id: "surge", active: bool, reason: str, multiplier: float,
  updated_at: datetime, updated_by: str }

New user fields: referral_code (unique), referred_by (ObjectId | None),
referral_bonus_awarded (bool), rating_sum / rating_count (ints),
availability ([\"<day 0-6>-<morning|afternoon|evening>\"], volunteers),
tz_offset_min (int), watch_zones ([zone names], NGOs),
suspended (bool) + suspended_reason (str).

New batch fields: prepared_hours_ago (float | None), safe_until (datetime |
None — food-safety hard stop), tracking ({position, updated_at, points}).
"""

import os
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, uri_parser
from pymongo.errors import (
    ConfigurationError,
    DuplicateKeyError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/food_rescue")

# Fail fast when MongoDB is down: the driver's 30-second default turns a dead
# database into a half-minute hang followed by an unreadable stack trace.
SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGO_TIMEOUT_MS", "5000"))

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS)

try:
    db = client.get_default_database()
except ConfigurationError:
    # URI carries no default database name — fall back explicitly.
    db = client["food_rescue"]


class DatabaseUnavailable(RuntimeError):
    """MongoDB is unreachable — raised at boot with operator instructions
    instead of leaking a 30-line pymongo traceback."""

users = db.users
food_batches = db.food_batches
matches = db.matches
notifications = db.notifications
trust_log = db.trust_log
delivery_proofs = db.delivery_proofs
chat_messages = db.chat_messages
ratings = db.ratings
recurring_donations = db.recurring_donations
batch_templates = db.batch_templates
platform_settings = db.platform_settings


def ping():
    """Verify the database is actually reachable before doing anything else.
    Raises DatabaseUnavailable with a fix-it message rather than a traceback."""
    try:
        client.admin.command("ping")
    except ServerSelectionTimeoutError as exc:
        # Show host:port only — a URI can carry credentials, which must never
        # be echoed into a log. ASCII only: the Windows console is not UTF-8.
        try:
            nodes = uri_parser.parse_uri(MONGO_URI)["nodelist"]
            hostport = ", ".join(f"{host}:{port}" for host, port in nodes)
        except Exception:
            hostport = "the configured host"
        raise DatabaseUnavailable(
            f"Cannot reach MongoDB at {hostport} (waited "
            f"{SERVER_SELECTION_TIMEOUT_MS / 1000:.0f}s).\n\n"
            "  The FoodRescue API needs a running MongoDB. To start it, open\n"
            "  PowerShell AS ADMINISTRATOR and run:\n\n"
            "      net start MongoDB\n\n"
            "  If the service starts and then dies, it is most likely running\n"
            "  out of memory - cap its cache by running scripts/fix-mongodb.ps1\n"
            "  (also as administrator), which is safe to re-run.\n"
        ) from exc


def initialize():
    """Create all indexes. Safe to call on every startup (idempotent).

    Unique-index builds fail if the collection already holds duplicate data
    (e.g. rows inserted while the index was missing). That must not brick the
    whole API at boot — log it loudly and keep serving.
    """
    ping()
    db.users.create_index([("location", "2dsphere")])
    db.food_batches.create_index([("pickup_location", "2dsphere")])
    db.food_batches.create_index([("status", 1), ("created_at", -1)])
    # Sweep queries: expiry, pickup-window and delivery-window enforcement.
    db.food_batches.create_index([("status", 1), ("expires_at", 1)])
    db.matches.create_index([("volunteer_id", ASCENDING)])
    db.notifications.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    # The notify() upsert key — keeps re-notification writes index-backed.
    db.notifications.create_index(
        [("user_id", ASCENDING), ("batch_id", ASCENDING), ("kind", ASCENDING)]
    )
    db.trust_log.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    # KYC review queue: admins list users by verification_status.
    db.users.create_index([("verification_status", ASCENDING)])
    # POS integration: API-key lookup by its public id part.
    db.users.create_index([("api_key_id", ASCENDING)], sparse=True)
    db.chat_messages.create_index([("batch_id", ASCENDING), ("created_at", ASCENDING)])
    db.ratings.create_index([("ratee_id", ASCENDING), ("created_at", DESCENDING)])
    db.recurring_donations.create_index([("active", ASCENDING), ("next_run_at", ASCENDING)])
    db.batch_templates.create_index([("donor_id", ASCENDING)])
    for collection, keys in (
        (db.users, [("email", ASCENDING)]),
        (db.delivery_proofs, [("batch_id", ASCENDING)]),
        (db.ratings, [("batch_id", ASCENDING), ("rater_id", ASCENDING), ("ratee_id", ASCENDING)]),
    ):
        try:
            collection.create_index(keys, unique=True)
        except (DuplicateKeyError, OperationFailure) as exc:
            print(
                f"[db] WARNING: unique index on {collection.name}{keys} not built "
                f"— existing duplicate data must be cleaned up manually: {exc}"
            )
    try:
        # sparse: legacy accounts predate referral codes and simply have none.
        db.users.create_index([("referral_code", ASCENDING)], unique=True, sparse=True)
    except (DuplicateKeyError, OperationFailure) as exc:
        print(f"[db] WARNING: referral_code unique index not built: {exc}")


def utcnow():
    return datetime.now(timezone.utc)


def geo_point(longitude, latitude):
    """Build a strict GeoJSON Point — longitude first. Raises ValueError on
    out-of-range coordinates so bad data never reaches a 2dsphere index."""
    longitude = float(longitude)
    latitude = float(latitude)
    if not (-180.0 <= longitude <= 180.0):
        raise ValueError("longitude must be between -180 and 180")
    if not (-90.0 <= latitude <= 90.0):
        raise ValueError("latitude must be between -90 and 90")
    return {"type": "Point", "coordinates": [longitude, latitude]}


def serialize(value):
    """Recursively convert BSON types to JSON-safe values. Naive datetimes
    coming back from MongoDB are UTC — tag them so browsers parse correctly."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serialize(val) for key, val in value.items()}
    if isinstance(value, list):
        return [serialize(item) for item in value]
    return value


# Donor-only secrets that must never reach NGO/volunteer clients.
PRIVATE_BATCH_FIELDS = ("pickup_code",)


def public_batch(batch):
    """Serialize a batch for a non-donor viewer — strips the handoff OTP."""
    safe = serialize(batch)
    if isinstance(safe, dict):
        for field in PRIVATE_BATCH_FIELDS:
            safe.pop(field, None)
    return safe


def adjust_trust(user_id, delta, reason, batch_id=None):
    """Apply a trust-score delta and record it in the audit log.

    Scoring rules:
      +50  donor self-delivers after no volunteer found (5x multiplier event)
      -20  NGO misses pickup window (>2 hours)
      +10  volunteer completes delivery
      -15  NGO/volunteer cancels after accepting
    """
    users.update_one({"_id": user_id}, {"$inc": {"trust_score": int(delta)}})
    # Trust never goes below zero — corrective floor after the atomic $inc.
    users.update_one(
        {"_id": user_id, "trust_score": {"$lt": 0}}, {"$set": {"trust_score": 0}}
    )
    entry = {
        "user_id": user_id,
        "batch_id": batch_id,
        "delta": int(delta),
        "reason": reason,
        "created_at": utcnow(),
    }
    trust_log.insert_one(entry)
    return entry


# ---------------------------------------------------------------------------
# Surge mode — a citywide emergency flag (flood relief, festival overload)
# that widens every matching radius. Cached briefly: it is read on every
# discovery request but changes maybe once a week.
# ---------------------------------------------------------------------------

SURGE_CACHE_SECONDS = 15
_surge_cache = {"state": None, "at": None}


def surge_state(fresh=False):
    """Current surge document: {active, reason, multiplier, ...}. Cached 15 s."""
    now = utcnow()
    cached, at = _surge_cache["state"], _surge_cache["at"]
    if not fresh and cached is not None and at is not None:
        if (now - at).total_seconds() < SURGE_CACHE_SECONDS:
            return cached
    doc = platform_settings.find_one({"_id": "surge"}) or {}
    state = {
        "active": bool(doc.get("active")),
        "reason": doc.get("reason") or "",
        "multiplier": float(doc.get("multiplier") or 2.0),
        "updated_at": doc.get("updated_at"),
    }
    _surge_cache["state"], _surge_cache["at"] = state, now
    return state


def surge_radius(base_meters):
    """Scale a matching radius by the surge multiplier when surge is active."""
    state = surge_state()
    if state["active"]:
        return int(base_meters * max(state["multiplier"], 1.0))
    return int(base_meters)


def notify(user_id, kind, message, batch_id=None):
    """Insert (or refresh) a notification. Upsert on (user, batch, kind) so
    escalation re-notifications never spam duplicates."""
    notifications.update_one(
        {"user_id": user_id, "batch_id": batch_id, "kind": kind},
        {
            "$set": {"message": message, "read": False},
            "$setOnInsert": {"created_at": utcnow()},
        },
        upsert=True,
    )


REFERRAL_BONUS = 10


def _award_referral_bonuses(batch):
    """Referral program payout: the first completed rescue a referred user is
    part of pays +10 trust to both them and their referrer — exactly once."""
    party_ids = {
        batch.get("donor_id"),
        batch.get("receiver_id"),
        batch.get("accepted_by"),
    }
    for user_id in filter(None, party_ids):
        # Atomic flip of referral_bonus_awarded: double-awards are impossible
        # even if two completions land in the same second.
        user = users.find_one_and_update(
            {
                "_id": user_id,
                "referred_by": {"$ne": None},
                "referral_bonus_awarded": {"$ne": True},
            },
            {"$set": {"referral_bonus_awarded": True}},
        )
        if user is None:
            continue
        referrer = users.find_one({"_id": user["referred_by"]})
        adjust_trust(
            user_id, REFERRAL_BONUS,
            "Referral bonus — completed your first rescue", batch["_id"],
        )
        notify(
            user_id, "referral_bonus",
            f"First rescue complete — +{REFERRAL_BONUS} referral trust bonus!",
            batch["_id"],
        )
        if referrer is not None:
            adjust_trust(
                referrer["_id"], REFERRAL_BONUS,
                f"Referral bonus — {user['name']} completed their first rescue",
                batch["_id"],
            )
            notify(
                referrer["_id"], "referral_bonus",
                f"{user['name']} completed their first rescue — "
                f"+{REFERRAL_BONUS} referral trust bonus for you!",
                batch["_id"],
            )


def record_match(batch, trust_score_changes=None):
    """Create the permanent Match document for a completed batch."""
    doc = {
        "batch_id": batch["_id"],
        "donor_id": batch["donor_id"],
        "receiver_id": batch.get("receiver_id"),
        "volunteer_id": batch["accepted_by"] if batch.get("delivery_mode") == "volunteer" else None,
        "matched_at": batch.get("accepted_at") or batch.get("claimed_at") or batch.get("created_at"),
        "completed_at": batch.get("completed_at"),
        "trust_score_changes": trust_score_changes or [],
    }
    matches.insert_one(doc)
    try:
        _award_referral_bonuses(batch)
    except Exception as exc:  # a referral hiccup must never break a completion
        print(f"[db] WARNING: referral bonus check failed: {exc}")
    return doc
