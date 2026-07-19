"""
sms.py — opt-in SMS/WhatsApp-style alerts through the Brevo transactional
SMS API (the same BREVO_API_KEY used for password-reset emails).

Philosophy: SMS is for the moments a browser tab can't cover — "food is
waiting 900 m away" — so only a handful of high-value events send one, and
only to users who opted in (sms_alerts: true) and gave a phone number during
KYC verification. Without an API key (local development) the message is
logged instead, so the flow stays fully demonstrable offline.
"""

import os
import re
import threading

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SMS_URL = "https://api.brevo.com/v3/transactionalSMS/sms"
# Brevo caps alphanumeric sender ids at 11 chars.
SMS_SENDER = (os.getenv("SENDER_NAME", "FoodRescue").strip() or "FoodRescue")[:11]
MAX_SMS_CHARS = 155  # keep every alert to a single SMS segment

_PHONE_CLEAN_RE = re.compile(r"[^\d+]")


def _phone_of(user):
    """The verified contact phone from the KYC submission, normalized to
    digits (international format), or None."""
    details = ((user.get("verification") or {}).get("details")) or {}
    raw = str(details.get("phone") or "").strip()
    if not raw:
        return None
    phone = _PHONE_CLEAN_RE.sub("", raw)
    if phone.startswith("+"):
        phone = phone[1:]
    # A bare 10-digit local number defaults to India's country code — the
    # pilot city. International numbers arrive already prefixed.
    if len(phone) == 10:
        phone = "91" + phone
    return phone if 10 <= len(phone) <= 15 else None


def wants_sms(user):
    """True when this user opted in AND supplied a usable phone number."""
    return bool(user.get("sms_alerts")) and _phone_of(user) is not None


def send_alert(user, message):
    """Fire-and-forget SMS to an opted-in user. Never raises; never blocks
    the caller (network I/O happens on a daemon thread)."""
    if not wants_sms(user) or user.get("is_synthetic"):
        return False
    phone = _phone_of(user)
    text = f"FoodRescue: {message}"[:MAX_SMS_CHARS]

    if not BREVO_API_KEY:
        # Local development — no SMS provider configured.
        print(f"[sms] DEV (no BREVO_API_KEY) to +{phone}: {text}")
        return True

    def _post():
        try:
            import requests

            resp = requests.post(
                BREVO_SMS_URL,
                json={
                    "type": "transactional",
                    "sender": SMS_SENDER,
                    "recipient": phone,
                    "content": text,
                },
                headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
                timeout=10,
            )
            if resp.status_code not in (200, 201, 202):
                print(f"[sms] Brevo SMS failed: {resp.status_code} {resp.text[:150]}")
        except Exception as exc:  # an SMS hiccup must never break the flow
            print(f"[sms] send error: {exc}")

    threading.Thread(target=_post, daemon=True).start()
    return True
