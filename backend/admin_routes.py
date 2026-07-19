"""
admin_routes.py — operations console endpoints (ADMIN_EMAILS-gated):

  GET  /admin/overview              platform health snapshot
  GET  /admin/users?q=&role=        search/browse accounts
  POST /admin/users/<id>/suspend    {"suspended": bool, "reason": str}
  POST /admin/users/<id>/trust      {"delta": int, "reason": str}
  GET  /admin/surge                 current surge state
  POST /admin/surge                 {"active": bool, "reason": str, "multiplier": float}

Gate: the ADMIN_EMAILS allowlist in .env (see auth.require_admin). When unset
(local development) any authenticated user passes — set it in production.
"""

import os
import re

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify, request

import db
import sms
import verification
from auth import require_admin

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

MAX_TRUST_DELTA = 100
MAX_SURGE_MULTIPLIER = 5.0
USER_PAGE_SIZE = 100

USER_FIELDS = {
    "name": 1, "email": 1, "role": 1, "trust_score": 1, "created_at": 1,
    "suspended": 1, "suspended_reason": 1, "active_batch_id": 1,
    "rating_sum": 1, "rating_count": 1, "partner_type": 1, "is_synthetic": 1,
    "referral_code": 1, "watch_zones": 1, "availability": 1,
    "verification_status": 1,
}


def _parse_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _admin_emails():
    return {
        email.strip().lower()
        for email in os.getenv("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }


def _user_row(user):
    row = db.serialize({key: user.get(key) for key in USER_FIELDS})
    row["_id"] = str(user["_id"])
    count = int(user.get("rating_count") or 0)
    row["rating"] = round(int(user.get("rating_sum") or 0) / count, 2) if count else None
    return row


@admin_bp.get("/overview")
@require_admin
def overview():
    """One-screen health snapshot for the ops console."""
    real = {"is_synthetic": {"$ne": True}}
    roles = {
        role: db.users.count_documents({"role": role, **real})
        for role in ("donor", "ngo", "volunteer")
    }
    status_counts = {
        row["_id"]: row["n"]
        for row in db.food_batches.aggregate(
            [{"$match": real}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        )
    }
    recent_ratings = [
        db.serialize(r)
        for r in db.ratings.find().sort("created_at", -1).limit(8)
    ]
    return jsonify(
        {
            "users": {**roles, "suspended": db.users.count_documents({"suspended": True})},
            "batches_by_status": status_counts,
            "recurring_schedules": db.recurring_donations.count_documents({"active": True}),
            "surge": db.serialize(db.surge_state(fresh=True)),
            "recent_ratings": recent_ratings,
            "pending_verifications": db.users.count_documents(
                {"verification_status": "pending", **real}
            ),
        }
    )


@admin_bp.get("/users")
@require_admin
def list_users():
    """Browse/search real accounts. ?q= matches name or email, ?role= filters."""
    query = {"is_synthetic": {"$ne": True}}
    role = (request.args.get("role") or "").strip().lower()
    if role in ("donor", "ngo", "volunteer"):
        query["role"] = role
    q = (request.args.get("q") or "").strip()
    if q:
        # Escape the needle — a user-supplied "(" must not blow up the regex.
        needle = re.compile(re.escape(q), re.IGNORECASE)
        query["$or"] = [{"name": needle}, {"email": needle}]

    cursor = db.users.find(query, USER_FIELDS).sort("created_at", -1).limit(USER_PAGE_SIZE)
    return jsonify({"users": [_user_row(u) for u in cursor]})


@admin_bp.post("/users/<user_id>/suspend")
@require_admin
def suspend_user(user_id):
    """Suspend or reinstate an account. Suspended users cannot log in, and
    every authenticated call they make is rejected."""
    object_id = _parse_object_id(user_id)
    if object_id is None:
        return jsonify({"error": "Invalid user id"}), 400
    target = db.users.find_one({"_id": object_id})
    if target is None:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    suspended = bool(data.get("suspended", True))
    reason = str(data.get("reason") or "").strip()[:200]

    # Never allow locking out an admin account (including yourself).
    if suspended and (
        target.get("role") == "admin"
        or (target.get("email") or "").lower() in _admin_emails()
    ):
        return jsonify({"error": "Admin accounts cannot be suspended"}), 409

    db.users.update_one(
        {"_id": object_id},
        {"$set": {"suspended": suspended, "suspended_reason": reason if suspended else ""}},
    )
    if not suspended:
        db.notify(object_id, "account_reinstated", "Your account has been reinstated. Welcome back!")
    return jsonify({"user_id": str(object_id), "suspended": suspended, "reason": reason})


@admin_bp.post("/users/<user_id>/trust")
@require_admin
def adjust_user_trust(user_id):
    """Manual trust correction with a mandatory audit reason."""
    object_id = _parse_object_id(user_id)
    if object_id is None:
        return jsonify({"error": "Invalid user id"}), 400
    if db.users.find_one({"_id": object_id}, {"_id": 1}) is None:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        delta = int(data.get("delta"))
    except (TypeError, ValueError):
        return jsonify({"error": "delta must be an integer"}), 400
    if delta == 0 or abs(delta) > MAX_TRUST_DELTA:
        return jsonify({"error": f"delta must be non-zero and within ±{MAX_TRUST_DELTA}"}), 400
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "A reason is required — trust changes are audited"}), 400

    entry = db.adjust_trust(object_id, delta, f"[admin] {reason[:180]}")
    updated = db.users.find_one({"_id": object_id}, {"trust_score": 1})
    return jsonify({"entry": db.serialize(entry), "trust_score": updated["trust_score"]})


# ---------------------------------------------------------------------------
# Account verification queue (KYC review)
# ---------------------------------------------------------------------------

def _verification_row(user):
    row = {
        "_id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role"),
        "address": user.get("address"),
        "created_at": db.serialize(user.get("created_at")),
        "trust_score": user.get("trust_score"),
    }
    row["verification"] = verification.public_view(user)
    return row


@admin_bp.get("/verifications")
@require_admin
def list_verifications():
    """Review queue. ?status=pending (default) | rejected | approved."""
    status = (request.args.get("status") or "pending").strip().lower()
    if status not in verification.VALID_STATUSES:
        return jsonify({"error": "status must be pending, approved or rejected"}), 400
    cursor = (
        db.users.find(
            {"verification_status": status, "is_synthetic": {"$ne": True}},
            # Exclude the base64 payload: the list stays light, and the partial
            # document object still drives public_view's has_document flag.
            {"password_hash": 0, "verification.document.image_base64": 0},
        )
        .sort("verification.submitted_at", 1)  # oldest submission first — FIFO queue
        .limit(USER_PAGE_SIZE)
    )
    return jsonify({"status": status, "requests": [_verification_row(u) for u in cursor]})


@admin_bp.get("/verifications/<user_id>/document")
@require_admin
def verification_document(user_id):
    """The submitted ID document image — fetched on demand so the queue list
    stays light."""
    object_id = _parse_object_id(user_id)
    if object_id is None:
        return jsonify({"error": "Invalid user id"}), 400
    user = db.users.find_one({"_id": object_id}, {"verification": 1, "name": 1})
    document = ((user or {}).get("verification") or {}).get("document")
    if not document:
        return jsonify({"error": "No ID document was uploaded"}), 404
    return jsonify(
        {
            "user_id": str(object_id),
            "name": user.get("name"),
            "content_type": document["content_type"],
            "data_uri": f"data:{document['content_type']};base64,{document['image_base64']}",
        }
    )


@admin_bp.post("/verifications/<user_id>/approve")
@require_admin
def approve_verification(user_id):
    object_id = _parse_object_id(user_id)
    if object_id is None:
        return jsonify({"error": "Invalid user id"}), 400
    user = db.users.find_one({"_id": object_id})
    if user is None:
        return jsonify({"error": "User not found"}), 404
    if user.get("verification_status", "approved") == "approved":
        return jsonify({"error": "Account is already verified"}), 409

    db.users.update_one(
        {"_id": object_id},
        {
            "$set": {
                "verification_status": "approved",
                "verification.reviewed_at": db.utcnow(),
                "verification.reviewed_by": g.current_user.get("email", ""),
                "verification.rejection_reason": None,
            }
        },
    )
    db.notify(
        object_id, "verification_approved",
        "🎉 Your account is verified — welcome aboard! You can now use every FoodRescue feature.",
    )
    sms.send_alert(user, "Your account is verified — welcome aboard! Sign in to start rescuing food.")
    return jsonify({"user_id": str(object_id), "verification_status": "approved"})


@admin_bp.post("/verifications/<user_id>/reject")
@require_admin
def reject_verification(user_id):
    object_id = _parse_object_id(user_id)
    if object_id is None:
        return jsonify({"error": "Invalid user id"}), 400
    user = db.users.find_one({"_id": object_id})
    if user is None:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "").strip()[:300]
    if not reason:
        return jsonify({"error": "A reason is required — the applicant sees it"}), 400

    db.users.update_one(
        {"_id": object_id},
        {
            "$set": {
                "verification_status": "rejected",
                "verification.reviewed_at": db.utcnow(),
                "verification.reviewed_by": g.current_user.get("email", ""),
                "verification.rejection_reason": reason,
            }
        },
    )
    db.notify(
        object_id, "verification_rejected",
        f"Your verification was declined: {reason} — please update your details and resubmit.",
    )
    sms.send_alert(user, f"Verification declined: {reason}. Update your details in the app and resubmit.")
    return jsonify({"user_id": str(object_id), "verification_status": "rejected", "reason": reason})


@admin_bp.get("/surge")
@require_admin
def get_surge():
    return jsonify({"surge": db.serialize(db.surge_state(fresh=True))})


@admin_bp.post("/surge")
@require_admin
def set_surge():
    """Flip emergency surge mode. Active surge multiplies every matching and
    discovery radius (default 2x) and shows a banner on all dashboards."""
    data = request.get_json(silent=True) or {}
    active = bool(data.get("active"))
    reason = str(data.get("reason") or "").strip()[:200]
    try:
        multiplier = float(data.get("multiplier", 2.0))
    except (TypeError, ValueError):
        return jsonify({"error": "multiplier must be a number"}), 400
    if not 1.0 <= multiplier <= MAX_SURGE_MULTIPLIER:
        return jsonify({"error": f"multiplier must be 1-{MAX_SURGE_MULTIPLIER:g}"}), 400
    if active and not reason:
        return jsonify({"error": "A public reason is required to activate surge mode"}), 400

    db.platform_settings.update_one(
        {"_id": "surge"},
        {
            "$set": {
                "active": active,
                "reason": reason,
                "multiplier": multiplier,
                "updated_at": db.utcnow(),
                "updated_by": g.current_user.get("email", ""),
            }
        },
        upsert=True,
    )
    state = db.surge_state(fresh=True)

    if active:
        # One bell per real user (deduped by db.notify's upsert on kind).
        message = f"🚨 Surge mode active: {reason} — matching radius widened {multiplier:g}x."
        for user in db.users.find(
            {"is_synthetic": {"$ne": True}, "role": {"$in": ["ngo", "volunteer"]}}, {"_id": 1}
        ).limit(500):
            db.notify(user["_id"], "surge_active", message)

    return jsonify({"surge": db.serialize(state)})
