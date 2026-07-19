"""
media_routes.py — proof-of-delivery photo handling.

Photos are stored as validated, size-capped base64 inside MongoDB
(delivery_proofs collection) rather than on the filesystem: no orphan files,
no static-file serving, no path-traversal surface, and the 16 MB document
limit comfortably fits the 4 MB photo cap.

Security applied to every upload:
  * strict data-URI grammar (JPEG / PNG / WebP only)
  * base64 must decode cleanly (validate=True)
  * decoded size capped at MAX_PROOF_BYTES
  * magic-byte check — the payload must really be the declared image type
Retrieval is restricted to the three parties of the batch (donor, receiving
NGO, volunteer).
"""

import base64
import re

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, g, jsonify

import db
from auth import require_auth

media_bp = Blueprint("media", __name__, url_prefix="/media")

MAX_PROOF_BYTES = 4 * 1024 * 1024  # 4 MB decoded
MIN_PROOF_BYTES = 100              # reject empty/garbage "images"

_DATA_URI_RE = re.compile(
    r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\s]+)$"
)

_MAGIC_CHECKS = {
    "image/jpeg": lambda raw: raw.startswith(b"\xff\xd8\xff"),
    "image/png": lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda raw: raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
}


def validate_proof_photo(data_uri):
    """Validate an image data URI. Returns (content_type, compact_base64).
    Raises ValueError with a client-safe message on any problem."""
    if not isinstance(data_uri, str):
        raise ValueError("proof_photo must be a base64 image data URI")

    match = _DATA_URI_RE.match(data_uri.strip())
    if match is None:
        raise ValueError("proof_photo must be a JPEG, PNG or WebP data URI")

    content_type = match.group(1)
    payload = re.sub(r"\s+", "", match.group(2))

    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        raise ValueError("proof_photo is not valid base64")

    if len(raw) > MAX_PROOF_BYTES:
        raise ValueError("proof photo is too large (max 4 MB)")
    if len(raw) < MIN_PROOF_BYTES:
        raise ValueError("proof photo is too small to be a real image")
    if not _MAGIC_CHECKS[content_type](raw):
        raise ValueError("proof photo content does not match its declared image type")

    return content_type, payload


def save_proof(batch, volunteer_id, data_uri):
    """Validate and persist the delivery proof for a batch (one per batch).
    Returns the proof document. Raises ValueError on invalid input."""
    content_type, payload = validate_proof_photo(data_uri)
    doc = {
        "batch_id": batch["_id"],
        "volunteer_id": volunteer_id,
        "content_type": content_type,
        "image_base64": payload,
        "size_bytes": len(payload) * 3 // 4,
        "uploaded_at": db.utcnow(),
    }
    db.delivery_proofs.replace_one({"batch_id": batch["_id"]}, doc, upsert=True)
    db.food_batches.update_one(
        {"_id": batch["_id"]}, {"$set": {"has_proof": True}}
    )
    return doc


@media_bp.get("/proof/<batch_id>")
@require_auth
def get_proof(batch_id):
    """Return the delivery-proof photo — only to the batch's own parties."""
    try:
        object_id = ObjectId(batch_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid batch id"}), 400

    batch = db.food_batches.find_one({"_id": object_id})
    if batch is None:
        return jsonify({"error": "Batch not found"}), 404

    proof = db.delivery_proofs.find_one({"batch_id": object_id})
    if proof is None:
        return jsonify({"error": "No delivery proof uploaded for this batch"}), 404

    allowed_ids = {
        batch.get("donor_id"),
        batch.get("receiver_id"),
        batch.get("accepted_by"),
        proof.get("volunteer_id"),
    }
    if g.current_user["_id"] not in allowed_ids:
        return jsonify({"error": "You are not a party to this delivery"}), 403

    return jsonify(
        {
            "batch_id": str(object_id),
            "content_type": proof["content_type"],
            "data_uri": f"data:{proof['content_type']};base64,{proof['image_base64']}",
            "uploaded_at": db.serialize(proof["uploaded_at"]),
        }
    )
