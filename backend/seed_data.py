"""
seed_data.py — Historical Data Simulator (Deliverable 1).

Generates ~2 years of synthetic vegetarian-first food-rescue history and
inserts it directly into the existing MongoDB collections (users,
food_batches, matches) so the ML models have data to train on:

  * a synthetic population of donors, NGOs and volunteers spread over the
    shared city-zone grid (zones.py),
  * 10,000 food broadcasts whose daily volume follows weekday, wedding-season,
    monsoon, festival and weather effects (pandas builds the intensity model,
    numpy samples from it),
  * realistic pickup timelines (accept -> claim -> travel -> complete) and
    Match documents for completed rescues,
  * 3 planted fraud accounts (2 volunteers, 1 NGO) whose completions are
    statistically impossible — ground truth for fraud_detection.py.

Every synthetic document carries is_synthetic: True. The live platform
ignores them everywhere it matters: the matching engine never notifies them
and the public stats/leaderboard exclude them. Only the ML layer reads them.

Usage (from backend/, venv active):
    python seed_data.py                      # seed 10,000 records over 730 days
    python seed_data.py --reset              # wipe old synthetic data, reseed
    python seed_data.py --records 2000 --days 365
"""

import argparse
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from werkzeug.security import generate_password_hash

import db
from zones import get_zones, haversine_km

RNG_SEED = 42

N_DONORS = 60
N_NGOS = 15
N_VOLUNTEERS = 40
N_FRAUD_VOLUNTEERS = 2
N_FRAUD_NGOS = 1

BATCH_TTL_HOURS = 3

# Mon..Sun — weekends are wedding/event heavy.
WEEKDAY_FACTOR = [0.80, 0.85, 0.90, 1.00, 1.35, 1.60, 1.30]

# (month, day) -> surplus spike factor (approx. recurring Indian festivals).
FESTIVAL_FACTOR = {
    (1, 1): 1.5, (1, 14): 1.4, (3, 10): 1.8, (4, 10): 1.5, (8, 19): 1.4,
    (8, 26): 1.5, (9, 7): 1.6, (10, 2): 1.4, (10, 12): 1.7, (10, 31): 1.8,
    (11, 1): 2.2, (11, 2): 2.0, (12, 25): 1.4, (12, 31): 1.8,
}

# month -> (min_temp, max_temp, rain_probability) — central-India climate.
MONTH_CLIMATE = {
    1: (8, 25, 0.05), 2: (11, 28, 0.05), 3: (16, 34, 0.05), 4: (22, 40, 0.05),
    5: (26, 43, 0.08), 6: (26, 38, 0.35), 7: (24, 32, 0.65), 8: (23, 31, 0.65),
    9: (23, 32, 0.45), 10: (18, 33, 0.15), 11: (13, 30, 0.05), 12: (9, 26, 0.05),
}


def _season_factor(month):
    if month in (11, 12, 1, 2):
        return 1.35  # wedding season
    if month in (6, 7, 8, 9):
        return 0.85  # monsoon slowdown
    return 1.0


# donor_type -> (name template, mean quantity kg, broadcast hours)
DONOR_PROFILES = {
    "restaurant":       ("Hotel {} Palace",        9.0,  [14, 15, 21, 22, 23]),
    "caterer":          ("Shree {} Caterers",      25.0, [15, 16, 17, 22]),
    "wedding_hall":     ("{} Wedding Garden",      45.0, [15, 16, 22, 23]),
    "hostel_mess":      ("{} Hostel Mess",         12.0, [14, 15, 20, 21]),
    "temple":           ("{} Temple Annakshetra",  20.0, [12, 13, 19, 20]),
    "corporate_canteen": ("{} Office Canteen",     10.0, [14, 15, 16]),
}

# (description template, qty scale, dietary) — vegetarian-first menu.
VEG_MENU = [
    ("Leftover paneer butter masala and naan from dinner service", 1.0),
    ("Dal tadka with steamed rice, freshly cooked", 1.1),
    ("Veg biryani from an event, still hot", 1.4),
    ("Chole and puri from morning service", 0.9),
    ("Mixed sabzi with rotis, cooked today", 1.0),
    ("Khichdi prepared for lunch, surplus remains", 1.2),
    ("Poha and jalebi from breakfast counter", 0.7),
    ("Idli and sambar batch, unserved", 0.8),
    ("Full thali meals packed and ready", 1.3),
    ("Fresh fruits and salad trays", 0.6),
    ("Bread and vegetable sandwiches", 0.5),
    ("Kheer and halwa from prasad distribution", 0.7),
    ("Sealed biscuit packets and dry ration kits", 0.5),
    ("Mithai boxes from a celebration", 0.6),
]
NONVEG_MENU = [
    ("Chicken curry surplus from dinner buffet", 1.2, ["non-vegetarian"]),
    ("Egg bhurji and pav from evening stall", 0.6, ["eggitarian"]),
    ("Fish curry batch from lunch service", 1.0, ["non-vegetarian"]),
]
NONVEG_SHARE = 0.10  # vegetarian-first: 90% of history is pure veg


def _jitter_location(rng, zone, sigma_km=0.9):
    east_km, north_km = rng.normal(0.0, sigma_km, 2)
    lat = zone["lat"] + north_km / 111.32
    lng = zone["lng"] + east_km / (111.32 * np.cos(np.radians(zone["lat"])))
    return db.geo_point(round(float(lng), 6), round(float(lat), 6))


def _build_population(rng, history_start):
    """Synthetic donors/NGOs/volunteers spread over the zone grid."""
    zones = get_zones()
    shared_hash = generate_password_hash(secrets.token_hex(16))  # unguessable

    def base_user(index, role, zone):
        return {
            "email": f"synth-{role}-{index}@seed.foodrescue.local",
            "password_hash": shared_hash,
            "role": role,
            "location": _jitter_location(rng, zone),
            "address": f"{zone['name']}, Rescue City",
            "trust_score": 100,
            "created_at": history_start,
            "active_batch_id": None,
            "is_synthetic": True,
            "zone": zone["name"],
        }

    donors, ngos, volunteers = [], [], []
    donor_types = list(DONOR_PROFILES)
    for i in range(N_DONORS):
        zone = zones[i % len(zones)]
        donor_type = donor_types[i % len(donor_types)]
        doc = base_user(i, "donor", zone)
        doc["name"] = DONOR_PROFILES[donor_type][0].format(zone["name"].split()[0]) + f" #{i + 1}"
        doc["donor_type"] = donor_type
        donors.append(doc)

    for i in range(N_NGOS):
        zone = zones[i % len(zones)]
        doc = base_user(i, "ngo", zone)
        doc["name"] = f"{zone['name'].split()[0]} Seva Foundation #{i + 1}"
        doc["partner_type"] = "animal_shelter" if i >= N_NGOS - 2 else "food_charity"
        doc["accepts_non_veg"] = i % 3 == 0
        ngos.append(doc)

    for i in range(N_VOLUNTEERS):
        zone = zones[i % len(zones)]
        doc = base_user(i, "volunteer", zone)
        doc["name"] = f"Volunteer {zone['name'].split()[0]} #{i + 1}"
        volunteers.append(doc)

    # Plant fraud accounts: their timelines will be physically impossible.
    for doc in volunteers[:N_FRAUD_VOLUNTEERS]:
        doc["seed_fraud_profile"] = "impossible_speed_bursts"
    for doc in ngos[:N_FRAUD_NGOS]:
        doc["seed_fraud_profile"] = "instant_pickup_confirmations"

    return donors, ngos, volunteers


def _daily_weather(rng, dates):
    """One weather record per calendar day."""
    weather = {}
    for date in dates:
        t_min, t_max, rain_p = MONTH_CLIMATE[date.month]
        temp = round(float(rng.uniform(t_min, t_max)), 1)
        raining = bool(rng.random() < rain_p)
        rain_mm = round(float(rng.exponential(14.0)) + 1.0, 1) if raining else 0.0
        if raining:
            condition = "rain"
        elif temp >= 40:
            condition = "heatwave"
        elif temp <= 12:
            condition = "cold"
        else:
            condition = "clear"
        weather[date] = {"condition": condition, "temp_c": temp, "rain_mm": rain_mm}
    return weather


def _intensity_frame(dates, weather):
    """pandas frame of expected surplus intensity per (date, zone) — the
    ground-truth signal the forecaster is later asked to rediscover."""
    rows = []
    for date in dates:
        day_factor = (
            WEEKDAY_FACTOR[date.weekday()]
            * _season_factor(date.month)
            * FESTIVAL_FACTOR.get((date.month, date.day), 1.0)
            * (0.90 if weather[date]["condition"] == "rain" else 1.0)
        )
        for zone in get_zones():
            rows.append({"date": date, "zone": zone["name"], "intensity": zone["weight"] * day_factor})
    return pd.DataFrame(rows)


def generate(n_records, n_days, rng):
    """Build all synthetic documents in memory. Returns (users, batches, matches)."""
    history_end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = history_end - timedelta(days=n_days)
    dates = [
        (history_start + timedelta(days=offset)).date()
        for offset in range(n_days)
    ]

    donors, ngos, volunteers = _build_population(rng, history_start)
    for doc in donors + ngos + volunteers:
        doc["_id"] = db.ObjectId()

    weather = _daily_weather(rng, dates)
    frame = _intensity_frame(dates, weather)
    counts = rng.multinomial(n_records, frame["intensity"] / frame["intensity"].sum())

    donors_by_zone = {}
    for donor in donors:
        donors_by_zone.setdefault(donor["zone"], []).append(donor)
    veg_ngos = [n for n in ngos if n["partner_type"] == "food_charity"]
    nonveg_ngos = [n for n in ngos if n.get("accepts_non_veg")]

    def _weighted_pick(pool):
        weights = [3.0 if "seed_fraud_profile" in doc else 1.0 for doc in pool]
        return random.choices(pool, weights=weights, k=1)[0]

    batches, match_docs = [], []
    completions = {}          # user_id -> completed count (drives trust)
    fraud_event_index = {}    # fraud volunteer id -> running event counter

    for (_, row), count in zip(frame.iterrows(), counts):
        for _ in range(int(count)):
            date, zone_name = row["date"], row["zone"]
            day_weather = weather[date]
            raining = day_weather["condition"] == "rain"

            donor = random.choice(donors_by_zone.get(zone_name) or donors)
            profile = DONOR_PROFILES[donor["donor_type"]]

            if donor["donor_type"] == "restaurant" and random.random() < NONVEG_SHARE:
                description, qty_scale, dietary_tags = random.choice(NONVEG_MENU)
            else:
                description, qty_scale = random.choice(VEG_MENU)
                dietary_tags = ["vegetarian"]

            quantity_kg = round(
                float(np.clip(rng.lognormal(np.log(profile[1] * qty_scale), 0.45), 2.0, 220.0)), 1
            )

            hour = random.choice(profile[2])
            created_at = datetime(
                date.year, date.month, date.day, hour, random.randint(0, 59),
                tzinfo=timezone.utc,
            )

            batch = {
                "_id": db.ObjectId(),
                "donor_id": donor["_id"],
                "food_description": description,
                "quantity_kg": quantity_kg,
                "dietary_tags": dietary_tags,
                "pickup_location": donor["location"],
                "pickup_address": donor["address"],
                "status": "expired",
                "accepted_by": None,
                "receiver_id": None,
                "delivery_mode": None,
                "expires_at": created_at + timedelta(hours=BATCH_TTL_HOURS),
                "created_at": created_at,
                "accepted_at": None,
                "claimed_at": None,
                "completed_at": None,
                "match_tier": 1,
                "route_info": None,
                "zone": zone_name,
                "weather": day_weather,
                "perishable": "Sealed" not in description and "dry" not in description,
                "is_synthetic": True,
            }

            completed_p = 0.88 - (0.08 if raining else 0.0)
            if random.random() >= completed_p:
                batch["match_tier"] = 2 if random.random() < 0.5 else 1
                batches.append(batch)
                continue

            # ---- completed rescue: build the pickup timeline -------------
            ngo = _weighted_pick(nonveg_ngos if dietary_tags != ["vegetarian"] else veg_ngos)
            accepted_at = created_at + timedelta(
                minutes=float(np.clip(rng.exponential(18.0), 2.0, 100.0))
            )
            mode = random.choices(
                ["ngo_self", "volunteer", "donor_self"], weights=[0.35, 0.55, 0.10]
            )[0]

            distance_km = haversine_km(
                donor["location"]["coordinates"][0], donor["location"]["coordinates"][1],
                ngo["location"]["coordinates"][0], ngo["location"]["coordinates"][1],
            ) + float(rng.uniform(0.3, 1.2))
            speed_kmh = float(np.clip(rng.normal(22.0, 5.0), 8.0, 45.0))
            if raining:
                speed_kmh *= 0.75
            travel_min = distance_km / speed_kmh * 60.0
            handling_min = float(rng.uniform(5.0, 20.0))

            volunteer = None
            claimed_at = None
            trust_changes = []

            if mode == "volunteer":
                volunteer = _weighted_pick(volunteers)
                claimed_at = accepted_at + timedelta(
                    minutes=float(np.clip(rng.exponential(12.0), 2.0, 60.0))
                )
                completed_at = claimed_at + timedelta(minutes=travel_min + handling_min)

                if "seed_fraud_profile" in volunteer:
                    # FRAUD: bursts of 6 completions in one evening hour, each
                    # "delivered" minutes after claiming regardless of distance.
                    index = fraud_event_index.get(volunteer["_id"], 0)
                    fraud_event_index[volunteer["_id"]] = index + 1
                    burst_date = dates[(index // 6 * 17) % len(dates)]
                    created_at = datetime(
                        burst_date.year, burst_date.month, burst_date.day,
                        20, 30 + (index % 6) * 4, tzinfo=timezone.utc,
                    )
                    accepted_at = created_at + timedelta(minutes=2)
                    claimed_at = accepted_at + timedelta(minutes=1)
                    completed_at = claimed_at + timedelta(
                        minutes=float(rng.uniform(1.5, 4.0))
                    )
                    batch["created_at"] = created_at
                    batch["expires_at"] = created_at + timedelta(hours=BATCH_TTL_HOURS)
                trust_changes.append(
                    {"user_id": volunteer["_id"], "delta": 10, "reason": "Delivery completed"}
                )
                completions[volunteer["_id"]] = completions.get(volunteer["_id"], 0) + 1
            elif mode == "ngo_self":
                completed_at = accepted_at + timedelta(minutes=travel_min + handling_min)
                if "seed_fraud_profile" in ngo:
                    # FRAUD: NGO confirms pickup minutes after accepting even
                    # though the route physically takes 20-40 minutes.
                    completed_at = accepted_at + timedelta(
                        minutes=float(rng.uniform(1.0, 3.0))
                    )
            else:  # donor_self
                completed_at = accepted_at + timedelta(minutes=float(rng.uniform(60.0, 150.0)))
                trust_changes.append(
                    {"user_id": donor["_id"], "delta": 50,
                     "reason": "Self-delivered after no volunteer found (5x bonus)"}
                )

            completions[ngo["_id"]] = completions.get(ngo["_id"], 0) + 1

            batch.update(
                {
                    "status": "completed",
                    "accepted_by": volunteer["_id"] if volunteer else ngo["_id"],
                    "receiver_id": ngo["_id"],
                    "delivery_mode": mode,
                    "accepted_at": accepted_at,
                    "claimed_at": claimed_at,
                    "completed_at": completed_at,
                    "match_tier": 2 if random.random() < 0.12 else 1,
                    "route_info": {
                        "distance_km": round(distance_km, 2),
                        "duration_min": int(travel_min),
                        "status": "simulated",
                    },
                }
            )
            batches.append(batch)
            match_docs.append(
                {
                    "batch_id": batch["_id"],
                    "donor_id": donor["_id"],
                    "receiver_id": ngo["_id"],
                    "volunteer_id": volunteer["_id"] if volunteer else None,
                    "matched_at": accepted_at,
                    "completed_at": completed_at,
                    "trust_score_changes": trust_changes,
                    "is_synthetic": True,
                }
            )

    # Trust evolves with activity — high-volume fraudsters end up with HIGH
    # trust, which is exactly why anomaly detection is needed on top of it.
    for doc in donors + ngos + volunteers:
        done = completions.get(doc["_id"], 0)
        doc["trust_score"] = int(min(100 + done * 1.5 + rng.normal(0, 8), 300))

    return donors + ngos + volunteers, batches, match_docs


def _insert_chunked(collection, docs, chunk=1000):
    for start in range(0, len(docs), chunk):
        collection.insert_many(docs[start:start + chunk])


def seed(n_records=10_000, n_days=730, reset=False, quiet=False):
    """Seed synthetic history. Returns a summary dict."""
    existing = db.users.count_documents({"is_synthetic": True})
    if existing and not reset:
        raise SystemExit(
            f"Found {existing} synthetic users already seeded. "
            "Re-run with --reset to wipe and reseed."
        )
    if reset:
        for collection in (db.users, db.food_batches, db.matches):
            collection.delete_many({"is_synthetic": True})
        db.db.fraud_flags.delete_many({})  # derived from synthetic data

    rng = np.random.default_rng(RNG_SEED)
    random.seed(RNG_SEED)

    users, batches, match_docs = generate(n_records, n_days, rng)
    _insert_chunked(db.users, users)
    _insert_chunked(db.food_batches, batches)
    _insert_chunked(db.matches, match_docs)

    frame = pd.DataFrame(
        [
            {"zone": b["zone"], "status": b["status"], "kg": b["quantity_kg"],
             "date": b["created_at"].date(),
             "veg": b["dietary_tags"] == ["vegetarian"]}
            for b in batches
        ]
    )
    summary = {
        "users": len(users),
        "batches": len(batches),
        "matches": len(match_docs),
        "date_range": [str(frame["date"].min()), str(frame["date"].max())],
        "total_kg": round(float(frame["kg"].sum()), 1),
        "vegetarian_share": round(float(frame["veg"].mean()), 3),
        "status_counts": frame["status"].value_counts().to_dict(),
        "fraud_accounts": [
            {"name": u["name"], "role": u["role"], "profile": u["seed_fraud_profile"]}
            for u in users if "seed_fraud_profile" in u
        ],
    }
    if not quiet:
        print(f"Seeded {summary['batches']} batches / {summary['matches']} matches "
              f"/ {summary['users']} synthetic users")
        print(f"  span {summary['date_range'][0]} .. {summary['date_range'][1]}, "
              f"{summary['total_kg']} kg total, "
              f"{summary['vegetarian_share'] * 100:.1f}% pure vegetarian")
        print(f"  statuses: {summary['status_counts']}")
        print("  kg by zone:")
        print(frame.groupby("zone")["kg"].sum().round(0).sort_values(ascending=False)
              .to_string())
        print("  planted fraud ground truth (for audit verification):")
        for account in summary["fraud_accounts"]:
            print(f"    - {account['role']}: {account['name']} ({account['profile']})")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic FoodRescue history")
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--reset", action="store_true",
                        help="delete previously seeded synthetic data first")
    args = parser.parse_args()
    try:
        seed(args.records, args.days, reset=args.reset)
    except SystemExit as exc:
        print(exc)
        sys.exit(1)
