"""
fraud_detection.py — Trust-score anomaly detection (Deliverable 4).

Unsupervised audit of the Matches + FoodBatches + Users collections. For
every completed rescue we reconstruct the physical timeline (accept -> claim
-> travel -> confirm) and compare it against the logistics route: an NGO that
confirms a pickup 2 minutes after accepting a 9 km route, or a volunteer who
"completes" six deliveries inside one hour, is statistically impossible.

Two detection layers, combined per account:

  1. Hard physics rules — implied speed above SPEED_CEILING_KMH, completion
     bursts, sub-3-minute multi-km deliveries. These are explainable and
     catch the blatant cases on their own.
  2. IsolationForest (scikit-learn) over per-account behaviour features
     (event volume, median/max implied speed, impossible-timeline share,
     burst size, median duration, accept latency). This catches accounts
     that are odd relative to their peers WITHOUT any labelled fraud data —
     which is the point: the platform has no ground truth in production.

Trust scores alone cannot catch this: fraudsters accumulate +10 completions
fast, so their trust is often the HIGHEST. The audit is behaviour-based.

Exposed via /api/run-audit (ds_routes.py). Each run is persisted to the
fraud_flags collection for review history.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

import db
from zones import haversine_km

SPEED_CEILING_KMH = 80.0     # nothing street-legal moves food faster
MIN_PLAUSIBLE_MIN = 3.0      # multi-km pickup confirmed under this = fake
BURST_WINDOW_MIN = 60.0
BURST_THRESHOLD = 5          # completions per hour no human courier hits
IMPOSSIBLE_SHARE_FLAG = 0.30 # flag when >=30% of an account's events are fake
MIN_EVENTS_FOR_RULES = 3
MIN_ACCOUNTS_FOR_ML = 10     # IsolationForest needs a peer group
CONTAMINATION = 0.05
FEATURES = [
    "events", "median_speed_kmh", "max_speed_kmh", "impossible_share",
    "max_burst_1h", "median_duration_min", "median_accept_latency_min",
]
# Human-readable labels so the audit can explain WHICH behaviours drove a flag.
FEATURE_LABELS = {
    "events": "completions",
    "median_speed_kmh": "median speed",
    "max_speed_kmh": "top speed",
    "impossible_share": "impossible-timeline share",
    "max_burst_1h": "peak deliveries/hour",
    "median_duration_min": "median delivery time",
    "median_accept_latency_min": "accept latency",
}

fraud_flags = db.db.fraud_flags


def _minutes(later, earlier):
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds() / 60.0


def build_event_frame():
    """One row per completed rescue: who moved it, how far, how fast."""
    batches = {
        batch["_id"]: batch
        for batch in db.food_batches.find(
            {"status": "completed"},
            {"pickup_location": 1, "route_info": 1, "receiver_id": 1,
             "accepted_at": 1, "claimed_at": 1, "created_at": 1,
             "quantity_kg": 1, "delivery_mode": 1},
        )
    }
    user_locations = {
        user["_id"]: (user.get("location") or {}).get("coordinates")
        for user in db.users.find({}, {"location": 1})
    }

    rows = []
    for match in db.matches.find({"completed_at": {"$ne": None}}):
        batch = batches.get(match.get("batch_id"))
        if batch is None:
            continue
        actor_id = match.get("volunteer_id") or match.get("receiver_id")
        if actor_id is None:
            continue
        role = "volunteer" if match.get("volunteer_id") else "ngo"

        # Physical clock starts when the mover takes responsibility.
        start = batch.get("claimed_at") if role == "volunteer" else None
        start = start or match.get("matched_at") or batch.get("accepted_at")
        duration_min = _minutes(match["completed_at"], start)
        if duration_min is None:
            continue

        distance_km = (batch.get("route_info") or {}).get("distance_km")
        if distance_km is None:
            pickup = (batch.get("pickup_location") or {}).get("coordinates")
            receiver = user_locations.get(batch.get("receiver_id"))
            if pickup and receiver:
                distance_km = haversine_km(pickup[0], pickup[1], receiver[0], receiver[1])
        if distance_km is None:
            continue

        speed_kmh = (
            distance_km / (duration_min / 60.0) if duration_min > 0.5 else float("inf")
        )
        rows.append(
            {
                "actor_id": actor_id,
                "role": role,
                "batch_id": match["batch_id"],
                "completed_at": match["completed_at"],
                "duration_min": duration_min,
                "distance_km": float(distance_km),
                "speed_kmh": min(speed_kmh, 999.0),
                "quantity_kg": float(batch.get("quantity_kg") or 0.0),
                "accept_latency_min": _minutes(
                    batch.get("accepted_at"), batch.get("created_at")
                ) or 0.0,
                "impossible": bool(
                    speed_kmh > SPEED_CEILING_KMH
                    or (duration_min < MIN_PLAUSIBLE_MIN and distance_km > 2.0)
                    or duration_min <= 0
                ),
            }
        )
    return pd.DataFrame(rows)


def _max_burst(completed_times):
    """Largest number of completions inside any sliding 60-minute window."""
    stamps = sorted(completed_times)
    best, left = 0, 0
    for right, stamp in enumerate(stamps):
        while _minutes(stamp, stamps[left]) > BURST_WINDOW_MIN:
            left += 1
        best = max(best, right - left + 1)
    return best


def build_account_frame(events):
    """Aggregate events into one behaviour row per NGO/volunteer account."""
    records = []
    for actor_id, group in events.groupby("actor_id"):
        records.append(
            {
                "actor_id": actor_id,
                "role": group["role"].iloc[0],
                "events": int(len(group)),
                "total_kg": round(float(group["quantity_kg"].sum()), 1),
                "median_speed_kmh": round(float(group["speed_kmh"].median()), 1),
                "max_speed_kmh": round(float(group["speed_kmh"].max()), 1),
                "impossible_share": round(float(group["impossible"].mean()), 3),
                "max_burst_1h": _max_burst(group["completed_at"].tolist()),
                "median_duration_min": round(float(group["duration_min"].median()), 1),
                "median_accept_latency_min": round(
                    float(group["accept_latency_min"].median()), 1
                ),
            }
        )
    return pd.DataFrame(records)


def _peer_stats(accounts):
    """Per-feature mean and (zero-safe) std across all audited accounts — the
    peer group each flagged account is compared against."""
    matrix = accounts[FEATURES].to_numpy(dtype=float)
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0)
    std = np.where(std > 1e-9, std, 1.0)  # a flat feature contributes z = 0
    return mean, std


def _explain(account, feat_mean, feat_std):
    """Rank the account's features by how many standard deviations they sit
    above the peer mean. This is the model's 'why': the top few positive-z
    features are the behaviours that make the account an outlier."""
    contributions = []
    for index, feature in enumerate(FEATURES):
        value = float(account[feature])
        z = (value - feat_mean[index]) / feat_std[index]
        contributions.append(
            {
                "feature": feature,
                "label": FEATURE_LABELS[feature],
                "value": round(value, 1),
                "peer_mean": round(float(feat_mean[index]), 1),
                "z": round(float(z), 2),
            }
        )
    contributions.sort(key=lambda item: item["z"], reverse=True)
    return contributions


def _rule_reasons(account):
    reasons = []
    if account["impossible_share"] >= IMPOSSIBLE_SHARE_FLAG and account["events"] >= MIN_EVENTS_FOR_RULES:
        reasons.append(
            f"{account['impossible_share'] * 100:.0f}% of {account['events']} completions "
            f"have physically impossible timelines (speed ceiling {SPEED_CEILING_KMH:.0f} km/h)"
        )
    if account["max_burst_1h"] >= BURST_THRESHOLD:
        reasons.append(
            f"{account['max_burst_1h']} deliveries confirmed within a single "
            f"{BURST_WINDOW_MIN:.0f}-minute window"
        )
    if account["median_speed_kmh"] > SPEED_CEILING_KMH:
        reasons.append(
            f"median implied speed {account['median_speed_kmh']:.0f} km/h "
            f"exceeds the {SPEED_CEILING_KMH:.0f} km/h ceiling"
        )
    return reasons


def run_audit(persist=True):
    """Full audit pass. Returns the flag report (JSON-safe dict)."""
    events = build_event_frame()
    if events.empty:
        return {
            "run_at": db.serialize(db.utcnow()),
            "accounts_analyzed": 0,
            "events_analyzed": 0,
            "model": None,
            "flagged": [],
            "note": "No completed matches to audit — run seed_data.py first.",
        }

    accounts = build_account_frame(events)
    feat_mean, feat_std = _peer_stats(accounts)

    # Layer 2: IsolationForest over standardized behaviour features.
    model_name = "rules-only"
    accounts["iso_outlier"] = False
    accounts["anomaly_score"] = 0.0
    if len(accounts) >= MIN_ACCOUNTS_FOR_ML:
        matrix = StandardScaler().fit_transform(accounts[FEATURES].to_numpy())
        forest = IsolationForest(
            n_estimators=200, contamination=CONTAMINATION, random_state=42
        ).fit(matrix)
        accounts["iso_outlier"] = forest.predict(matrix) == -1
        raw = -forest.decision_function(matrix)  # higher = more anomalous
        spread = raw.max() - raw.min()
        accounts["anomaly_score"] = (
            (raw - raw.min()) / spread if spread > 0 else np.zeros(len(raw))
        )
        model_name = f"IsolationForest(n_estimators=200, contamination={CONTAMINATION})"

    user_names = {
        user["_id"]: user
        for user in db.users.find(
            {"_id": {"$in": accounts["actor_id"].tolist()}},
            {"name": 1, "trust_score": 1, "is_synthetic": 1},
        )
    }

    flagged = []
    for _, account in accounts.iterrows():
        reasons = _rule_reasons(account)
        if account["iso_outlier"]:
            reasons.append(
                f"IsolationForest peer-group outlier (anomaly score "
                f"{account['anomaly_score']:.2f})"
            )
        if not reasons:
            continue
        user = user_names.get(account["actor_id"], {})
        flagged.append(
            {
                "account_id": str(account["actor_id"]),
                "name": user.get("name", "unknown"),
                "role": account["role"],
                "trust_score": user.get("trust_score"),
                "is_synthetic": bool(user.get("is_synthetic", False)),
                "anomaly_score": round(float(account["anomaly_score"]), 3),
                "reasons": reasons,
                "features": {
                    feature: (
                        int(account[feature])
                        if feature in ("events", "max_burst_1h")
                        else float(account[feature])
                    )
                    for feature in FEATURES
                },
                "explain": _explain(account, feat_mean, feat_std),
                "recommended_action": (
                    "suspend pending review"
                    if account["impossible_share"] >= IMPOSSIBLE_SHARE_FLAG
                    else "manual review"
                ),
            }
        )
    flagged.sort(key=lambda item: item["anomaly_score"], reverse=True)

    report = {
        "run_at": db.serialize(db.utcnow()),
        "accounts_analyzed": int(len(accounts)),
        "events_analyzed": int(len(events)),
        "model": model_name,
        "thresholds": {
            "speed_ceiling_kmh": SPEED_CEILING_KMH,
            "burst_per_hour": BURST_THRESHOLD,
            "impossible_share": IMPOSSIBLE_SHARE_FLAG,
        },
        "flagged_count": len(flagged),
        "flagged": flagged,
    }
    if persist:
        fraud_flags.insert_one(
            {**{key: value for key, value in report.items()}, "run_at": db.utcnow()}
        )
    return report


if __name__ == "__main__":
    import json

    report = run_audit(persist=False)
    print(f"analyzed {report['accounts_analyzed']} accounts / "
          f"{report['events_analyzed']} events — model: {report['model']}")
    for flag in report["flagged"]:
        print(f"\nFLAG {flag['role']}: {flag['name']} "
              f"(trust={flag['trust_score']}, score={flag['anomaly_score']})")
        for reason in flag["reasons"]:
            print(f"   - {reason}")
