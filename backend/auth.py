"""
auth.py — registration, login, and JWT middleware for FoodRescue.

Exposes:
  auth_bp                   Flask Blueprint mounted at /auth
  require_auth              decorator: valid Bearer token -> g.current_user
  require_role("ngo", ...)  decorator: require_auth + role check
"""

import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta, timezone
from functools import wraps

import jwt
import requests as http_requests
from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify, request
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

import db
import verification

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    # Never hardcode a secret. Fall back to an ephemeral random one so a dev
    # instance still boots — tokens simply won't survive a restart until
    # JWT_SECRET is set in .env.
    JWT_SECRET = secrets.token_hex(32)
    print("[auth] WARNING: JWT_SECRET not set in .env — using an ephemeral secret.")

JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "72"))

VALID_ROLES = {"donor", "ngo", "volunteer"}  # self-registration; admin is bootstrapped
VALID_PARTNER_TYPES = {"food_charity", "animal_shelter"}
DEFAULT_TRUST_SCORE = 100
MIN_PASSWORD_LENGTH = 8

import re as _re

# Pragmatic email shape — the real proof of ownership is the reset-code flow.
_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _registration_error(name, email, password, address):
    """First human-readable problem with the basic signup fields, or None.
    Junk signups waste the verification team's review time — catch the
    obvious ones before they ever reach the queue."""
    if len(name) < 3 or not any(ch.isalpha() for ch in name):
        return "Please enter your real full name / organization name (min 3 letters)"
    if not _EMAIL_RE.match(email):
        return "Please enter a valid email address"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if not (any(ch.isalpha() for ch in password) and any(ch.isdigit() for ch in password)):
        return "password must contain at least one letter and one number"
    if len(address) < 4:
        return "Please enter your full pickup/office address"
    return None

# --- Password reset (Brevo transactional email) ----------------------------
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "no-reply@foodrescue.local").strip()
SENDER_NAME = os.getenv("SENDER_NAME", "FoodRescue").strip()
RESET_CODE_TTL_MINUTES = 15
MAX_RESET_ATTEMPTS = 5

# Referral codes: unambiguous alphabet (no 0/O/1/I) — read over the phone.
REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 6


def _new_referral_code():
    """Unique 6-char referral code; retry on the (rare) collision."""
    for _ in range(20):
        code = "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
        if db.users.find_one({"referral_code": code}, {"_id": 1}) is None:
            return code
    return secrets.token_hex(6).upper()  # astronomically unlikely fallback


def _as_str(value):
    """NoSQL-injection guard: JSON bodies can smuggle dicts/lists into fields
    we pass to MongoDB (e.g. {"$gt": ""} as an email). Only accept strings."""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Rate limiting (in-memory sliding window, per IP + endpoint)
# ---------------------------------------------------------------------------
# Good enough for a single-process deployment; swap for a Redis-backed
# limiter (e.g. flask-limiter) when running multiple workers.

_rate_buckets = defaultdict(deque)


def rate_limit(max_calls, window_seconds):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            forwarded = request.headers.get("X-Forwarded-For", "")
            ip = (forwarded.split(",")[0].strip() if forwarded else None) or (
                request.remote_addr or "unknown"
            )
            bucket = _rate_buckets[f"{fn.__name__}:{ip}"]
            now = time.monotonic()
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= max_calls:
                return (
                    jsonify({"error": "Too many attempts — please wait a minute and try again"}),
                    429,
                )
            bucket.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _issue_token(user):
    payload = {
        "sub": str(user["_id"]),
        "role": user["role"],
        # Session revocation: bumping token_version on the user (password
        # change/reset) instantly invalidates every previously issued token.
        "ver": int(user.get("token_version") or 0),
        "iat": db.utcnow(),
        "exp": db.utcnow() + timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


_PRIVATE_USER_FIELDS = {
    "password_hash", "reset_code_hash", "reset_code_expires", "reset_attempts",
}


def _public_user(user):
    """User document safe to return to the client — no password hash, no
    reset-code material, and the (potentially multi-MB) ID-document image is
    collapsed to a has_document flag."""
    safe = {key: val for key, val in user.items() if key not in _PRIVATE_USER_FIELDS}
    if "verification" in safe:
        safe["verification"] = verification.public_view(user)
    safe.setdefault("verification_status", "approved")  # grandfathered accounts
    return db.serialize(safe)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

def require_auth(fn):
    """Verify the Bearer JWT and load the user onto flask.g.current_user."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401
        token = header.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        try:
            user = db.users.find_one({"_id": ObjectId(payload.get("sub", ""))})
        except InvalidId:
            user = None
        if user is None:
            return jsonify({"error": "User no longer exists"}), 401
        if int(payload.get("ver") or 0) != int(user.get("token_version") or 0):
            # Password changed since this token was minted — session revoked.
            return jsonify({"error": "Token expired"}), 401
        if user.get("suspended"):
            return jsonify({"error": "Account suspended — contact support"}), 403
        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


def _admin_emails():
    return {
        email.strip().lower()
        for email in os.getenv("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }


def is_admin_user(user):
    """A real admin account, or a legacy ADMIN_EMAILS allowlisted account."""
    if (user.get("role") or "") == "admin":
        return True
    return (user.get("email") or "").lower() in _admin_emails()


def admin_allowed():
    """Soft gate used by the data-science audit: real admins always pass;
    otherwise the legacy ADMIN_EMAILS behaviour applies (when the allowlist is
    unset — local dev — any authenticated user passes)."""
    if is_admin_user(g.current_user):
        return True
    return not _admin_emails()


def require_admin(fn):
    """require_auth plus the admin gate: the dedicated admin account
    (role "admin") or a legacy ADMIN_EMAILS-allowlisted user."""

    @wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        if not is_admin_user(g.current_user):
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)

    return wrapper


def require_role(*roles):
    """require_auth plus a role check, e.g. @require_role("ngo").

    Also enforces account verification: donors, NGOs and volunteers must be
    admin-approved (verification_status "approved") before they may act.
    Accounts predating the verification feature have no status field and are
    treated as approved."""

    def decorator(fn):
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.current_user.get("role") not in roles:
                return jsonify({"error": f"Requires role: {', '.join(roles)}"}), 403
            status = g.current_user.get("verification_status", "approved")
            if g.current_user.get("role") in verification.VERIFIED_ROLES and status != "approved":
                return (
                    jsonify(
                        {
                            "error": (
                                "Your account is awaiting verification by our team"
                                if status == "pending"
                                else "Your verification was rejected — please resubmit your details"
                            ),
                            "code": "not_verified",
                            "verification_status": status,
                        }
                    ),
                    403,
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.post("/register")
@rate_limit(max_calls=12, window_seconds=60)
def register():
    data = request.get_json(silent=True) or {}

    name = _as_str(data.get("name")).strip()
    email = _as_str(data.get("email")).strip().lower()
    password = _as_str(data.get("password"))
    role = _as_str(data.get("role")).strip().lower()
    address = _as_str(data.get("address")).strip()

    if not name or not email or not password:
        return jsonify({"error": "name, email and password are required"}), 400
    problem = _registration_error(name, email, password, address)
    if problem:
        return jsonify({"error": problem}), 400
    if role not in VALID_ROLES:
        return jsonify({"error": "role must be one of: donor, ngo, volunteer"}), 400

    try:
        # lat/lng arrive separately; storage is strict GeoJSON — longitude first.
        location = db.geo_point(data.get("longitude"), data.get("latitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "valid latitude and longitude are required"}), 400

    # Referral program: an optional code links the new account to its referrer;
    # both earn a trust bonus when the newcomer completes their first rescue.
    referred_by = None
    supplied_code = _as_str(data.get("referral_code")).strip().upper()
    if supplied_code:
        referrer = db.users.find_one({"referral_code": supplied_code})
        if referrer is None:
            return jsonify({"error": "Unknown referral code"}), 400
        referred_by = referrer["_id"]

    user = {
        "name": name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": role,
        "location": location,
        "address": address,
        "trust_score": DEFAULT_TRUST_SCORE,
        "created_at": db.utcnow(),
        "active_batch_id": None,
        "referral_code": _new_referral_code(),
        "referred_by": referred_by,
        "referral_bonus_awarded": False,
        "rating_sum": 0,
        "rating_count": 0,
    }
    if role == "ngo":
        partner_type = (_as_str(data.get("partner_type")) or "food_charity").strip().lower()
        if partner_type not in VALID_PARTNER_TYPES:
            partner_type = "food_charity"
        user["partner_type"] = partner_type
        user["accepts_non_veg"] = bool(data.get("accepts_non_veg", False))
        # Cold-chain capability: dairy/refrigeration-dependent batches only
        # match NGOs that declared cold storage.
        user["has_cold_storage"] = bool(data.get("has_cold_storage", False))

    # KYC: every new account submits role-specific identity details and waits
    # for an admin review before it can act on the platform.
    try:
        user["verification"] = verification.build_verification(role, data.get("verification"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    user["verification_status"] = "pending"

    try:
        result = db.users.insert_one(user)
    except DuplicateKeyError:
        return jsonify({"error": "An account with this email already exists"}), 409

    user["_id"] = result.inserted_id
    if referred_by is not None:
        db.notify(
            referred_by, "referral_signup",
            f"{name} joined FoodRescue with your referral code!",
        )
    return jsonify({"token": _issue_token(user), "user": _public_user(user)}), 201


@auth_bp.post("/login")
@rate_limit(max_calls=10, window_seconds=60)
def login():
    data = request.get_json(silent=True) or {}
    email = _as_str(data.get("email")).strip().lower()
    password = _as_str(data.get("password"))

    user = db.users.find_one({"email": email})
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password"}), 401
    if user.get("suspended"):
        return jsonify({"error": "Account suspended — contact support"}), 403

    return jsonify({"token": _issue_token(user), "user": _public_user(user)})


@auth_bp.get("/me")
@require_auth
def me():
    return jsonify({"user": _public_user(g.current_user)})


@auth_bp.get("/referral")
@require_auth
def my_referral():
    """The user's referral code plus how their invites are doing."""
    user = g.current_user
    code = user.get("referral_code")
    if not code:
        # Legacy account from before the referral program — mint one now.
        code = _new_referral_code()
        db.users.update_one({"_id": user["_id"]}, {"$set": {"referral_code": code}})
    referred = list(db.users.find({"referred_by": user["_id"]}, {"name": 1, "referral_bonus_awarded": 1}))
    return jsonify(
        {
            "referral_code": code,
            "referred_count": len(referred),
            "bonus_paid_count": sum(1 for r in referred if r.get("referral_bonus_awarded")),
            "bonus_per_rescue": db.REFERRAL_BONUS,
            "referred": [{"name": r["name"], "bonus_paid": bool(r.get("referral_bonus_awarded"))} for r in referred],
        }
    )


@auth_bp.get("/notifications")
@require_auth
def my_notifications():
    """Latest notifications for the current user — shared across all roles."""
    cursor = (
        db.notifications.find({"user_id": g.current_user["_id"]})
        .sort("created_at", -1)
        .limit(20)
    )
    return jsonify({"notifications": [db.serialize(n) for n in cursor]})


@auth_bp.post("/notifications/read")
@require_auth
def mark_notifications_read():
    result = db.notifications.update_many(
        {"user_id": g.current_user["_id"], "read": False}, {"$set": {"read": True}}
    )
    return jsonify({"marked_read": result.modified_count})


# ---------------------------------------------------------------------------
# Notification preferences (SMS alerts via Brevo)
# ---------------------------------------------------------------------------

@auth_bp.get("/preferences")
@require_auth
def get_preferences():
    import sms

    user = g.current_user
    return jsonify(
        {
            "preferences": {
                "sms_alerts": bool(user.get("sms_alerts")),
                "sms_ready": sms.wants_sms({**user, "sms_alerts": True}),
                "phone_on_file": bool(
                    ((user.get("verification") or {}).get("details") or {}).get("phone")
                ),
            }
        }
    )


@auth_bp.post("/preferences")
@require_auth
def set_preferences():
    """Opt in/out of SMS alerts for high-value events (new rescues nearby,
    routes available, verification decisions). Requires the phone number
    submitted during KYC verification."""
    data = request.get_json(silent=True) or {}
    updates = {}
    if "sms_alerts" in data:
        wants = bool(data.get("sms_alerts"))
        if wants:
            phone = ((g.current_user.get("verification") or {}).get("details") or {}).get("phone")
            if not phone:
                return jsonify({"error": "No phone number on file — add one via verification details"}), 400
        updates["sms_alerts"] = wants
    if not updates:
        return jsonify({"error": "Nothing to update — send sms_alerts"}), 400
    db.users.update_one({"_id": g.current_user["_id"]}, {"$set": updates})
    return jsonify({"preferences": {"sms_alerts": updates.get("sms_alerts", False)}})


# ---------------------------------------------------------------------------
# Account verification (KYC) — status + resubmission
# ---------------------------------------------------------------------------

@auth_bp.get("/verification")
@require_auth
def my_verification():
    """The current user's verification status and submitted details."""
    return jsonify({"verification": verification.public_view(g.current_user)})


@auth_bp.post("/verification")
@rate_limit(max_calls=6, window_seconds=60)
@require_auth
def resubmit_verification():
    """Update the verification submission (typically after a rejection).
    Resets the status to pending for a fresh admin review."""
    user = g.current_user
    role = user.get("role")
    if role not in verification.VERIFIED_ROLES:
        return jsonify({"error": "This account type does not need verification"}), 400
    if user.get("verification_status", "approved") == "approved":
        return jsonify({"error": "Your account is already verified"}), 409

    data = request.get_json(silent=True) or {}
    try:
        doc = verification.build_verification(role, data.get("verification") or data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Keep the previously uploaded document when the resubmission has none.
    if doc["document"] is None:
        doc["document"] = (user.get("verification") or {}).get("document")

    db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"verification": doc, "verification_status": "pending"}},
    )
    db.notify(
        user["_id"], "verification_pending",
        "Thanks — your updated details were submitted and are back in the review queue.",
    )
    updated = db.users.find_one({"_id": user["_id"]})
    return jsonify({"verification": verification.public_view(updated)})


# ---------------------------------------------------------------------------
# Forgot / reset password — one-time code emailed via the Brevo API
# ---------------------------------------------------------------------------

def _send_reset_email(email, name, code):
    """Send the reset code through Brevo's transactional-email API.
    Returns True when the API accepted the message. Without an API key
    (local development) nothing is sent and the caller falls back to
    returning the code in the response."""
    if not BREVO_API_KEY:
        return False
    body = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": email, "name": name or email}],
        "subject": f"{code} is your FoodRescue password reset code",
        "htmlContent": (
            "<div style='font-family:Arial,sans-serif;max-width:480px;margin:auto'>"
            "<h2 style='color:#7c3aed'>&#129365; FoodRescue</h2>"
            f"<p>Hi {name or 'there'},</p>"
            "<p>We received a request to reset your FoodRescue password. "
            "Enter this code to continue:</p>"
            f"<p style='font-size:32px;font-weight:bold;letter-spacing:6px;"
            f"color:#c026d3;text-align:center'>{code}</p>"
            f"<p>The code expires in {RESET_CODE_TTL_MINUTES} minutes. "
            "If you didn't ask for this, you can safely ignore this email — "
            "your password stays unchanged.</p>"
            "</div>"
        ),
    }
    try:
        resp = http_requests.post(
            BREVO_API_URL,
            json=body,
            headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 201, 202):
            return True
        print(f"[auth] Brevo send failed: {resp.status_code} {resp.text[:200]}")
    except http_requests.RequestException as exc:
        print(f"[auth] Brevo send error: {exc}")
    return False


@auth_bp.post("/forgot-password")
@rate_limit(max_calls=5, window_seconds=60)
def forgot_password():
    """Start a password reset. Always answers 200 with a generic message so
    the endpoint cannot be used to probe which emails have accounts."""
    data = request.get_json(silent=True) or {}
    email = _as_str(data.get("email")).strip().lower()
    generic = {"message": "If that email is registered, a reset code has been sent."}
    if not email:
        return jsonify({"error": "email is required"}), 400

    user = db.users.find_one({"email": email})
    if user is None or user.get("is_synthetic"):
        return jsonify(generic)

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "reset_code_hash": generate_password_hash(code),
                "reset_code_expires": db.utcnow() + timedelta(minutes=RESET_CODE_TTL_MINUTES),
                "reset_attempts": 0,
            }
        },
    )
    sent = _send_reset_email(email, user.get("name"), code)
    response = dict(generic, email_sent=sent, expires_in_minutes=RESET_CODE_TTL_MINUTES)
    if not sent:
        # No BREVO_API_KEY configured (local development): surface the code so
        # the flow stays testable without an email provider. Never happens in
        # production — set BREVO_API_KEY there.
        print(f"[auth] DEV password-reset code for {email}: {code}")
        response["dev_code"] = code
    return jsonify(response)


@auth_bp.post("/reset-password")
@rate_limit(max_calls=8, window_seconds=60)
def reset_password():
    """Finish a password reset: email + emailed code + new password."""
    data = request.get_json(silent=True) or {}
    email = _as_str(data.get("email")).strip().lower()
    code = _as_str(data.get("code")).strip()
    new_password = _as_str(data.get("new_password"))

    if not email or not code:
        return jsonify({"error": "email and code are required"}), 400
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"new password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
    if not (any(c.isalpha() for c in new_password) and any(c.isdigit() for c in new_password)):
        return jsonify({"error": "new password must contain at least one letter and one number"}), 400

    invalid = ({"error": "Invalid or expired reset code"}, 400)
    user = db.users.find_one({"email": email})
    if user is None or not user.get("reset_code_hash"):
        return jsonify(invalid[0]), invalid[1]

    expires = user.get("reset_code_expires")
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is None or expires < db.utcnow():
        return jsonify(invalid[0]), invalid[1]
    if int(user.get("reset_attempts") or 0) >= MAX_RESET_ATTEMPTS:
        return jsonify({"error": "Too many wrong codes — request a new reset code"}), 429

    if not check_password_hash(user["reset_code_hash"], code):
        db.users.update_one({"_id": user["_id"]}, {"$inc": {"reset_attempts": 1}})
        return jsonify(invalid[0]), invalid[1]

    db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"password_hash": generate_password_hash(new_password)},
            # A reset usually means the old password (and possibly open
            # sessions) can't be trusted — revoke every issued token.
            "$inc": {"token_version": 1},
            "$unset": {"reset_code_hash": "", "reset_code_expires": "", "reset_attempts": ""},
        },
    )
    db.notify(user["_id"], "password_reset", "Your password was changed successfully.")
    return jsonify({"message": "Password updated — you can sign in with your new password."})


# ---------------------------------------------------------------------------
# Account settings — profile, change password, delete account
# ---------------------------------------------------------------------------

def _password_problem(password):
    """The registration-strength rules, shared with change-password."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if not (any(ch.isalpha() for ch in password) and any(ch.isdigit() for ch in password)):
        return "password must contain at least one letter and one number"
    return None


@auth_bp.post("/change-password")
@rate_limit(max_calls=6, window_seconds=60)
@require_auth
def change_password():
    """Rotate the password while signed in: current password + new password.
    (Forgot-password stays the recovery path for a lost current password.)"""
    data = request.get_json(silent=True) or {}
    current = _as_str(data.get("current_password"))
    new_password = _as_str(data.get("new_password"))

    if not check_password_hash(g.current_user["password_hash"], current):
        return jsonify({"error": "Current password is incorrect"}), 403
    problem = _password_problem(new_password)
    if problem:
        return jsonify({"error": f"new {problem}"}), 400
    if new_password == current:
        return jsonify({"error": "New password must be different from the current one"}), 400

    db.users.update_one(
        {"_id": g.current_user["_id"]},
        {
            "$set": {"password_hash": generate_password_hash(new_password)},
            # Revoke every existing session — a stolen token dies right here.
            "$inc": {"token_version": 1},
            # Any in-flight reset code predates this change — kill it.
            "$unset": {"reset_code_hash": "", "reset_code_expires": "", "reset_attempts": ""},
        },
    )
    db.notify(g.current_user["_id"], "password_changed", "Your password was changed successfully.")
    # A fresh token for THIS session so the user stays signed in; every other
    # session (other devices, or a hijacker) is now locked out.
    updated = db.users.find_one({"_id": g.current_user["_id"]})
    return jsonify({"message": "Password updated.", "token": _issue_token(updated)})


@auth_bp.post("/profile")
@rate_limit(max_calls=10, window_seconds=60)
@require_auth
def update_profile():
    """Update name / address / location (and the NGO dietary flag). Only the
    fields present in the body change. Location matters: the matching engine
    is geospatial, so a moved kitchen or NGO must be able to correct it."""
    data = request.get_json(silent=True) or {}
    user = g.current_user
    updates = {}

    if "name" in data:
        name = _as_str(data.get("name")).strip()
        if len(name) < 3 or not any(ch.isalpha() for ch in name):
            return jsonify({"error": "Please enter your real full name / organization name (min 3 letters)"}), 400
        updates["name"] = name

    if "address" in data:
        address = _as_str(data.get("address")).strip()
        if len(address) < 4:
            return jsonify({"error": "Please enter your full pickup/office address"}), 400
        updates["address"] = address

    if data.get("latitude") is not None or data.get("longitude") is not None:
        try:
            updates["location"] = db.geo_point(data.get("longitude"), data.get("latitude"))
        except (TypeError, ValueError):
            return jsonify({"error": "valid latitude and longitude are required"}), 400

    if user.get("role") == "ngo" and "accepts_non_veg" in data:
        updates["accepts_non_veg"] = bool(data.get("accepts_non_veg"))

    if not updates:
        return jsonify({"error": "Nothing to update — send name, address or latitude/longitude"}), 400

    db.users.update_one({"_id": user["_id"]}, {"$set": updates})
    updated = db.users.find_one({"_id": user["_id"]})
    return jsonify({"user": _public_user(updated)})


# Statuses in which a batch still has people counting on it.
_ACTIVE_BATCH_STATUSES = (
    "pending", "ngo_pickup", "volunteer_needed", "in_transit", "donor_delivering",
)


@auth_bp.post("/delete-account")
@rate_limit(max_calls=4, window_seconds=60)
@require_auth
def delete_account():
    """Permanently delete the account (password-confirmed). Refused while any
    rescue involving the account is still active — food already in motion
    must reach its destination. Completed history (matches, ratings, chat)
    keeps only the name embedded at the time; the login, KYC document,
    schedules and notifications are erased."""
    user = g.current_user
    if user.get("role") == "admin":
        return jsonify({"error": "Admin accounts are managed via .env, not deletable in-app"}), 403

    data = request.get_json(silent=True) or {}
    if not check_password_hash(user["password_hash"], _as_str(data.get("password"))):
        return jsonify({"error": "Password is incorrect"}), 403

    active = db.food_batches.count_documents(
        {
            "status": {"$in": list(_ACTIVE_BATCH_STATUSES)},
            "$or": [
                {"donor_id": user["_id"]},
                {"receiver_id": user["_id"]},
                {"accepted_by": user["_id"]},
            ],
        }
    )
    if active:
        return (
            jsonify(
                {
                    "error": (
                        "You still have an active rescue in progress — complete or "
                        "cancel it first, then delete your account."
                    )
                }
            ),
            409,
        )

    db.notifications.delete_many({"user_id": user["_id"]})
    db.batch_templates.delete_many({"donor_id": user["_id"]})
    db.recurring_donations.delete_many({"donor_id": user["_id"]})
    db.users.delete_one({"_id": user["_id"]})
    # Existing JWTs die with the user document ("User no longer exists").
    return jsonify({"deleted": True, "message": "Your account has been deleted. Thank you for every rescue."})


# ---------------------------------------------------------------------------
# Admin account bootstrap
# ---------------------------------------------------------------------------

def ensure_admin_account():
    """Create (once) the dedicated administrator account from ADMIN_EMAIL /
    ADMIN_PASSWORD in .env. Runs at every startup; idempotent. The admin signs
    in on the admin console with these separate credentials."""
    email = os.getenv("ADMIN_EMAIL", "admin@foodrescue.local").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "admin12345")

    existing = db.users.find_one({"email": email})
    if existing is not None:
        if existing.get("role") != "admin":
            print(f"[auth] WARNING: ADMIN_EMAIL {email} belongs to a non-admin account — not promoted.")
        return
    db.users.insert_one(
        {
            "name": "Platform Admin",
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "admin",
            "location": db.geo_point(0.0, 0.0),
            "address": "FoodRescue HQ",
            "trust_score": DEFAULT_TRUST_SCORE,
            "created_at": db.utcnow(),
            "active_batch_id": None,
            "referral_code": _new_referral_code(),
            "referred_by": None,
            "referral_bonus_awarded": False,
            "rating_sum": 0,
            "rating_count": 0,
            "verification_status": "approved",
        }
    )
    if password == "admin12345":
        print(
            f"[auth] Admin account created: {email} with the DEFAULT password — "
            "set ADMIN_EMAIL/ADMIN_PASSWORD in .env before going live."
        )
    else:
        print(f"[auth] Admin account created: {email}")
