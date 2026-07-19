"""
integrations.py — machine-to-machine endpoints for donor systems.

The flagship use case: a restaurant's POS / inventory system calls
POST /integrations/pos/broadcast at closing time with whatever surplus is
left, and the batch enters the normal matching flow — no human, no browser.

Authentication: per-donor API keys ("frk_<id>_<secret>").
  * The donor generates a key from their dashboard (shown exactly once).
  * Only a salted HASH of the key is stored; the public <id> part is indexed
    for lookup, the <secret> part is verified with check_password_hash.
  * Suspended or unverified donors are refused even with a valid key.
"""

import secrets

from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

import db
import donor_routes
from auth import rate_limit, require_role

integrations_bp = Blueprint("integrations", __name__, url_prefix="/integrations")

KEY_PREFIX = "frk"


def generate_api_key():
    """Returns (full_key, key_id, key_hash). Only the hash is persisted."""
    key_id = secrets.token_hex(4)          # public lookup handle
    key_secret = secrets.token_hex(16)     # never stored in clear
    full_key = f"{KEY_PREFIX}_{key_id}_{key_secret}"
    return full_key, key_id, generate_password_hash(full_key)


# ---------------------------------------------------------------------------
# Donor-side key management (JWT-authenticated, mounted here for cohesion)
# ---------------------------------------------------------------------------

@integrations_bp.get("/api-key")
@require_role("donor")
def api_key_status():
    """Masked info about the donor's POS key — the key itself is never
    retrievable after generation."""
    user = g.current_user
    if not user.get("api_key_hash"):
        return jsonify({"api_key": None})
    return jsonify(
        {
            "api_key": {
                "key_id": user.get("api_key_id"),
                "masked": f"{KEY_PREFIX}_{user.get('api_key_id')}_" + "•" * 12,
                "created_at": db.serialize(user.get("api_key_created_at")),
                "last_used_at": db.serialize(user.get("api_key_last_used_at")),
                "uses": int(user.get("api_key_uses") or 0),
            }
        }
    )


@integrations_bp.post("/api-key")
@require_role("donor")
def create_api_key():
    """Generate (or rotate) the donor's POS API key. The full key appears in
    this response only — store it in the POS system immediately."""
    full_key, key_id, key_hash = generate_api_key()
    db.users.update_one(
        {"_id": g.current_user["_id"]},
        {
            "$set": {
                "api_key_id": key_id,
                "api_key_hash": key_hash,
                "api_key_created_at": db.utcnow(),
                "api_key_last_used_at": None,
                "api_key_uses": 0,
            }
        },
    )
    return jsonify(
        {
            "api_key": full_key,
            "note": "Store this key now — it is shown only once. "
                    "POST it as the X-API-Key header to /integrations/pos/broadcast.",
        }
    ), 201


@integrations_bp.delete("/api-key")
@require_role("donor")
def revoke_api_key():
    db.users.update_one(
        {"_id": g.current_user["_id"]},
        {"$unset": {"api_key_id": "", "api_key_hash": "", "api_key_created_at": "",
                    "api_key_last_used_at": "", "api_key_uses": ""}},
    )
    return jsonify({"revoked": True})


# ---------------------------------------------------------------------------
# The POS webhook itself (API-key authenticated — no JWT, no browser)
# ---------------------------------------------------------------------------

def _donor_from_api_key(full_key):
    """Resolve and verify an API key. Returns (donor, error_response)."""
    parts = (full_key or "").strip().split("_")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None, (jsonify({"error": "Invalid API key"}), 401)
    donor = db.users.find_one({"api_key_id": parts[1], "role": "donor"})
    if donor is None or not donor.get("api_key_hash"):
        return None, (jsonify({"error": "Invalid API key"}), 401)
    if not check_password_hash(donor["api_key_hash"], full_key.strip()):
        return None, (jsonify({"error": "Invalid API key"}), 401)
    if donor.get("suspended"):
        return None, (jsonify({"error": "Account suspended"}), 403)
    if donor.get("verification_status", "approved") != "approved":
        return None, (jsonify({"error": "Account pending verification"}), 403)
    return donor, None


@integrations_bp.post("/pos/broadcast")
@rate_limit(max_calls=30, window_seconds=60)
def pos_broadcast():
    """Closing-time auto-broadcast from a POS/inventory system.

    Headers:  X-API-Key: frk_...
    Body:     {"food_description": "...", "quantity_kg": 12.5,
               "prepared_hours_ago": 1.5,        (optional)
               "address": "...", "latitude": .., "longitude": ..}  (optional —
               defaults to the donor's registered address/location)
    """
    donor, error = _donor_from_api_key(request.headers.get("X-API-Key"))
    if error:
        return error

    data = request.get_json(silent=True) or {}

    # POS systems rarely know geography — default to the donor's profile.
    address = str(data.get("address") or "").strip() or donor.get("address") or ""
    if data.get("latitude") is not None and data.get("longitude") is not None:
        try:
            pickup_location = db.geo_point(data.get("longitude"), data.get("latitude"))
        except (TypeError, ValueError):
            return jsonify({"error": "valid latitude and longitude are required"}), 400
    else:
        pickup_location = donor.get("location")
        if not pickup_location:
            return jsonify({"error": "Donor profile has no location — send latitude/longitude"}), 400

    try:
        batch = donor_routes.create_batch(
            donor,
            data.get("food_description"),
            data.get("quantity_kg"),
            address,
            pickup_location,
            dietary_tags=data.get("dietary_tags"),
            prepared_hours_ago=data.get("prepared_hours_ago"),
            cold_chain=data.get("cold_chain"),
            source="pos",
        )
    except donor_routes.BatchError as exc:
        return jsonify({"error": str(exc)}), 400

    db.users.update_one(
        {"_id": donor["_id"]},
        {"$set": {"api_key_last_used_at": db.utcnow()}, "$inc": {"api_key_uses": 1}},
    )
    db.notify(
        donor["_id"], "pos_broadcast",
        f"🤖 Your POS system broadcast \"{batch['food_description']}\" "
        f"({batch['quantity_kg']} kg) — matching NGOs now.",
        batch["_id"],
    )
    return jsonify(
        {
            "batch_id": str(batch["_id"]),
            "status": batch["status"],
            "expires_at": db.serialize(batch["expires_at"]),
            "cold_chain": bool(batch.get("cold_chain")),
        }
    ), 201
