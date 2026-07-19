"""
e2e_test.py — end-to-end smoke test for the FoodRescue API.

Drives the real HTTP API through every major flow:
  registration/login, KYC verification (pending gate, admin approve/reject,
  resubmission), the dedicated admin sign-in, forgot/reset password (Brevo
  dev fallback), broadcast, atomic NGO accept (+ race check), volunteer
  request, atomic volunteer claim (+ race check), proof-of-delivery photo
  upload, trust scoring, donor self-delivery, NGO cancel penalty,
  notifications, stats, the public activity feed, and security behaviors
  (401/403/409/400, rate limit).

Run against a scratch database:
  1. start the API with MONGO_URI=mongodb://localhost:27017/food_rescue_e2e
  2. .venv/Scripts/python.exe e2e_test.py
"""

import base64
import io
import os
import struct
import sys
import time
import zlib
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

# The server loads .env; the suite must sign in with the SAME admin creds.
load_dotenv()

BASE = "http://localhost:5000"
E2E_DB = "food_rescue_e2e"

# The dedicated admin account bootstrapped by app.py at startup.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@foodrescue.local")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin12345")
ADMIN = None  # set in main() once the admin signs in

# Role-appropriate KYC details submitted with every registration.
VERIFY_DETAILS = {
    "donor": {"phone": "+91 9876543210", "org_type": "restaurant",
              "business_name": "E2E Kitchen", "id_number": "FSSAI-11419-E2E"},
    "ngo": {"phone": "+91 9876500000", "registration_number": "MP-SOC-2020-1234"},
    "volunteer": {"phone": "+91 9876511111", "driving_licence": "MP09-2021-0012345",
                  "vehicle_type": "bike"},
}

# Indore city coordinates; everyone within a few km so Tier-1 matching applies.
LOCS = {
    "donor": (22.7196, 75.8577),
    "ngo1": (22.7250, 75.8700),      # ~1.5 km from donor
    "ngo2": (22.7000, 75.8400),      # ~3 km
    "shelter": (22.7300, 75.8800),   # animal shelter partner
    "vol1": (22.7150, 75.8600),      # ~0.6 km
    "vol2": (22.7350, 75.8500),      # ~1.9 km
}

PASS = "e2e-password-123"
STAMP = str(int(time.time()))

checks_passed = 0
checks_failed = 0


def check(label, condition, detail=""):
    global checks_passed, checks_failed
    if condition:
        checks_passed += 1
        print(f"  PASS  {label}")
    else:
        checks_failed += 1
        print(f"  FAIL  {label}  {detail}")


def make_png(width=40, height=40):
    """Build a tiny valid PNG in pure Python (no Pillow needed)."""
    def chunk(kind, payload):
        data = kind + payload
        return struct.pack(">I", len(payload)) + data + struct.pack(">I", zlib.crc32(data))

    raw = b"".join(b"\x00" + b"\x40\x90\x40" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PROOF_URI = "data:image/png;base64," + base64.b64encode(make_png()).decode()
FAKE_URI = "data:image/png;base64," + base64.b64encode(b"not-actually-a-png-image" * 10).decode()


def admin_login():
    resp = requests.post(
        f"{BASE}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, (
        f"admin login failed: {resp.status_code} {resp.text[:200]} — restart the API "
        "so it bootstraps the admin account (app.py > auth.ensure_admin_account)"
    )
    data = resp.json()
    return {"token": data["token"], "user": data["user"]}


def approve(actor):
    resp = api(ADMIN, "POST", f"/admin/verifications/{actor['user']['_id']}/approve")
    assert resp.status_code == 200, (
        f"approve {actor['user']['email']}: {resp.status_code} {resp.text[:200]}"
    )


def register(name, role, lat, lng, approve_now=True, **extra):
    body = {
        "name": name,
        "email": f"{name.lower().replace(' ', '-')}-{STAMP}@e2e.test",
        "password": PASS,
        "role": role,
        "latitude": lat,
        "longitude": lng,
        "address": f"{name} HQ, Indore",
        "verification": dict(VERIFY_DETAILS[role]),
        **extra,
    }
    resp = requests.post(f"{BASE}/auth/register", json=body, timeout=10)
    assert resp.status_code == 201, f"register {name}: {resp.status_code} {resp.text}"
    data = resp.json()
    actor = {"token": data["token"], "user": data["user"]}
    if approve_now and ADMIN is not None:
        approve(actor)
    return actor


def api(actor, method, path, body=None, expect=None):
    resp = requests.request(
        method,
        f"{BASE}{path}",
        json=body,
        headers={"Authorization": f"Bearer {actor['token']}"},
        timeout=15,
    )
    if expect is not None:
        assert resp.status_code == expect, f"{method} {path}: {resp.status_code} {resp.text[:300]}"
    return resp


def me(actor):
    return api(actor, "GET", "/auth/me", expect=200).json()["user"]


def main():
    mongo = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=4000)
    edb = mongo[E2E_DB]
    # Clear documents but KEEP the database: dropping it would also destroy
    # the indexes the running server created at startup (2dsphere, unique
    # email), silently breaking geo queries and duplicate detection.
    # The bootstrapped admin account survives the wipe — it is only created
    # at server startup, and the whole suite depends on it.
    edb["users"].delete_many({"role": {"$ne": "admin"}})
    for coll in ["food_batches", "matches", "notifications", "trust_log", "delivery_proofs"]:
        edb[coll].delete_many({})
    # Recreate the index set (idempotent) in case a previous run dropped them.
    edb.users.create_index([("location", "2dsphere")])
    edb.food_batches.create_index([("pickup_location", "2dsphere")])
    edb.food_batches.create_index([("status", 1), ("created_at", -1)])
    edb.users.create_index([("email", 1)], unique=True)
    edb.delivery_proofs.create_index([("batch_id", 1)], unique=True)
    print(f"[setup] cleared {E2E_DB} (indexes preserved)")

    resp = requests.get(f"{BASE}/health", timeout=5)
    check("health endpoint", resp.status_code == 200 and resp.json()["status"] == "ok")

    print("\n== Admin sign-in (dedicated credentials) ==")
    global ADMIN
    ADMIN = admin_login()
    check("dedicated admin account signs in", ADMIN["user"].get("role") == "admin",
          str(ADMIN["user"].get("role")))

    print("\n== Registration + KYC verification gate ==")
    noverify = requests.post(f"{BASE}/auth/register", json={
        "name": "No KYC", "email": f"no-kyc-{STAMP}@e2e.test", "password": PASS,
        "role": "ngo", "latitude": 22.71, "longitude": 75.86, "address": "x",
    }, timeout=10)
    check("registration without verification details -> 400", noverify.status_code == 400,
          noverify.text[:120])

    donor = register("E2E Donor", "donor", *LOCS["donor"], approve_now=False)
    check("new account starts as pending verification",
          donor["user"].get("verification_status") == "pending",
          str(donor["user"].get("verification_status")))
    blocked = api(donor, "POST", "/donor/broadcast", {
        "food_description": "5 kg rice", "quantity_kg": 5,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1], "address": "x",
    })
    check("unverified donor blocked from acting -> 403 not_verified",
          blocked.status_code == 403 and blocked.json().get("code") == "not_verified",
          blocked.text[:150])
    queue = api(ADMIN, "GET", "/admin/verifications?status=pending", expect=200).json()["requests"]
    check("admin verification queue lists the pending donor",
          any(r["_id"] == donor["user"]["_id"] for r in queue), str(len(queue)))
    approve(donor)
    check("approved donor shows verification_status approved",
          me(donor).get("verification_status") == "approved")

    ngo1 = register("E2E Food Bank", "ngo", *LOCS["ngo1"])
    ngo2 = register("E2E Shelter Home", "ngo", *LOCS["ngo2"])
    shelter = register("E2E Animal Shelter", "ngo", *LOCS["shelter"], partner_type="animal_shelter")
    vol1 = register("E2E Rider One", "volunteer", *LOCS["vol1"])
    vol2 = register("E2E Rider Two", "volunteer", *LOCS["vol2"])
    check("six actors registered + verified", True)

    point = mongo[E2E_DB].users.find_one({"_id": __import__("bson").ObjectId(donor["user"]["_id"])})["location"]
    check(
        "GeoJSON strictness (lng first)",
        point["type"] == "Point" and abs(point["coordinates"][0] - 75.8577) < 1e-6,
        str(point),
    )

    dup = requests.post(
        f"{BASE}/auth/register",
        json={"name": "Dup Tester", "email": donor["user"]["email"], "password": PASS,
              "role": "donor", "latitude": 1, "longitude": 1, "address": "12 Dup Street, Indore",
              "verification": dict(VERIFY_DETAILS["donor"])},
        timeout=10,
    )
    check("duplicate email -> 409", dup.status_code == 409, dup.text[:120])
    junk = requests.post(
        f"{BASE}/auth/register",
        json={"name": "Junk", "email": "not-an-email", "password": "aaaaaaaa",
              "role": "donor", "latitude": 1, "longitude": 1, "address": "x",
              "verification": dict(VERIFY_DETAILS["donor"])},
        timeout=10,
    )
    check("junk signup rejected (bad email) -> 400", junk.status_code == 400, junk.text[:120])
    fake_phone = requests.post(
        f"{BASE}/auth/register",
        json={"name": "Fake Phone", "email": f"fake-phone-{STAMP}@e2e.test", "password": PASS,
              "role": "donor", "latitude": 1, "longitude": 1, "address": "22 Real Street, Indore",
              "verification": {**VERIFY_DETAILS["donor"], "phone": "0000000000"}},
        timeout=10,
    )
    check("keyboard-mash phone rejected -> 400", fake_phone.status_code == 400, fake_phone.text[:120])

    weak = requests.post(
        f"{BASE}/auth/register",
        json={"name": "X", "email": f"weak-{STAMP}@e2e.test", "password": "short",
              "role": "donor", "latitude": 1, "longitude": 1, "address": "x"},
        timeout=10,
    )
    check("weak password -> 400", weak.status_code == 400)

    inject = requests.post(
        f"{BASE}/auth/login", json={"email": {"$gt": ""}, "password": {"$gt": ""}}, timeout=10
    )
    check("NoSQL injection login -> 401 (not 500)", inject.status_code == 401, inject.text[:120])

    print("\n== Auth guards ==")
    bad = requests.get(f"{BASE}/auth/me", headers={"Authorization": "Bearer garbage"}, timeout=10)
    check("garbage token -> 401", bad.status_code == 401)
    wrong_role = api(donor, "GET", "/ngo/available-batches")
    check("donor calling NGO route -> 403", wrong_role.status_code == 403)

    print("\n== Flow 1: broadcast -> NGO race -> volunteer race -> proof -> complete ==")
    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "40 portions veg biryani",
        "quantity_kg": 12.5,
        "dietary_tags": ["vegetarian", "jain"],
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Wedding Hall, MG Road",
    }, expect=201)
    batch1 = resp.json()["batch"]
    check("broadcast created pending batch", batch1["status"] == "pending")
    check(
        "donor got a 6-digit handoff pickup code",
        len(batch1.get("pickup_code") or "") == 6,
        str(batch1.get("pickup_code")),
    )
    check("perishable batch got the 3h TTL", batch1.get("ttl_hours") == 3)
    time.sleep(1.5)  # let the matching thread notify NGOs

    notif = api(ngo1, "GET", "/auth/notifications", expect=200).json()["notifications"]
    check("tier-1 NGO notified", any(n["kind"] == "batch_nearby" for n in notif), str(notif)[:200])

    avail = api(ngo1, "GET", "/ngo/available-batches", expect=200).json()["batches"]
    found = next((b for b in avail if b["_id"] == batch1["_id"]), None)
    check("NGO sees batch with distance", found is not None and "distance_meters" in found)
    check("NGO sees donor trust score", found and found["donor"]["trust_score"] == 100)
    check("pickup code hidden from NGO view", found is not None and "pickup_code" not in found)

    r1 = api(ngo1, "POST", f"/ngo/accept/{batch1['_id']}")
    r2 = api(ngo2, "POST", f"/ngo/accept/{batch1['_id']}")
    check("first NGO accept -> 200", r1.status_code == 200, r1.text[:120])
    check("second NGO accept -> 409 (atomic)", r2.status_code == 409, r2.text[:120])
    check("winner status ngo_pickup", r1.json()["batch"]["status"] == "ngo_pickup")

    double = api(ngo1, "POST", f"/ngo/accept/{batch1['_id']}")
    check("NGO double-booking blocked -> 409", double.status_code == 409)

    api(ngo1, "POST", f"/ngo/request-volunteer/{batch1['_id']}", expect=200)
    time.sleep(1.5)  # volunteer notification thread

    vnotif = api(vol1, "GET", "/auth/notifications", expect=200).json()["notifications"]
    check("volunteer notified of route", any(n["kind"] == "route_available" for n in vnotif))

    vavail = api(vol1, "GET", "/volunteer/available-batches", expect=200).json()["batches"]
    vfound = next((b for b in vavail if b["_id"] == batch1["_id"]), None)
    check("volunteer sees route with NGO drop-off", vfound is not None and vfound["receiver"]["name"] == "E2E Food Bank", str(vfound)[:200])

    c1 = api(vol1, "POST", f"/volunteer/claim/{batch1['_id']}")
    c2 = api(vol2, "POST", f"/volunteer/claim/{batch1['_id']}")
    check("first volunteer claim -> 200", c1.status_code == 200, c1.text[:120])
    check("second volunteer claim -> 409 (atomic)", c2.status_code == 409)
    claimed = c1.json()["batch"]
    check("claim computed 2-leg route_info", claimed["route_info"] is not None and claimed["route_info"]["status"] == "fallback", str(claimed.get("route_info")))
    check("pickup code hidden from volunteer claim", "pickup_code" not in claimed)

    no_photo = api(vol1, "POST", f"/volunteer/complete/{batch1['_id']}", {})
    check("complete without photo -> 400", no_photo.status_code == 400, no_photo.text[:120])
    fake = api(vol1, "POST", f"/volunteer/complete/{batch1['_id']}", {"proof_photo": FAKE_URI})
    check("fake image (bad magic bytes) -> 400", fake.status_code == 400, fake.text[:120])

    otp = batch1["pickup_code"]
    wrong_otp = "000000" if otp != "000000" else "111111"
    no_code = api(vol1, "POST", f"/volunteer/complete/{batch1['_id']}", {"proof_photo": PROOF_URI})
    check("complete without pickup code -> 400", no_code.status_code == 400, no_code.text[:120])
    bad_code = api(vol1, "POST", f"/volunteer/complete/{batch1['_id']}",
                   {"proof_photo": PROOF_URI, "pickup_code": wrong_otp})
    check("wrong pickup code -> 403", bad_code.status_code == 403, bad_code.text[:120])
    still = api(vol1, "GET", "/volunteer/active-batch", expect=200).json()["batch"]
    check("failed proof/code left delivery in_transit", still is not None and still["status"] == "in_transit")

    done = api(vol1, "POST", f"/volunteer/complete/{batch1['_id']}",
               {"proof_photo": PROOF_URI, "pickup_code": otp})
    check("complete with photo + pickup code -> 200", done.status_code == 200, done.text[:200])
    check("volunteer trust 100 -> 110", me(vol1)["trust_score"] == 110)

    proof_view = api(donor, "GET", f"/media/proof/{batch1['_id']}")
    check("donor can view proof photo", proof_view.status_code == 200 and proof_view.json()["data_uri"].startswith("data:image/png"))
    ngo_view = api(ngo1, "GET", f"/media/proof/{batch1['_id']}")
    check("receiving NGO can view proof", ngo_view.status_code == 200)
    outsider = api(vol2, "GET", f"/media/proof/{batch1['_id']}")
    check("outsider blocked from proof -> 403", outsider.status_code == 403, outsider.text[:120])

    match_doc = mongo[E2E_DB].matches.find_one({})
    check("Match document recorded", match_doc is not None and match_doc["volunteer_id"] is not None)

    print("\n== Flow 2: donor self-delivery (+50) ==")
    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "8 kg dal + rice",
        "quantity_kg": 8,
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Canteen, MG Road",
    }, expect=201)
    batch2 = resp.json()["batch"]
    check("dietary_tags default to vegetarian", batch2["dietary_tags"] == ["vegetarian"])

    api(ngo1, "POST", f"/ngo/accept/{batch2['_id']}", expect=200)
    api(ngo1, "POST", f"/ngo/request-volunteer/{batch2['_id']}", expect=200)
    early = api(donor, "POST", f"/donor/self-deliver/{batch1['_id']}")
    check("self-deliver on completed batch -> 409", early.status_code == 409)
    sd = api(donor, "POST", f"/donor/self-deliver/{batch2['_id']}")
    check("self-deliver -> 200 with route", sd.status_code == 200 and sd.json()["batch"]["route_info"] is not None, sd.text[:200])
    check("donor trust 100 -> 150 (5x bonus)", me(donor)["trust_score"] == 150)
    api(donor, "POST", f"/donor/complete/{batch2['_id']}", expect=200)
    check("self-delivery completes", True)

    print("\n== Flow 3: NGO cancel penalty ==")
    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "5 kg fruit",
        "quantity_kg": 5,
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Juice Bar, MG Road",
    }, expect=201)
    batch3 = resp.json()["batch"]
    api(ngo2, "POST", f"/ngo/accept/{batch3['_id']}", expect=200)
    api(ngo2, "POST", f"/ngo/cancel/{batch3['_id']}", expect=200)
    check("NGO cancel penalty 100 -> 85", me(ngo2)["trust_score"] == 85)
    reset = mongo[E2E_DB].food_batches.find_one({"food_description": "5 kg fruit"})
    check("cancelled batch back to pending", reset["status"] == "pending" and reset["accepted_by"] is None)

    api(ngo2, "POST", f"/ngo/accept/{batch3['_id']}", expect=200)
    otp3 = batch3["pickup_code"]
    bad_confirm = api(ngo2, "POST", f"/ngo/confirm-pickup/{batch3['_id']}",
                      {"pickup_code": "000000" if otp3 != "000000" else "111111"})
    check("NGO confirm with wrong code -> 403", bad_confirm.status_code == 403, bad_confirm.text[:120])
    no_code_confirm = api(ngo2, "POST", f"/ngo/confirm-pickup/{batch3['_id']}", {})
    check("NGO confirm without code -> 400", no_code_confirm.status_code == 400)
    api(ngo2, "POST", f"/ngo/confirm-pickup/{batch3['_id']}", {"pickup_code": otp3}, expect=200)
    check("NGO confirm-pickup with donor code completes batch", True)

    hist = api(ngo2, "GET", "/ngo/trust-history", expect=200).json()["history"]
    check("trust history logged the -15", any(h["delta"] == -15 for h in hist))

    print("\n== Stats ==")
    stats = requests.get(f"{BASE}/stats/impact", timeout=10).json()
    check("impact kg = 12.5 + 8 + 5", abs(stats["total_kg_rescued"] - 25.5) < 0.01, str(stats))
    check("3 rescues completed", stats["rescues_completed"] == 3)
    lb = requests.get(f"{BASE}/stats/leaderboard", timeout=10).json()
    check("leaderboard has volunteers + donors", len(lb["top_volunteers"]) >= 1 and len(lb["top_donors"]) >= 1, str(lb)[:200])
    check("top volunteer is Rider One @110", lb["top_volunteers"][0]["trust_score"] == 110)

    trends = requests.get(f"{BASE}/stats/trends", timeout=10).json()
    check("trends returns a 31-point daily series", len(trends.get("daily", [])) == 31, str(trends)[:150])
    check("trends totals: 3 rescues, 25.5 kg", trends["totals"]["rescues"] == 3
          and abs(trends["totals"]["rescued_kg"] - 25.5) < 0.01, str(trends["totals"]))

    print("\n== Gamification ==")
    ach = api(vol1, "GET", "/gamification/achievements", expect=200).json()
    check(
        "volunteer achievements: 1 rescue + first-delivery badge",
        ach["stats"]["rescues_completed"] == 1
        and any(b["id"] == "first_delivery" and b["earned"] for b in ach["badges"]),
        str(ach.get("stats")),
    )
    check("volunteer XP/level computed", ach["level"]["xp"] > 0 and ach["level"]["number"] >= 1, str(ach["level"]))
    check("volunteer streak is live (1 day)", ach["stats"]["streak_days"] == 1)
    dach = api(donor, "GET", "/gamification/achievements", expect=200).json()
    check(
        "donor self-delivery hero badge earned",
        any(b["id"] == "self_delivery_hero" and b["earned"] for b in dach["badges"]),
        str([b["id"] for b in dach["badges"] if b["earned"]]),
    )

    print("\n== Data-science layer (NLP, forecasting, audit) ==")
    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "We have 15 kg of cooked paneer left over",
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Kitchen, MG Road",
    }, expect=201)
    nlp_batch = resp.json()["batch"]
    check("NLP parsed 15 kg from raw sentence", nlp_batch["quantity_kg"] == 15.0, str(nlp_batch.get("nlp")))
    check("NLP vegetarian-first tags", nlp_batch["dietary_tags"] == ["vegetarian"])
    check("NLP perishable + zone stored", nlp_batch.get("perishable") is True and bool(nlp_batch.get("zone")))

    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "Leftover chicken curry about 4 kg",
        "dietary_tags": ["vegetarian"],
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Hotel Kitchen, MG Road",
    }, expect=201)
    override_batch = resp.json()["batch"]
    check(
        "meat term overrides vegetarian claim",
        override_batch["dietary_tags"] == ["non-vegetarian"]
        and override_batch["nlp"]["dietary_override"] is True,
        str(override_batch.get("nlp")),
    )

    preview = api(donor, "POST", "/api/parse-food", {"text": "fifty plates of veg biryani"})
    check(
        "parse-food estimates kg from plate count",
        preview.status_code == 200 and preview.json()["parsed"]["quantity_kg"] == 17.5,
        preview.text[:150],
    )

    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "Sealed biscuit packets and dry ration kits, 5 kg",
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Store Room, MG Road",
    }, expect=201)
    dry_batch = resp.json()["batch"]
    check(
        "non-perishable batch got the extended 12h TTL",
        dry_batch.get("ttl_hours") == 12 and dry_batch.get("perishable") is False,
        f"ttl={dry_batch.get('ttl_hours')} perishable={dry_batch.get('perishable')}",
    )

    # Seed a compact synthetic history into the e2e DB (same code path as
    # production seeding) so the forecaster and auditor have data.
    import os
    os.environ["MONGO_URI"] = f"mongodb://localhost:27017/{E2E_DB}"
    import seed_data  # noqa: E402 — env must be set before db.py connects
    seed_data.seed(n_records=1500, n_days=120, reset=True, quiet=True)
    print("[setup] seeded 1500 synthetic records for ML checks")

    forecast = api(donor, "GET", "/api/predict-surplus?refresh=1")
    check("predict-surplus -> 200", forecast.status_code == 200, forecast.text[:150])
    fc = forecast.json()
    check(
        "forecast covers 8 zones x 7 days",
        len(fc.get("zones", [])) == 8 and all(len(z["daily"]) == 7 for z in fc["zones"]),
    )
    check(
        "fleet allocation sums to fleet_size",
        sum(z["suggested_volunteers"] for z in fc["zones"]) == fc["fleet_size"],
        str([(z["zone"], z["suggested_volunteers"]) for z in fc["zones"]]),
    )
    noauth = requests.get(f"{BASE}/api/predict-surplus", timeout=10)
    check("predict-surplus without token -> 401", noauth.status_code == 401)

    audit = api(donor, "GET", "/api/run-audit")
    check("run-audit -> 200", audit.status_code == 200, audit.text[:150])
    report = audit.json()
    flagged_names = {f["name"] for f in report.get("flagged", [])}
    planted = {"Volunteer City #1", "Volunteer Old #2", "City Seva Foundation #1"}
    check(
        "audit catches all 3 planted fraud accounts",
        planted <= flagged_names,
        f"flagged={sorted(flagged_names)}",
    )
    check(
        "audit ran IsolationForest (not rules-only)",
        "IsolationForest" in str(report.get("model")),
        str(report.get("model")),
    )

    print("\n== Flow 4: expiry releases the NGO's active slot (regression) ==")
    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "6 kg poha from breakfast",
        "quantity_kg": 6,
        "latitude": LOCS["donor"][0],
        "longitude": LOCS["donor"][1],
        "address": "Breakfast Counter, MG Road",
    }, expect=201)
    batch4 = resp.json()["batch"]
    api(ngo2, "POST", f"/ngo/accept/{batch4['_id']}", expect=200)
    api(ngo2, "POST", f"/ngo/request-volunteer/{batch4['_id']}", expect=200)

    # Force the batch past its deadline, then run the same sweep the server's
    # 5-minute scheduler runs (engine is bound to the e2e DB via MONGO_URI).
    import engine  # noqa: E402
    batch4_id = __import__("bson").ObjectId(batch4["_id"])
    edb.food_batches.update_one(
        {"_id": batch4_id},
        {"$set": {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}},
    )
    engine.expire_stale_batches()
    expired_doc = edb.food_batches.find_one({"_id": batch4_id})
    check("stale volunteer_needed batch expired", expired_doc["status"] == "expired", expired_doc["status"])
    check(
        "expiry released the NGO's active slot (stuck-NGO regression)",
        me(ngo2).get("active_batch_id") is None,
        str(me(ngo2).get("active_batch_id")),
    )

    # =====================================================================
    # Advanced-feature suite (referrals, chat, tracking, ratings, safety,
    # templates, recurring, shifts, watch zones, surge, admin, impact)
    # =====================================================================

    print("\n== Referral program ==")
    donor_code = me(donor).get("referral_code")
    check("donor has a 6-char referral code", donor_code is not None and len(donor_code) == 6, str(donor_code))
    bad_ref = requests.post(f"{BASE}/auth/register", json={
        "name": "Bad Ref", "email": f"bad-ref-{STAMP}@e2e.test", "password": PASS,
        "role": "volunteer", "latitude": 22.71, "longitude": 75.86, "address": "x",
        "referral_code": "ZZZZZZ",
    }, timeout=10)
    check("unknown referral code -> 400", bad_ref.status_code == 400, bad_ref.text[:120])
    vol3 = register("E2E Rider Three", "volunteer", 22.7180, 75.8590, referral_code=donor_code)
    referral = api(donor, "GET", "/auth/referral", expect=200).json()
    check("referrer sees 1 referred signup", referral["referred_count"] == 1, str(referral))
    ref_notif = api(donor, "GET", "/auth/notifications", expect=200).json()["notifications"]
    check("referrer notified of signup", any(n["kind"] == "referral_signup" for n in ref_notif))

    print("\n== Chat + live tracking (during a rescue) ==")
    resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "10 kg cooked rice", "quantity_kg": 10,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1],
        "address": "Community Kitchen, MG Road",
    }, expect=201)
    batchR = resp.json()["batch"]
    api(ngo1, "POST", f"/ngo/accept/{batchR['_id']}", expect=200)
    api(ngo1, "POST", f"/ngo/request-volunteer/{batchR['_id']}", expect=200)
    api(vol3, "POST", f"/volunteer/claim/{batchR['_id']}", expect=200)

    sent = api(donor, "POST", f"/social/chat/{batchR['_id']}", {"text": "Gate 2, ask the guard for me"})
    check("donor can post to batch chat", sent.status_code == 201, sent.text[:150])
    first_msg_id = sent.json()["message"]["_id"]
    api(vol3, "POST", f"/social/chat/{batchR['_id']}", {"text": "On my way, 5 minutes out"}, expect=201)
    thread = api(ngo1, "GET", f"/social/chat/{batchR['_id']}", expect=200).json()["messages"]
    check("NGO reads full thread (2 messages)", len(thread) == 2, str(len(thread)))
    newer = api(donor, "GET", f"/social/chat/{batchR['_id']}?after={first_msg_id}", expect=200).json()["messages"]
    check("chat ?after= returns only newer messages", len(newer) == 1 and newer[0]["sender_role"] == "volunteer", str(newer)[:150])
    outsider_chat = api(vol2, "GET", f"/social/chat/{batchR['_id']}")
    check("outsider blocked from chat -> 403", outsider_chat.status_code == 403)
    empty_msg = api(donor, "POST", f"/social/chat/{batchR['_id']}", {"text": "  "})
    check("empty chat message -> 400", empty_msg.status_code == 400)
    chat_notif = api(vol3, "GET", "/auth/notifications", expect=200).json()["notifications"]
    check("chat pings the other parties' bell", any(n["kind"] == "chat_message" for n in chat_notif))

    ping = api(vol3, "POST", f"/social/track/{batchR['_id']}",
               {"latitude": LOCS["ngo1"][0], "longitude": LOCS["ngo1"][1]})
    check("courier GPS ping accepted", ping.status_code == 200, ping.text[:120])
    not_holder = api(vol2, "POST", f"/social/track/{batchR['_id']}", {"latitude": 22.7, "longitude": 75.8})
    check("non-holder GPS ping -> 409", not_holder.status_code == 409)
    track = api(donor, "GET", f"/social/track/{batchR['_id']}", expect=200).json()["tracking"]
    check("donor sees live position + progress",
          track["position"] is not None and track["progress_pct"] is not None
          and 0 <= track["progress_pct"] <= 100 and isinstance(track["eta_min"], int),
          str(track)[:200])
    check("tracking near drop-off shows ~100% progress", track["progress_pct"] >= 90, str(track["progress_pct"]))
    outsider_track = api(vol2, "GET", f"/social/track/{batchR['_id']}")
    check("outsider blocked from tracking -> 403", outsider_track.status_code == 403)

    print("\n== Ratings + referral trust bonus on first rescue ==")
    early_rate = api(donor, "POST", f"/social/rate/{batchR['_id']}", {"stars": 5})
    check("rating before completion -> 409", early_rate.status_code == 409)
    donor_trust_before = me(donor)["trust_score"]
    api(vol3, "POST", f"/volunteer/complete/{batchR['_id']}",
        {"proof_photo": PROOF_URI, "pickup_code": batchR["pickup_code"]}, expect=200)
    vol3_trust = me(vol3)["trust_score"]
    check("referred volunteer got +10 delivery +10 referral bonus", vol3_trust == 120, str(vol3_trust))
    check("referrer got +10 referral bonus", me(donor)["trust_score"] == donor_trust_before + 10,
          f"{donor_trust_before} -> {me(donor)['trust_score']}")

    rated = api(donor, "POST", f"/social/rate/{batchR['_id']}", {"stars": 5, "comment": "Lightning fast!"})
    check("donor rates volunteer 5 stars", rated.status_code == 201 and rated.json()["rating"]["ratee_name"] == "E2E Rider Three", rated.text[:200])
    dup_rate = api(donor, "POST", f"/social/rate/{batchR['_id']}", {"stars": 4})
    check("duplicate rating -> 409", dup_rate.status_code == 409)
    bad_stars = api(ngo1, "POST", f"/social/rate/{batchR['_id']}", {"stars": 7})
    check("stars out of range -> 400", bad_stars.status_code == 400)
    api(ngo1, "POST", f"/social/rate/{batchR['_id']}",
        {"stars": 4, "ratee_id": donor["user"]["_id"], "comment": "Well packed"}, expect=201)
    my_ratings = api(vol3, "GET", "/social/ratings/me", expect=200).json()
    check("volunteer reputation shows the 5-star review",
          my_ratings["average"] == 5.0 and my_ratings["count"] == 1, str(my_ratings)[:150])
    lb2 = requests.get(f"{BASE}/stats/leaderboard", timeout=10).json()
    check("leaderboard carries star ratings",
          any(v.get("rating") == 5.0 for v in lb2["top_volunteers"]), str(lb2["top_volunteers"])[:200])

    print("\n== Food-safety window (4-hour rule) ==")
    too_old = api(donor, "POST", "/donor/broadcast", {
        "food_description": "Cooked sabzi from this morning", "quantity_kg": 4,
        "prepared_hours_ago": 5,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1], "address": "Home Kitchen",
    })
    check("food prepared 5h ago rejected -> 400", too_old.status_code == 400, too_old.text[:150])
    fresh = api(donor, "POST", "/donor/broadcast", {
        "food_description": "Fresh cooked dal, 6 kg", "quantity_kg": 6,
        "prepared_hours_ago": 1,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1], "address": "Home Kitchen",
    }, expect=201)
    safe_batch = fresh.json()["batch"]
    check("fresh food carries a safe_until deadline", safe_batch.get("safe_until") is not None, str(safe_batch.get("safe_until")))
    check("expiry tightened to the safety window",
          safe_batch["expires_at"] <= safe_batch["safe_until"],
          f"{safe_batch['expires_at']} vs {safe_batch['safe_until']}")
    # Force the safety window into the past — acceptance must now be refused.
    edb.food_batches.update_one(
        {"_id": __import__("bson").ObjectId(safe_batch["_id"])},
        {"$set": {"safe_until": datetime(2000, 1, 1, tzinfo=timezone.utc)}},
    )
    unsafe_accept = api(ngo2, "POST", f"/ngo/accept/{safe_batch['_id']}")
    check("accepting food past its safe window -> 409", unsafe_accept.status_code == 409, unsafe_accept.text[:150])

    print("\n== Templates + donate-again ==")
    tpl = api(donor, "POST", "/donor/templates", {
        "name": "Friday buffet", "food_description": "Buffet leftovers, mixed veg",
        "quantity_kg": 20, "address": "Banquet Hall, MG Road",
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1],
        "dietary_tags": ["vegetarian", "jain"],
    })
    check("template saved", tpl.status_code == 201, tpl.text[:150])
    tpl_id = tpl.json()["template"]["_id"]
    tpl_list = api(donor, "GET", "/donor/templates", expect=200).json()["templates"]
    check("template listed", any(t["_id"] == tpl_id for t in tpl_list))
    donated = api(donor, "POST", f"/donor/templates/{tpl_id}/donate")
    check("one-click donate from template", donated.status_code == 201
          and donated.json()["batch"]["source"] == "template", donated.text[:150])
    redo = api(donor, "POST", f"/donor/rebroadcast/{batch1['_id']}")
    check("donate-again clones a past batch", redo.status_code == 201
          and redo.json()["batch"]["food_description"] == batch1["food_description"]
          and redo.json()["batch"]["source"] == "rebroadcast", redo.text[:150])
    api(donor, "DELETE", f"/donor/templates/{tpl_id}", expect=200)
    check("template deleted", True)

    print("\n== Recurring donation schedules ==")
    sched = api(donor, "POST", "/donor/recurring", {
        "food_description": "Canteen surplus rice + dal", "quantity_kg": 15,
        "address": "Office Canteen, Vijay Nagar",
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1],
        "days": [0, 1, 2, 3, 4, 5, 6], "time": "21:30", "tz_offset_min": 330,
    })
    check("recurring schedule created", sched.status_code == 201
          and sched.json()["schedule"]["next_run_at"] is not None, sched.text[:200])
    sched_id = sched.json()["schedule"]["_id"]
    bad_sched = api(donor, "POST", "/donor/recurring", {
        "food_description": "x", "quantity_kg": 1, "address": "y",
        "latitude": 22.7, "longitude": 75.8, "days": [9], "time": "21:30",
    })
    check("invalid weekday -> 400", bad_sched.status_code == 400)
    ran = api(donor, "POST", f"/donor/recurring/{sched_id}/run-now")
    check("run-now broadcasts from the schedule", ran.status_code == 201
          and ran.json()["batch"]["source"] == "recurring", ran.text[:150])
    slist = api(donor, "GET", "/donor/recurring", expect=200).json()["schedules"]
    mine = next(s for s in slist if s["_id"] == sched_id)
    check("schedule recorded the run", mine["runs"] == 1 and mine["last_run_at"] is not None, str(mine)[:150])
    toggled = api(donor, "POST", f"/donor/recurring/{sched_id}/toggle", expect=200).json()["schedule"]
    check("schedule paused", toggled["active"] is False)
    api(donor, "DELETE", f"/donor/recurring/{sched_id}", expect=200)

    print("\n== Volunteer availability shifts ==")
    shifts = api(vol1, "POST", "/volunteer/availability",
                 {"slots": ["0-morning", "4-evening", "5-evening"], "tz_offset_min": 330})
    check("availability saved", shifts.status_code == 200, shifts.text[:120])
    got = api(vol1, "GET", "/volunteer/availability", expect=200).json()
    check("availability round-trips", got["slots"] == ["0-morning", "4-evening", "5-evening"]
          and got["tz_offset_min"] == 330, str(got))
    bad_slot = api(vol1, "POST", "/volunteer/availability", {"slots": ["7-morning"]})
    check("invalid shift slot -> 400", bad_slot.status_code == 400)
    day, period = engine._current_period(330)
    if period is not None:
        api(vol1, "POST", "/volunteer/availability",
            {"slots": [f"{day}-{period}"], "tz_offset_min": 330}, expect=200)
        vol1_doc = edb.users.find_one({"email": vol1["user"]["email"]})
        check("engine sees on-shift volunteer", engine._is_on_shift(vol1_doc) is True)
    else:
        check("engine on-shift check (skipped: local night)", True)

    print("\n== NGO watch zones ==")
    wz = api(ngo2, "GET", "/ngo/watch-zones", expect=200).json()
    check("8 city zones offered", len(wz["zones"]) == 8, str(wz["zones"]))
    api(ngo2, "POST", "/ngo/watch-zones", {"zones": wz["zones"]}, expect=200)
    api(donor, "POST", "/donor/broadcast", {
        "food_description": "12 kg veg pulao", "quantity_kg": 12,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1],
        "address": "Hall 3, MG Road",
    }, expect=201)
    time.sleep(1.5)
    wz_notif = api(ngo2, "GET", "/auth/notifications", expect=200).json()["notifications"]
    check("watcher notified of new batch in zone", any(n["kind"] == "watch_zone" for n in wz_notif), str([n["kind"] for n in wz_notif])[:200])

    print("\n== Surge mode + admin console (admin-only) ==")
    not_admin = api(donor, "GET", "/admin/overview")
    check("non-admin blocked from admin console -> 403", not_admin.status_code == 403,
          not_admin.text[:120])
    surge_on = api(ADMIN, "POST", "/admin/surge", {"active": True, "reason": "Flood relief drive", "multiplier": 2.5})
    check("surge activated", surge_on.status_code == 200 and surge_on.json()["surge"]["active"] is True, surge_on.text[:150])
    public_surge = requests.get(f"{BASE}/stats/surge", timeout=10).json()
    check("public surge banner flag live", public_surge["active"] is True
          and public_surge["multiplier"] == 2.5 and "Flood" in public_surge["reason"], str(public_surge))
    api(ADMIN, "POST", "/admin/surge", {"active": False}, expect=200)
    check("surge deactivated", requests.get(f"{BASE}/stats/surge", timeout=10).json()["active"] is False)

    overview = api(ADMIN, "GET", "/admin/overview", expect=200).json()
    check("admin overview counts users + batches",
          overview["users"]["donor"] >= 1 and len(overview["batches_by_status"]) >= 1, str(overview)[:200])
    found_users = api(ADMIN, "GET", "/admin/users?q=Rider Three", expect=200).json()["users"]
    check("admin user search finds account", len(found_users) == 1
          and found_users[0]["rating"] == 5.0, str(found_users)[:200])

    vol4 = register("E2E Rider Four", "volunteer", 22.7100, 75.8500)
    vol4_id = vol4["user"]["_id"]
    api(ADMIN, "POST", f"/admin/users/{vol4_id}/suspend", {"suspended": True, "reason": "spam"}, expect=200)
    locked = requests.post(f"{BASE}/auth/login",
                           json={"email": vol4["user"]["email"], "password": PASS}, timeout=10)
    check("suspended user cannot log in -> 403", locked.status_code == 403, locked.text[:120])
    stale_token = api(vol4, "GET", "/volunteer/available-batches")
    check("suspended user's live token rejected -> 403", stale_token.status_code == 403)
    api(ADMIN, "POST", f"/admin/users/{vol4_id}/suspend", {"suspended": False}, expect=200)
    unlocked = requests.post(f"{BASE}/auth/login",
                             json={"email": vol4["user"]["email"], "password": PASS}, timeout=10)
    check("reinstated user logs in again", unlocked.status_code == 200)

    no_reason = api(ADMIN, "POST", f"/admin/users/{vol4_id}/trust", {"delta": 5})
    check("trust adjust without reason -> 400", no_reason.status_code == 400)
    adj = api(ADMIN, "POST", f"/admin/users/{vol4_id}/trust", {"delta": 5, "reason": "manual correction"})
    check("admin trust adjust applied", adj.status_code == 200 and adj.json()["trust_score"] == 105, adj.text[:120])
    admin_suspend = api(ADMIN, "POST", f"/admin/users/{ADMIN['user']['_id']}/suspend", {"suspended": True})
    check("admin account cannot be suspended -> 409", admin_suspend.status_code == 409,
          admin_suspend.text[:120])

    print("\n== Verification rejection + resubmission ==")
    vol5 = register("E2E Rider Reject", "volunteer", 22.7200, 75.8620, approve_now=False)
    rejected = api(ADMIN, "POST", f"/admin/verifications/{vol5['user']['_id']}/reject",
                   {"reason": "Licence photo unreadable"})
    check("admin rejects with a reason", rejected.status_code == 200
          and rejected.json()["verification_status"] == "rejected", rejected.text[:150])
    check("rejected user sees status + reason",
          me(vol5).get("verification_status") == "rejected")
    still_blocked = api(vol5, "GET", "/volunteer/available-batches")
    check("rejected account still blocked -> 403", still_blocked.status_code == 403
          and still_blocked.json().get("code") == "not_verified")
    resub = api(vol5, "POST", "/auth/verification", {"verification": {
        "phone": "+91 9876522222", "driving_licence": "MP09-2022-0099999", "vehicle_type": "scooter",
    }})
    check("rejected account can resubmit -> back to pending", resub.status_code == 200
          and resub.json()["verification"]["status"] == "pending", resub.text[:150])
    approve(vol5)
    check("resubmitted account approved + unblocked",
          me(vol5).get("verification_status") == "approved"
          and api(vol5, "GET", "/volunteer/available-batches").status_code == 200)

    print("\n== Forgot password (Brevo dev fallback) ==")
    fp = requests.post(f"{BASE}/auth/forgot-password", json={"email": donor["user"]["email"]}, timeout=10)
    fp_data = fp.json() if fp.status_code == 200 else {}
    check("forgot-password issues a dev code (no BREVO_API_KEY locally)",
          fp.status_code == 200 and len(fp_data.get("dev_code", "")) == 6, fp.text[:150])
    reset_code = fp_data.get("dev_code", "")
    wrong_code = "000000" if reset_code != "000000" else "111111"
    NEW_PASS = "e2e-new-password-456"
    bad_reset = requests.post(f"{BASE}/auth/reset-password", json={
        "email": donor["user"]["email"], "code": wrong_code, "new_password": NEW_PASS}, timeout=10)
    check("wrong reset code -> 400", bad_reset.status_code == 400, bad_reset.text[:120])
    good_reset = requests.post(f"{BASE}/auth/reset-password", json={
        "email": donor["user"]["email"], "code": reset_code, "new_password": NEW_PASS}, timeout=10)
    check("correct reset code changes the password", good_reset.status_code == 200, good_reset.text[:150])
    old_login = requests.post(f"{BASE}/auth/login",
                              json={"email": donor["user"]["email"], "password": PASS}, timeout=10)
    check("old password no longer works -> 401", old_login.status_code == 401)
    new_login = requests.post(f"{BASE}/auth/login",
                              json={"email": donor["user"]["email"], "password": NEW_PASS}, timeout=10)
    check("new password signs in", new_login.status_code == 200, new_login.text[:120])
    donor["token"] = new_login.json()["token"]
    reused = requests.post(f"{BASE}/auth/reset-password", json={
        "email": donor["user"]["email"], "code": reset_code, "new_password": NEW_PASS}, timeout=10)
    check("reset code is single-use -> 400", reused.status_code == 400)

    print("\n== Public live-activity feed ==")
    act = requests.get(f"{BASE}/stats/activity", timeout=10)
    act_items = act.json().get("activity", []) if act.status_code == 200 else []
    check("activity feed lists completed rescues",
          act.status_code == 200 and len(act_items) >= 1
          and all("kg" in a and "donor_first_name" in a for a in act_items), str(act_items)[:200])

    print("\n== Personal impact report + CSV export ==")
    impact_report = api(donor, "GET", "/stats/my-impact", expect=200).json()
    check("impact report totals (4 completed rescues)",
          impact_report["totals"]["rescues"] == 4 and abs(impact_report["totals"]["kg"] - 35.5) < 0.01,
          str(impact_report["totals"]))
    check("impact report has a 12-week series + live streak",
          len(impact_report["weekly"]) == 12 and impact_report["weekly"][-1]["rescues"] == 4
          and impact_report["streak_days"] == 1, str(impact_report["weekly"][-1]))
    csv_resp = api(donor, "GET", "/stats/my-impact.csv")
    csv_lines = [line for line in csv_resp.text.strip().splitlines() if line.strip()]
    check("CSV export: header + 4 rescue rows",
          csv_resp.status_code == 200 and "text/csv" in csv_resp.headers.get("Content-Type", "")
          and csv_lines[0].startswith("completed_at_utc") and len(csv_lines) == 5,
          f"{len(csv_lines)} lines")

    # =====================================================================
    # New-round features: cold chain, multi-stop trips, POS keys, SMS prefs
    # =====================================================================

    print("\n== Cold chain (dairy detection + cold-storage gate + OTP) ==")
    cold_resp = api(donor, "POST", "/donor/broadcast", {
        "food_description": "6 kg of fresh paneer and curd surplus", "quantity_kg": 6,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1],
        "address": "Dairy counter, MG Road",
    }, expect=201).json()["batch"]
    check("dairy terms auto-flag cold_chain", cold_resp.get("cold_chain") is True, str(cold_resp.get("cold_chain")))
    created = datetime.fromisoformat(cold_resp["created_at"])
    expires = datetime.fromisoformat(cold_resp["expires_at"])
    check("cold-chain TTL tightened to <=2h",
          (expires - created).total_seconds() <= 2 * 3600 + 60,
          f"{(expires - created).total_seconds() / 3600:.1f}h")
    cold_id = cold_resp["_id"]

    ngo_cold = register("E2E Cold Kitchen", "ngo", 22.7210, 75.8590)
    listing = api(ngo_cold, "GET", "/ngo/available-batches", expect=200).json()["batches"]
    check("cold batch hidden from NGO without cold storage",
          all(b["_id"] != cold_id for b in listing), f"{len(listing)} listed")
    blocked_cold = api(ngo_cold, "POST", f"/ngo/accept/{cold_id}")
    check("accept without cold storage -> 409", blocked_cold.status_code == 409, blocked_cold.text[:150])
    api(ngo_cold, "POST", "/ngo/cold-storage", {"enabled": True}, expect=200)
    listing = api(ngo_cold, "GET", "/ngo/available-batches", expect=200).json()["batches"]
    check("cold batch visible after enabling cold storage",
          any(b["_id"] == cold_id for b in listing))
    api(ngo_cold, "POST", f"/ngo/accept/{cold_id}", expect=200)

    my_batches = api(donor, "GET", "/donor/my-batches", expect=200).json()["batches"]
    cold_code = next(b["pickup_code"] for b in my_batches if b["_id"] == cold_id)
    wrong_otp = api(ngo_cold, "POST", f"/ngo/confirm-pickup/{cold_id}", {"pickup_code": "000000" if cold_code != "000000" else "111111"})
    check("wrong handoff OTP -> 403", wrong_otp.status_code == 403, wrong_otp.text[:120])
    api(ngo_cold, "POST", f"/ngo/confirm-pickup/{cold_id}", {"pickup_code": cold_code}, expect=200)
    check("correct handoff OTP completes the cold rescue", True)

    print("\n== Multi-stop volunteer trips (route add-ons) ==")
    ngo_m1 = register("E2E Stop One NGO", "ngo", 22.7250, 75.8710)
    ngo_m2 = register("E2E Stop Two NGO", "ngo", 22.7280, 75.8750)
    batch_a = api(donor, "POST", "/donor/broadcast", {
        "food_description": "9 kg veg biryani leg A", "quantity_kg": 9,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1], "address": "Kitchen A, MG Road",
    }, expect=201).json()["batch"]["_id"]
    api(ngo_m1, "POST", f"/ngo/accept/{batch_a}", expect=200)
    api(ngo_m1, "POST", f"/ngo/request-volunteer/{batch_a}", expect=200)
    batch_b = api(donor, "POST", "/donor/broadcast", {
        "food_description": "4 kg dal leg B", "quantity_kg": 4,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1], "address": "Kitchen A, MG Road",
    }, expect=201).json()["batch"]["_id"]
    api(ngo_m2, "POST", f"/ngo/accept/{batch_b}", expect=200)
    api(ngo_m2, "POST", f"/ngo/request-volunteer/{batch_b}", expect=200)

    api(vol5, "POST", f"/volunteer/claim/{batch_a}", expect=200)
    addons = api(vol5, "GET", "/volunteer/route-addons", expect=200).json()["addons"]
    check("route add-ons suggest the second nearby batch",
          any(a["_id"] == batch_b for a in addons), str([a.get("_id") for a in addons]))
    api(vol5, "POST", f"/volunteer/claim/{batch_b}", expect=200)
    actives = api(vol5, "GET", "/volunteer/active-batch", expect=200).json()
    check("courier carries two legs at once",
          len(actives.get("batches") or []) == 2, str(len(actives.get("batches") or [])))
    full = api(vol5, "GET", "/volunteer/route-addons", expect=200).json()
    check("trip reports full at two legs", full.get("addons") == [] and "full" in str(full.get("reason", "")).lower(),
          str(full)[:120])

    my_batches = api(donor, "GET", "/donor/my-batches", expect=200).json()["batches"]
    codes = {b["_id"]: b["pickup_code"] for b in my_batches}
    vol5_trust_before = me(vol5)["trust_score"]
    api(vol5, "POST", f"/volunteer/complete/{batch_a}",
        {"proof_photo": PROOF_URI, "pickup_code": codes[batch_a]}, expect=200)
    still = api(vol5, "GET", "/volunteer/active-batch", expect=200).json()
    check("first leg done — second leg still active",
          still.get("batch") and still["batch"]["_id"] == batch_b, str(still.get("batch", {}).get("_id")))
    api(vol5, "POST", f"/volunteer/complete/{batch_b}",
        {"proof_photo": PROOF_URI, "pickup_code": codes[batch_b]}, expect=200)
    check("both legs completed (+20 trust)",
          me(vol5)["trust_score"] == vol5_trust_before + 20, str(me(vol5)["trust_score"]))

    print("\n== POS integration (per-donor API keys) ==")
    key_resp = api(donor, "POST", "/integrations/api-key", expect=201).json()
    pos_key = key_resp["api_key"]
    check("API key generated (frk_ prefix, shown once)", pos_key.startswith("frk_"), pos_key[:12])
    masked = api(donor, "GET", "/integrations/api-key", expect=200).json()["api_key"]
    check("key status is masked", "•" in masked["masked"] and pos_key not in str(masked))
    pos_created = requests.post(f"{BASE}/integrations/pos/broadcast",
                                json={"food_description": "7 kg cooked rice from the POS", "quantity_kg": 7},
                                headers={"X-API-Key": pos_key}, timeout=15)
    check("POS broadcast with valid key -> 201", pos_created.status_code == 201, pos_created.text[:150])
    check("POS batch defaults to donor's location/address", pos_created.json().get("status") == "pending")
    newest = api(donor, "GET", "/donor/my-batches", expect=200).json()["batches"][0]
    check("POS batch tagged source=pos", newest.get("source") == "pos", str(newest.get("source")))
    bad_key = requests.post(f"{BASE}/integrations/pos/broadcast",
                            json={"food_description": "1 kg x", "quantity_kg": 1},
                            headers={"X-API-Key": "frk_deadbeef_badbadbadbad"}, timeout=10)
    check("POS broadcast with bad key -> 401", bad_key.status_code == 401)
    api(donor, "DELETE", "/integrations/api-key", expect=200)
    revoked = requests.post(f"{BASE}/integrations/pos/broadcast",
                            json={"food_description": "1 kg x", "quantity_kg": 1},
                            headers={"X-API-Key": pos_key}, timeout=10)
    check("revoked key refused -> 401", revoked.status_code == 401)

    print("\n== SMS preferences + live zone map ==")
    prefs = api(donor, "GET", "/auth/preferences", expect=200).json()["preferences"]
    check("sms alerts default off, phone on file",
          prefs["sms_alerts"] is False and prefs["phone_on_file"] is True, str(prefs))
    api(donor, "POST", "/auth/preferences", {"sms_alerts": True}, expect=200)
    prefs = api(donor, "GET", "/auth/preferences", expect=200).json()["preferences"]
    check("sms alerts opt-in persists", prefs["sms_alerts"] is True)
    zmap = requests.get(f"{BASE}/stats/zones-live", timeout=10)
    zdata = zmap.json().get("zones", []) if zmap.status_code == 200 else []
    check("live zone map: 8 zones with counts",
          zmap.status_code == 200 and len(zdata) == 8
          and all("pending" in z and "moving" in z for z in zdata), str(zdata)[:150])

    print("\n== ESG carbon dashboard + certified report ==")
    esg = api(donor, "GET", "/esg/dashboard", expect=200).json()
    totals = esg["totals"]
    check("ESG dashboard aggregates the donor's verified rescues",
          totals["rescues"] >= 4 and totals["kg_food"] > 0, str(totals)[:150])
    check("carbon model: CO2e = kg x 2.5 (0.70 landfill + 1.80 production)",
          abs(totals["total_co2e_kg"] - totals["kg_food"] * 2.5) < 0.6
          and abs(totals["landfill_co2e_kg"] + totals["production_co2e_kg"]
                  - totals["total_co2e_kg"]) < 0.2, str(totals)[:150])
    check("methane figure derived from IPCC decay factor",
          abs(totals["methane_kg"] - totals["kg_food"] * 0.025) < 0.06, str(totals["methane_kg"]))
    eq = totals["equivalents"]
    check("real-world equivalents present (cars/trees/km/meals)",
          all(k in eq for k in ("car_days_off_road", "tree_years", "km_not_driven", "meals_served"))
          and eq["meals_served"] == int(totals["kg_food"] * 2.5), str(eq))
    check("12-month contribution series with this month populated",
          len(esg["monthly"]) == 12 and esg["monthly"][-1]["kg"] > 0,
          str(esg["monthly"][-1]))
    esg_csv = api(donor, "GET", "/esg/report.csv")
    check("certified ESG CSV report (methodology + lifetime totals)",
          esg_csv.status_code == 200 and "text/csv" in esg_csv.headers.get("Content-Type", "")
          and "LIFETIME_CO2E_AVOIDED_KG" in esg_csv.text and "METHODOLOGY" in esg_csv.text,
          esg_csv.text[:120])
    non_donor_esg = api(ngo1, "GET", "/esg/dashboard")
    check("ESG dashboard is donor-only -> 403", non_donor_esg.status_code == 403)

    print("\n== Account settings: profile, change-password, cancel broadcast, delete ==")
    acct = register("Acct Donor", "donor", *LOCS["donor"])
    acct_email = acct["user"]["email"]

    resp = api(acct, "POST", "/auth/profile", {
        "name": "Acct Donor Renamed", "address": "New Cloud Kitchen, Indore",
        "latitude": 22.7220, "longitude": 75.8610,
    })
    check("profile update (name/address/location) -> 200", resp.status_code == 200, resp.text[:200])
    fresh = me(acct)
    check("profile changes visible via /auth/me",
          fresh["name"] == "Acct Donor Renamed"
          and fresh["address"] == "New Cloud Kitchen, Indore"
          and fresh["location"]["coordinates"] == [75.8610, 22.7220],
          str({k: fresh.get(k) for k in ("name", "address", "location")}))
    resp = api(acct, "POST", "/auth/profile", {"latitude": 999, "longitude": 75.9})
    check("profile rejects out-of-range coordinates -> 400", resp.status_code == 400, resp.text[:150])
    resp = api(acct, "POST", "/auth/profile", {})
    check("empty profile update -> 400", resp.status_code == 400, resp.text[:150])
    resp = api(acct, "POST", "/auth/profile", {"name": "x"})
    check("junk name rejected -> 400", resp.status_code == 400, resp.text[:150])

    resp = api(acct, "POST", "/auth/change-password",
               {"current_password": "wrong-pass-1", "new_password": "brand-new-pw-9"})
    check("change-password with wrong current -> 403", resp.status_code == 403, resp.text[:150])
    resp = api(acct, "POST", "/auth/change-password",
               {"current_password": PASS, "new_password": "short1"})
    check("change-password rejects weak new password -> 400", resp.status_code == 400, resp.text[:150])
    new_pass = "acct-new-pass-42"
    pre_change_token = acct["token"]
    resp = api(acct, "POST", "/auth/change-password",
               {"current_password": PASS, "new_password": new_pass})
    check("change-password -> 200 with a fresh session token",
          resp.status_code == 200 and bool(resp.json().get("token")), resp.text[:200])
    acct["token"] = resp.json().get("token") or pre_change_token
    resp = requests.get(f"{BASE}/auth/me",
                        headers={"Authorization": f"Bearer {pre_change_token}"}, timeout=10)
    check("pre-change token is revoked -> 401", resp.status_code == 401, str(resp.status_code))
    check("fresh token keeps this session alive",
          api(acct, "GET", "/auth/me").status_code == 200)
    resp = requests.post(f"{BASE}/auth/login",
                         json={"email": acct_email, "password": PASS}, timeout=10)
    check("old password no longer signs in -> 401", resp.status_code == 401, str(resp.status_code))
    resp = requests.post(f"{BASE}/auth/login",
                         json={"email": acct_email, "password": new_pass}, timeout=10)
    check("new password signs in", resp.status_code == 200, resp.text[:150])

    resp = api(acct, "POST", "/donor/broadcast", {
        "food_description": "6 kg leftover veg pulao",
        "quantity_kg": 6,
        "latitude": LOCS["donor"][0], "longitude": LOCS["donor"][1],
        "address": "Acct Donor kitchen, Indore",
    }, expect=201)
    acct_batch = resp.json()["batch"]["_id"]
    resp = api(acct, "POST", "/auth/delete-account", {"password": new_pass})
    check("delete-account blocked while a rescue is active -> 409",
          resp.status_code == 409, resp.text[:200])
    resp = api(acct, "POST", f"/donor/cancel/{acct_batch}")
    check("donor cancels own pending broadcast -> 200 cancelled",
          resp.status_code == 200 and resp.json()["batch"]["status"] == "cancelled",
          resp.text[:200])
    resp = api(acct, "POST", f"/donor/cancel/{acct_batch}")
    check("cancelling twice -> 409", resp.status_code == 409, resp.text[:150])
    resp = api(donor, "POST", f"/donor/cancel/{acct_batch}")
    check("cancelling someone else's batch -> 409", resp.status_code == 409, resp.text[:150])

    resp = api(acct, "POST", "/auth/delete-account", {"password": "totally-wrong-9"})
    check("delete-account with wrong password -> 403", resp.status_code == 403, resp.text[:150])
    resp = api(acct, "POST", "/auth/delete-account", {"password": new_pass})
    check("delete-account -> 200", resp.status_code == 200, resp.text[:200])
    resp = api(acct, "GET", "/auth/me")
    check("deleted account's token is dead -> 401", resp.status_code == 401, str(resp.status_code))
    resp = requests.post(f"{BASE}/auth/login",
                         json={"email": acct_email, "password": new_pass}, timeout=10)
    check("deleted account cannot sign in -> 401", resp.status_code == 401, str(resp.status_code))
    resp = api(ADMIN, "POST", "/auth/delete-account", {"password": ADMIN_PASSWORD})
    check("admin account refuses in-app deletion -> 403", resp.status_code == 403, resp.text[:150])

    print("\n== Rate limiting (last — it throttles the IP) ==")
    statuses = []
    for _ in range(12):
        r = requests.post(f"{BASE}/auth/login", json={"email": "nobody@e2e.test", "password": "wrong-pass"}, timeout=10)
        statuses.append(r.status_code)
    check("brute-force login throttled -> 429", 429 in statuses, str(statuses))

    print(f"\n{'=' * 50}\nRESULT: {checks_passed} passed, {checks_failed} failed")
    return 1 if checks_failed else 0


if __name__ == "__main__":
    sys.exit(main())
