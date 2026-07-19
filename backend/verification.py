"""
verification.py — KYC-style account verification for FoodRescue.

Every new donor / NGO / volunteer submits role-specific identity details at
registration. The account starts as verification_status="pending" and cannot
perform role activities (broadcast, accept, claim, …) until an administrator
reviews the submission in the admin console and approves it.

User-document fields:

  verification_status: "pending" | "approved" | "rejected"
  verification: {
    details:          {role-specific cleaned fields, see REQUIRED/OPTIONAL},
    document:         {content_type, image_base64} | None   # ID photo/scan
    submitted_at:     datetime,
    reviewed_at:      datetime | None,
    reviewed_by:      str | None,          # admin email
    rejection_reason: str | None,
  }

Accounts created before this feature have neither field — they are treated as
approved (grandfathered), so no migration is needed.
"""

import re

import db

# Roles that must pass verification before acting. Admins are exempt.
VERIFIED_ROLES = {"donor", "ngo", "volunteer"}

VALID_STATUSES = {"pending", "approved", "rejected"}

DONOR_ORG_TYPES = {"restaurant", "caterer", "event_hall", "hostel_canteen", "household", "other"}
VEHICLE_TYPES = {"bicycle", "bike", "scooter", "car", "van", "other"}

# Generous international phone shape: optional +, 7-15 digits (spaces/dashes ok).
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{5,17}[0-9]$")
# Licence / registration ids: letters, digits, spaces, slashes and dashes.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-/]{4,39}$")
# Indian driving licences start with a two-letter state code + RTO digits
# (e.g. MP09 20210012345). We check the prefix, not the whole grammar —
# formats drifted across decades and states.
_LICENCE_PREFIX_RE = re.compile(r"^[A-Z]{2}[ -]?[0-9]{1,2}")

MAX_FIELD_CHARS = 80


def _as_str(value):
    return value if isinstance(value, str) else ""


def _validate_phone(raw):
    """A phone number a human could actually answer. Blocks the classic
    keyboard-mash entries (0000000000, 1111111111 …)."""
    raw = _as_str(raw).strip()
    if not _PHONE_RE.match(raw):
        raise ValueError("A valid contact phone number is required for verification")
    digits = re.sub(r"\D", "", raw)
    if len(set(digits)) < 4:
        raise ValueError("That phone number doesn't look real — please enter your actual number")
    return raw[:20]


def _clean_id(value, label, required, licence=False):
    """Normalize a licence/registration number; raise ValueError when bad.
    Real-world ids always carry digits — pure text like \"none\" or \"abcd\"
    is exactly the junk a review team wastes time rejecting by hand."""
    value = _as_str(value).strip().upper()
    if not value:
        if required:
            raise ValueError(f"{label} is required for verification")
        return ""
    if not _ID_RE.match(value):
        raise ValueError(f"{label} looks invalid — use 5-40 letters/digits")
    if sum(ch.isdigit() for ch in value) < 2:
        raise ValueError(f"{label} looks invalid — a real ID contains digits")
    if licence and not _LICENCE_PREFIX_RE.match(value):
        raise ValueError(
            f"{label} must start with the two-letter state code and RTO number "
            "(e.g. MP09 20210012345)"
        )
    return value[:MAX_FIELD_CHARS]


def validate_details(role, data):
    """Validate + clean the role-specific verification fields from a client
    payload. Returns the cleaned details dict; raises ValueError on problems."""
    if role not in VERIFIED_ROLES:
        raise ValueError("verification does not apply to this role")
    data = data if isinstance(data, dict) else {}

    details = {"phone": _validate_phone(data.get("phone"))}

    if role == "donor":
        org_type = _as_str(data.get("org_type")).strip().lower() or "other"
        if org_type not in DONOR_ORG_TYPES:
            org_type = "other"
        details["org_type"] = org_type
        details["business_name"] = _as_str(data.get("business_name")).strip()[:MAX_FIELD_CHARS]
        # Households donate leftovers without a licence; businesses must show
        # their FSSAI / shop-establishment / government id.
        details["id_number"] = _clean_id(
            data.get("id_number"), "Business / FSSAI / government ID",
            required=(org_type != "household"),
        )
    elif role == "ngo":
        details["registration_number"] = _clean_id(
            data.get("registration_number"), "NGO registration number", required=True
        )
        details["darpan_id"] = _clean_id(data.get("darpan_id"), "NGO Darpan ID", required=False)
        details["id_number"] = _clean_id(data.get("id_number"), "Authorised-person ID", required=False)
    else:  # volunteer
        details["driving_licence"] = _clean_id(
            data.get("driving_licence"), "Driving licence number", required=True,
            licence=True,
        )
        vehicle = _as_str(data.get("vehicle_type")).strip().lower() or "bike"
        if vehicle not in VEHICLE_TYPES:
            vehicle = "other"
        details["vehicle_type"] = vehicle
        details["id_number"] = _clean_id(data.get("id_number"), "Government ID", required=False)

    return details


def validate_id_document(data_uri):
    """Validate the optional ID-document image (same rules as proof photos).
    Returns {content_type, image_base64} or None when no document supplied.
    Raises ValueError on an invalid image."""
    if not data_uri:
        return None
    # Lazy import: media_routes imports auth at module level; importing it here
    # at call time avoids a circular import at boot.
    from media_routes import validate_proof_photo

    try:
        content_type, payload = validate_proof_photo(data_uri)
    except ValueError as exc:
        raise ValueError(str(exc).replace("proof photo", "ID document").replace("proof_photo", "id_document"))
    return {"content_type": content_type, "image_base64": payload}


def build_verification(role, data):
    """Full verification sub-document for a new/updated submission."""
    return {
        "details": validate_details(role, data),
        "document": validate_id_document(data.get("id_document") if isinstance(data, dict) else None),
        "submitted_at": db.utcnow(),
        "reviewed_at": None,
        "reviewed_by": None,
        "rejection_reason": None,
    }


def public_view(user):
    """Verification info safe to echo to the account owner / admin lists —
    the base64 document payload is replaced with a has_document flag."""
    verification = user.get("verification") or {}
    document = verification.get("document")
    return {
        "status": user.get("verification_status", "approved"),
        "details": verification.get("details") or {},
        "has_document": bool(document),
        "submitted_at": db.serialize(verification.get("submitted_at")),
        "reviewed_at": db.serialize(verification.get("reviewed_at")),
        "rejection_reason": verification.get("rejection_reason"),
    }
