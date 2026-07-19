"""
gamification.py — achievements, streaks and levels for every role.

    GET /gamification/achievements   (auth required)

Everything is computed live from the food_batches collection — no separate
progress documents to drift out of sync. Synthetic seed data never counts:
achievements only reward real rescues.

XP model: 10 XP per completed rescue + 1 XP per kg moved. Levels are fixed
bands so progress is always visible on the dashboard.
"""

from datetime import timedelta

from flask import Blueprint, g, jsonify

import db
from auth import require_auth

gamification_bp = Blueprint("gamification", __name__, url_prefix="/gamification")

LEVELS = [
    (0, "Rookie Rescuer"),
    (50, "Meal Mover"),
    (150, "Street Hero"),
    (400, "Hunger Fighter"),
    (900, "Rescue Legend"),
    (2000, "Guardian of the City"),
]

# (badge id, label, emoji, metric, threshold)
BADGES = {
    "volunteer": [
        ("first_delivery", "First Delivery", "🚀", "completed", 1),
        ("five_deliveries", "5 Deliveries", "🔥", "completed", 5),
        ("twentyfive_deliveries", "25 Deliveries", "🏅", "completed", 25),
        ("hundred_kg", "100 kg Moved", "💪", "kg", 100),
        ("five_hundred_kg", "500 kg Moved", "🦾", "kg", 500),
        ("week_streak", "7-Day Streak", "⚡", "streak", 7),
    ],
    "donor": [
        ("first_donation", "First Donation", "🌱", "completed", 1),
        ("ten_donations", "10 Donations", "🥗", "completed", 10),
        ("fifty_donations", "50 Donations", "🏆", "completed", 50),
        ("hundred_kg", "100 kg Donated", "🍚", "kg", 100),
        ("self_delivery_hero", "Self-Delivery Hero", "🦸", "self_deliveries", 1),
        ("week_streak", "7-Day Streak", "⚡", "streak", 7),
    ],
    "ngo": [
        ("first_rescue", "First Rescue", "🤝", "completed", 1),
        ("ten_rescues", "10 Rescues Received", "🏛️", "completed", 10),
        ("fifty_rescues", "50 Rescues Received", "🏆", "completed", 50),
        ("five_hundred_kg", "500 kg Received", "🍛", "kg", 500),
        ("week_streak", "7-Day Streak", "⚡", "streak", 7),
    ],
}


def _completed_query(user):
    """Completed real batches this user was responsible for, by role."""
    role = user.get("role")
    base = {"status": "completed", "is_synthetic": {"$ne": True}}
    if role == "donor":
        return {**base, "donor_id": user["_id"]}
    if role == "ngo":
        return {**base, "receiver_id": user["_id"]}
    return {**base, "accepted_by": user["_id"], "delivery_mode": "volunteer"}


def _streak_days(dates):
    """Longest run of consecutive active days that ends today or yesterday —
    a live streak, not a historical best."""
    if not dates:
        return 0
    days = {d.date() for d in dates if d is not None}
    today = db.utcnow().date()
    anchor = today if today in days else today - timedelta(days=1)
    if anchor not in days:
        return 0
    streak = 0
    while anchor in days:
        streak += 1
        anchor -= timedelta(days=1)
    return streak


def _level_for(xp):
    current = LEVELS[0]
    next_level = None
    for threshold, name in LEVELS:
        if xp >= threshold:
            current = (threshold, name)
        elif next_level is None:
            next_level = (threshold, name)
    return {
        "name": current[1],
        "number": LEVELS.index(current) + 1,
        "xp": xp,
        "next_level": next_level[1] if next_level else None,
        "xp_to_next": (next_level[0] - xp) if next_level else 0,
    }


@gamification_bp.get("/achievements")
@require_auth
def achievements():
    user = g.current_user
    role = user.get("role", "volunteer")

    completed = list(
        db.food_batches.find(
            _completed_query(user), {"quantity_kg": 1, "completed_at": 1, "delivery_mode": 1}
        )
    )
    count = len(completed)
    kg = round(sum(float(b.get("quantity_kg") or 0.0) for b in completed), 1)
    streak = _streak_days([b.get("completed_at") for b in completed])
    self_deliveries = sum(1 for b in completed if b.get("delivery_mode") == "donor_self")

    metrics = {
        "completed": count,
        "kg": kg,
        "streak": streak,
        "self_deliveries": self_deliveries,
    }

    badges = [
        {
            "id": badge_id,
            "label": label,
            "emoji": emoji,
            "earned": metrics[metric] >= threshold,
            "progress": min(round(metrics[metric] / threshold, 3), 1.0),
            "threshold": threshold,
            "metric": metric,
        }
        for badge_id, label, emoji, metric, threshold in BADGES.get(role, BADGES["volunteer"])
    ]

    xp = int(count * 10 + kg)
    return jsonify(
        {
            "role": role,
            "stats": {
                "rescues_completed": count,
                "kg_moved": kg,
                "streak_days": streak,
                "meals_estimate": int(kg * 2.5),
            },
            "level": _level_for(xp),
            "badges": badges,
            "badges_earned": sum(1 for b in badges if b["earned"]),
        }
    )
