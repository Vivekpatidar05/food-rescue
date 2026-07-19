"""
analytics.py — descriptive operational analytics for the ops dashboard.

Unlike the forecaster (time_series.py) and the fraud audit (fraud_detection.py),
nothing here is predictive — these are aggregate summaries of history used to
explain *when* surplus happens. The flagship is the weekday x hour demand
surface: it exposes the twin lunch/dinner peaks and the weekend wedding-hall
spike the simulator baked in, which is what a dispatcher schedules volunteers
around.

Like the forecaster, this reads ALL broadcast history (synthetic + live) — it
describes demand patterns, not public impact, so the real-only rule that guards
the public /stats endpoints does not apply.
"""

import pandas as pd

import db

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def demand_patterns():
    """Weekday x hour surplus surface over all broadcast history.

    Returns a dense 7x24 grid (kg and batch counts) plus the peak cell and a
    per-weekday total, ready to render as a heatmap without further reshaping.
    Hours are the batch's stored clock hour."""
    rows = []
    cursor = db.food_batches.find(
        {"created_at": {"$ne": None}},
        {"created_at": 1, "quantity_kg": 1},
    )
    for batch in cursor:
        stamp = pd.Timestamp(batch["created_at"])
        if stamp.tz is not None:
            stamp = stamp.tz_localize(None)
        rows.append(
            {
                "weekday": int(stamp.weekday()),
                "hour": int(stamp.hour),
                "kg": float(batch.get("quantity_kg") or 0.0),
            }
        )

    if not rows:
        return {
            "weekdays": WEEKDAYS,
            "hours": list(range(24)),
            "grid": [[0.0] * 24 for _ in range(7)],
            "counts": [[0] * 24 for _ in range(7)],
            "max_kg": 0.0,
            "total_kg": 0.0,
            "peak": None,
            "note": "No broadcast history yet — run seed_data.py.",
        }

    frame = pd.DataFrame(rows)
    kg_pivot = (
        frame.pivot_table(index="weekday", columns="hour", values="kg",
                          aggfunc="sum", fill_value=0.0)
        .reindex(index=range(7), columns=range(24), fill_value=0.0)
    )
    count_pivot = (
        frame.pivot_table(index="weekday", columns="hour", values="kg",
                          aggfunc="count", fill_value=0)
        .reindex(index=range(7), columns=range(24), fill_value=0)
    )

    grid = [[round(float(kg_pivot.iat[d, h]), 1) for h in range(24)] for d in range(7)]
    counts = [[int(count_pivot.iat[d, h]) for h in range(24)] for d in range(7)]

    max_kg = max((max(row) for row in grid), default=0.0)
    peak_d, peak_h, peak_kg = 0, 0, -1.0
    for d in range(7):
        for h in range(24):
            if grid[d][h] > peak_kg:
                peak_d, peak_h, peak_kg = d, h, grid[d][h]

    return {
        "weekdays": WEEKDAYS,
        "hours": list(range(24)),
        "grid": grid,
        "counts": counts,
        "max_kg": round(float(max_kg), 1),
        "total_kg": round(float(frame["kg"].sum()), 1),
        "weekday_totals": [round(float(sum(grid[d])), 1) for d in range(7)],
        "peak": {
            "weekday": WEEKDAYS[peak_d],
            "hour": peak_h,
            "kg": round(float(peak_kg), 1),
        },
        "generated_at": db.serialize(db.utcnow()),
    }


if __name__ == "__main__":
    import json

    result = demand_patterns()
    print("peak:", result["peak"], "max_kg:", result["max_kg"])
    print(json.dumps(result["weekday_totals"], indent=2))
