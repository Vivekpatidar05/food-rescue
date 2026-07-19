"""
stats_routes.py — public, aggregate-only impact statistics, plus the
authenticated personal impact report (JSON + CSV download).

No personal data beyond first names on the leaderboard; the public routes are
safe to expose without authentication for landing-page widgets.
"""

import csv
import io
from datetime import timedelta

from flask import Blueprint, Response, g, jsonify, request

import db
import gamification
from auth import require_auth

stats_bp = Blueprint("stats", __name__, url_prefix="/stats")

MEALS_PER_KG = 2.5      # rough conversion used by food banks
CO2E_KG_PER_KG = 2.5    # avoided emissions per kg of food waste

ACTIVE_STATUSES = ["pending", "ngo_pickup", "volunteer_needed", "in_transit", "donor_delivering"]

# Synthetic history (seed_data.py) only trains the ML models — public impact
# numbers and leaderboards must reflect real rescues only.
REAL_ONLY = {"is_synthetic": {"$ne": True}}


@stats_bp.get("/impact")
def impact():
    totals = list(
        db.food_batches.aggregate(
            [
                {"$match": {"status": "completed", **REAL_ONLY}},
                {
                    "$group": {
                        "_id": None,
                        "kg": {"$sum": "$quantity_kg"},
                        "count": {"$sum": 1},
                    }
                },
            ]
        )
    )
    kg = round(totals[0]["kg"], 1) if totals else 0.0
    completed = totals[0]["count"] if totals else 0

    return jsonify(
        {
            "total_kg_rescued": kg,
            "meals_served_estimate": int(kg * MEALS_PER_KG),
            "co2e_kg_saved_estimate": round(kg * CO2E_KG_PER_KG, 1),
            "rescues_completed": completed,
            "active_batches": db.food_batches.count_documents(
                {"status": {"$in": ACTIVE_STATUSES}, **REAL_ONLY}
            ),
            "community": {
                "donors": db.users.count_documents({"role": "donor", **REAL_ONLY}),
                "ngos": db.users.count_documents({"role": "ngo", **REAL_ONLY}),
                "volunteers": db.users.count_documents({"role": "volunteer", **REAL_ONLY}),
            },
        }
    )


@stats_bp.get("/trends")
def trends():
    """Daily rescue activity for the last N days (default 30, max 90) —
    aggregate-only, real rescues only. Powers the ops dashboard charts."""
    try:
        days = min(max(int(request.args.get("days", "30")), 7), 90)
    except ValueError:
        days = 30
    since = db.utcnow() - timedelta(days=days)

    def _daily(match, date_field):
        rows = db.food_batches.aggregate(
            [
                {"$match": {**match, **REAL_ONLY, date_field: {"$gte": since}}},
                {
                    "$group": {
                        "_id": {
                            "$dateToString": {"format": "%Y-%m-%d", "date": f"${date_field}"}
                        },
                        "kg": {"$sum": "$quantity_kg"},
                        "count": {"$sum": 1},
                    }
                },
            ]
        )
        return {row["_id"]: row for row in rows}

    completed = _daily({"status": "completed"}, "completed_at")
    broadcast = _daily({}, "created_at")

    daily = []
    for offset in range(days, -1, -1):
        day = (db.utcnow() - timedelta(days=offset)).strftime("%Y-%m-%d")
        done = completed.get(day, {})
        sent = broadcast.get(day, {})
        daily.append(
            {
                "date": day,
                "rescued_kg": round(float(done.get("kg", 0.0)), 1),
                "rescues": int(done.get("count", 0)),
                "broadcasts": int(sent.get("count", 0)),
                "broadcast_kg": round(float(sent.get("kg", 0.0)), 1),
            }
        )

    return jsonify(
        {
            "days": days,
            "daily": daily,
            "totals": {
                "rescued_kg": round(sum(d["rescued_kg"] for d in daily), 1),
                "rescues": sum(d["rescues"] for d in daily),
                "broadcasts": sum(d["broadcasts"] for d in daily),
            },
        }
    )


@stats_bp.get("/activity")
def activity():
    """Public live-rescue ticker: the latest completed real rescues, lightly
    anonymized (first names only) — powers the dashboards' activity feed."""
    import zones  # local import: zones resolves its city center lazily

    items = []
    cursor = (
        db.food_batches.find(
            {"status": "completed", **REAL_ONLY},
            {"quantity_kg": 1, "completed_at": 1, "pickup_location": 1,
             "donor_id": 1, "delivery_mode": 1},
        )
        .sort("completed_at", -1)
        .limit(12)
    )
    for batch in cursor:
        donor = db.users.find_one({"_id": batch.get("donor_id")}, {"name": 1})
        first_name = ((donor or {}).get("name") or "Someone").split()[0]
        zone_name = None
        try:
            coords = (batch.get("pickup_location") or {}).get("coordinates")
            if coords:
                zone_name = zones.assign_zone(coords[0], coords[1])
        except Exception:
            zone_name = None
        items.append(
            {
                "kg": round(float(batch.get("quantity_kg") or 0.0), 1),
                "zone": zone_name,
                "delivery_mode": batch.get("delivery_mode"),
                "donor_first_name": first_name,
                "completed_at": db.serialize(batch.get("completed_at")),
            }
        )
    return jsonify({"activity": items, "meals_per_kg": MEALS_PER_KG})


@stats_bp.get("/zones-live")
def zones_live():
    """Live per-zone batch counts for the ops city map: pending, moving, and
    completed-today totals (real rescues only)."""
    import zones

    since_midnight = db.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {}
    for row in db.food_batches.aggregate(
        [
            {"$match": {**REAL_ONLY, "zone": {"$ne": None}}},
            {
                "$group": {
                    "_id": "$zone",
                    "pending": {"$sum": {"$cond": [{"$eq": ["$status", "pending"]}, 1, 0]}},
                    "moving": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["ngo_pickup", "volunteer_needed",
                                                     "in_transit", "donor_delivering"]]},
                                1, 0,
                            ]
                        }
                    },
                    "completed_today": {
                        "$sum": {
                            "$cond": [
                                {"$and": [{"$eq": ["$status", "completed"]},
                                          {"$gte": ["$completed_at", since_midnight]}]},
                                1, 0,
                            ]
                        }
                    },
                }
            },
        ]
    ):
        counts[row["_id"]] = row

    payload = []
    for zone in zones.get_zones():
        row = counts.get(zone["name"], {})
        payload.append(
            {
                "name": zone["name"],
                "lng": zone["lng"],
                "lat": zone["lat"],
                "pending": int(row.get("pending") or 0),
                "moving": int(row.get("moving") or 0),
                "completed_today": int(row.get("completed_today") or 0),
            }
        )
    return jsonify({"zones": payload})


@stats_bp.get("/surge")
def surge():
    """Public surge flag — every dashboard polls this for the emergency
    banner. Aggregate-only, no auth needed."""
    state = db.surge_state()
    return jsonify(
        {
            "active": state["active"],
            "reason": state["reason"] if state["active"] else "",
            "multiplier": state["multiplier"] if state["active"] else 1.0,
        }
    )


@stats_bp.get("/leaderboard")
def leaderboard():
    top_volunteers = []
    for user in db.users.find({"role": "volunteer", **REAL_ONLY}).sort("trust_score", -1).limit(5):
        rating_count = int(user.get("rating_count") or 0)
        top_volunteers.append(
            {
                "name": user["name"],
                "trust_score": user.get("trust_score", 100),
                "deliveries": db.matches.count_documents({"volunteer_id": user["_id"]}),
                "rating": round(int(user.get("rating_sum") or 0) / rating_count, 1)
                if rating_count else None,
            }
        )

    top_donors = list(
        db.food_batches.aggregate(
            [
                {"$match": {"status": "completed", **REAL_ONLY}},
                {"$group": {"_id": "$donor_id", "kg_donated": {"$sum": "$quantity_kg"}}},
                {"$sort": {"kg_donated": -1}},
                {"$limit": 5},
                {
                    "$lookup": {
                        "from": "users",
                        "localField": "_id",
                        "foreignField": "_id",
                        "as": "user",
                    }
                },
                {"$unwind": {"path": "$user", "preserveNullAndEmptyArrays": True}},
                {
                    "$project": {
                        "_id": 0,
                        "name": "$user.name",
                        "kg_donated": {"$round": ["$kg_donated", 1]},
                    }
                },
            ]
        )
    )

    return jsonify({"top_volunteers": top_volunteers, "top_donors": top_donors})


# ---------------------------------------------------------------------------
# Personal impact report — the user's own footprint, plus a CSV export
# ---------------------------------------------------------------------------

IMPACT_WEEKS = 12


def _my_completed(user):
    """The current user's completed real batches, oldest first."""
    return list(
        db.food_batches.find(
            gamification._completed_query(user),
            {
                "food_description": 1, "quantity_kg": 1, "completed_at": 1,
                "zone": 1, "delivery_mode": 1, "dietary_tags": 1,
            },
        ).sort("completed_at", 1)
    )


@stats_bp.get("/my-impact")
@require_auth
def my_impact():
    """Personal impact report: lifetime totals, a 12-week weekly series for
    the dashboard sparkline, streak, and favourite zone."""
    user = g.current_user
    completed = _my_completed(user)
    total_kg = round(sum(float(b.get("quantity_kg") or 0.0) for b in completed), 1)

    # Weekly buckets, oldest → newest, aligned to the current week's Monday.
    now = db.utcnow()
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    weeks = [
        {"week_start": (this_monday - timedelta(weeks=offset)).strftime("%Y-%m-%d"),
         "kg": 0.0, "rescues": 0}
        for offset in range(IMPACT_WEEKS - 1, -1, -1)
    ]
    index = {week["week_start"]: week for week in weeks}
    for batch in completed:
        done_at = batch.get("completed_at")
        if done_at is None:
            continue
        if done_at.tzinfo is None:
            done_at = done_at.replace(tzinfo=db.timezone.utc)
        monday = (done_at - timedelta(days=done_at.weekday())).strftime("%Y-%m-%d")
        week = index.get(monday)
        if week:
            week["kg"] = round(week["kg"] + float(batch.get("quantity_kg") or 0.0), 1)
            week["rescues"] += 1

    zone_counts = {}
    for batch in completed:
        zone = batch.get("zone")
        if zone:
            zone_counts[zone] = zone_counts.get(zone, 0) + 1

    return jsonify(
        {
            "role": user.get("role"),
            "totals": {
                "rescues": len(completed),
                "kg": total_kg,
                "meals_estimate": int(total_kg * MEALS_PER_KG),
                "co2e_kg_saved": round(total_kg * CO2E_KG_PER_KG, 1),
            },
            "weekly": weeks,
            "streak_days": gamification._streak_days(
                [b.get("completed_at") for b in completed]
            ),
            "top_zone": max(zone_counts, key=zone_counts.get) if zone_counts else None,
            "member_since": db.serialize(user.get("created_at")),
        }
    )


@stats_bp.get("/my-impact.csv")
@require_auth
def my_impact_csv():
    """Downloadable CSV of every completed rescue the user was part of —
    for NGO grant reports and donors' CSR paperwork."""
    user = g.current_user
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["completed_at_utc", "food_description", "quantity_kg", "meals_estimate",
         "co2e_kg_saved", "zone", "delivery_mode", "dietary_tags"]
    )
    for batch in _my_completed(user):
        kg = float(batch.get("quantity_kg") or 0.0)
        completed_at = batch.get("completed_at")
        writer.writerow(
            [
                completed_at.strftime("%Y-%m-%d %H:%M") if completed_at else "",
                batch.get("food_description", ""),
                kg,
                int(kg * MEALS_PER_KG),
                round(kg * CO2E_KG_PER_KG, 1),
                batch.get("zone", ""),
                batch.get("delivery_mode", ""),
                "|".join(batch.get("dietary_tags") or []),
            ]
        )
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=foodrescue-impact.csv"},
    )
