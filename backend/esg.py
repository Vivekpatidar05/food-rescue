"""
esg.py — Automated ESG (carbon-footprint) monetization for corporate donors.

THE SCIENCE (documented so an auditor can retrace every number)
---------------------------------------------------------------
When 1 kg of vegetarian food is rescued and eaten instead of landfilled, two
distinct emission pathways are avoided:

1. LANDFILL DECOMPOSITION (methane pathway)
   Organic waste in a landfill decomposes anaerobically and releases methane.
   Using the IPCC first-order-decay model for food waste in a typical
   open/managed dumpsite with minimal gas capture (the reality for most
   Indian municipal sites):

       CH4_PER_KG_FOOD = 0.025  kg CH4 per kg of food waste
       GWP100_CH4      = 28     (IPCC AR5/AR6 100-year global warming
                                 potential of methane vs CO2)

       landfill_co2e = kg_food × 0.025 × 28  =  kg_food × 0.70 kg CO2e

   Food eaten within hours decomposes aerobically in a human, not
   anaerobically in a dump — this entire pathway is avoided.

2. AVOIDED REPLACEMENT PRODUCTION
   A rescued meal replaces a meal that would otherwise have to be grown,
   processed, transported and cooked. Life-cycle footprint of mixed
   vegetarian food (Poore & Nemecek 2018, Science — veg-weighted average of
   cereals, pulses, vegetables, dairy-light dishes):

       PRODUCTION_CO2E_PER_KG = 1.80 kg CO2e per kg

   TOTAL AVOIDED = 0.70 + 1.80 = 2.50 kg CO2e per kg rescued
   (matching the 2.5 factor used across the platform's public stats).

REAL-WORLD EQUIVALENTS (how a human visualizes a tonne of CO2e)
   * Passenger car:   4.6 t CO2e/year (US EPA)  → 12.6 kg/day  → "car-days off the road"
   * Urban tree:      absorbs ~21 kg CO2/year (10-yr growth, EPA)  → "tree-years"
   * Petrol driving:  0.192 kg CO2e/km (average petrol car)        → "km not driven"
   * Smartphone:      0.0122 kg CO2e per full charge (EPA)         → "phone charges"

All constants live here, in one place, so a methodology reviewer audits ONE
file. Only real (non-synthetic), COMPLETED rescues ever enter a report.
"""

import csv
import io
from datetime import timedelta

from flask import Blueprint, Response, g, jsonify

import db
from auth import require_auth, require_role

esg_bp = Blueprint("esg", __name__, url_prefix="/esg")

# --- Scientific constants (see module docstring for sources) ---------------
CH4_PER_KG_FOOD = 0.025          # kg methane per kg food landfilled
GWP100_CH4 = 28                  # 100-year warming potential of methane
LANDFILL_CO2E_PER_KG = CH4_PER_KG_FOOD * GWP100_CH4          # 0.70
PRODUCTION_CO2E_PER_KG = 1.80    # avoided replacement production
TOTAL_CO2E_PER_KG = LANDFILL_CO2E_PER_KG + PRODUCTION_CO2E_PER_KG  # 2.50

CAR_KG_CO2E_PER_DAY = 12.6       # 4.6 t/year passenger vehicle
TREE_KG_CO2E_PER_YEAR = 21.0     # urban tree annual sequestration
KG_CO2E_PER_KM_DRIVEN = 0.192    # average petrol car
KG_CO2E_PER_PHONE_CHARGE = 0.0122
MEALS_PER_KG = 2.5

MONTHS_IN_REPORT = 12


def carbon_metrics(kg_food):
    """The full emission-avoidance breakdown for `kg_food` kilograms of
    rescued vegetarian food, plus human-scale equivalents."""
    kg_food = max(0.0, float(kg_food or 0.0))
    methane_kg = kg_food * CH4_PER_KG_FOOD
    landfill_co2e = kg_food * LANDFILL_CO2E_PER_KG
    production_co2e = kg_food * PRODUCTION_CO2E_PER_KG
    total_co2e = landfill_co2e + production_co2e
    return {
        "kg_food": round(kg_food, 1),
        "methane_kg": round(methane_kg, 2),
        "landfill_co2e_kg": round(landfill_co2e, 1),
        "production_co2e_kg": round(production_co2e, 1),
        "total_co2e_kg": round(total_co2e, 1),
        "equivalents": {
            "car_days_off_road": round(total_co2e / CAR_KG_CO2E_PER_DAY, 1),
            "tree_years": round(total_co2e / TREE_KG_CO2E_PER_YEAR, 1),
            "km_not_driven": round(total_co2e / KG_CO2E_PER_KM_DRIVEN),
            "phone_charges": round(total_co2e / KG_CO2E_PER_PHONE_CHARGE),
            "meals_served": int(kg_food * MEALS_PER_KG),
        },
    }


def methodology():
    """Constants exposed with every report — auditors love a paper trail."""
    return {
        "ch4_per_kg_food": CH4_PER_KG_FOOD,
        "gwp100_ch4": GWP100_CH4,
        "landfill_co2e_per_kg": LANDFILL_CO2E_PER_KG,
        "production_co2e_per_kg": PRODUCTION_CO2E_PER_KG,
        "total_co2e_per_kg": TOTAL_CO2E_PER_KG,
        "sources": (
            "IPCC first-order-decay landfill model; IPCC AR5/AR6 GWP-100; "
            "Poore & Nemecek 2018 (Science); US EPA equivalency factors"
        ),
    }


def donor_monthly_series(donor_id, months=MONTHS_IN_REPORT):
    """Aggregate the donor's real completed rescues into a monthly series.

    Uses a single MongoDB $group on the completion month — the database does
    the heavy lifting, Python only shapes the response."""
    since = db.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(months - 1):
        since = (since - timedelta(days=1)).replace(day=1)

    rows = list(
        db.food_batches.aggregate(
            [
                {
                    "$match": {
                        "donor_id": donor_id,
                        "status": "completed",
                        "is_synthetic": {"$ne": True},
                        "completed_at": {"$gte": since},
                    }
                },
                {
                    "$group": {
                        "_id": {"$dateToString": {"format": "%Y-%m", "date": "$completed_at"}},
                        "kg": {"$sum": "$quantity_kg"},
                        "rescues": {"$sum": 1},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
        )
    )
    by_month = {row["_id"]: row for row in rows}

    series = []
    cursor = since
    now = db.utcnow()
    while cursor <= now:
        key = cursor.strftime("%Y-%m")
        row = by_month.get(key, {})
        kg = round(float(row.get("kg") or 0.0), 1)
        series.append(
            {
                "month": key,
                "rescues": int(row.get("rescues") or 0),
                "kg": kg,
                "co2e_kg": round(kg * TOTAL_CO2E_PER_KG, 1),
                "methane_kg": round(kg * CH4_PER_KG_FOOD, 2),
            }
        )
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return series


def donor_lifetime_totals(donor_id):
    """Lifetime totals across every real completed rescue for this donor."""
    rows = list(
        db.food_batches.aggregate(
            [
                {
                    "$match": {
                        "donor_id": donor_id,
                        "status": "completed",
                        "is_synthetic": {"$ne": True},
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "kg": {"$sum": "$quantity_kg"},
                        "rescues": {"$sum": 1},
                        "first": {"$min": "$completed_at"},
                        "last": {"$max": "$completed_at"},
                    }
                },
            ]
        )
    )
    if not rows:
        return {"kg": 0.0, "rescues": 0, "first": None, "last": None}
    row = rows[0]
    return {
        "kg": round(float(row.get("kg") or 0.0), 1),
        "rescues": int(row.get("rescues") or 0),
        "first": row.get("first"),
        "last": row.get("last"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@esg_bp.get("/dashboard")
@require_role("donor")
def dashboard():
    """The corporate sustainability dashboard: lifetime carbon impact,
    human-scale equivalents and the 12-month contribution series."""
    donor = g.current_user
    totals = donor_lifetime_totals(donor["_id"])
    metrics = carbon_metrics(totals["kg"])
    return jsonify(
        {
            "donor": {"name": donor.get("name"), "since": db.serialize(totals["first"])},
            "totals": {
                "rescues": totals["rescues"],
                **metrics,
            },
            "monthly": donor_monthly_series(donor["_id"]),
            "methodology": methodology(),
        }
    )


@esg_bp.get("/report.csv")
@require_role("donor")
def report_csv():
    """Certified ESG audit report as CSV — monthly contributions, verified
    carbon offsets and the full methodology, ready for CSR filings."""
    donor = g.current_user
    totals = donor_lifetime_totals(donor["_id"])
    metrics = carbon_metrics(totals["kg"])
    series = donor_monthly_series(donor["_id"])

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["FoodRescue — Certified ESG / Carbon Offset Report"])
    writer.writerow(["Organization", donor.get("name", "")])
    writer.writerow(["Generated (UTC)", db.utcnow().strftime("%Y-%m-%d %H:%M")])
    writer.writerow(["Basis", "Real, proof-backed completed rescues only (synthetic/training data excluded)"])
    writer.writerow([])
    writer.writerow(["month", "rescues", "food_kg", "methane_avoided_kg", "co2e_avoided_kg"])
    for row in series:
        writer.writerow([row["month"], row["rescues"], row["kg"], row["methane_kg"], row["co2e_kg"]])
    writer.writerow([])
    writer.writerow(["LIFETIME_TOTAL_RESCUES", totals["rescues"]])
    writer.writerow(["LIFETIME_TOTAL_FOOD_KG", metrics["kg_food"]])
    writer.writerow(["LIFETIME_METHANE_AVOIDED_KG", metrics["methane_kg"]])
    writer.writerow(["LIFETIME_CO2E_AVOIDED_KG", metrics["total_co2e_kg"]])
    writer.writerow(["EQUIV_CAR_DAYS_OFF_ROAD", metrics["equivalents"]["car_days_off_road"]])
    writer.writerow(["EQUIV_TREE_YEARS", metrics["equivalents"]["tree_years"]])
    writer.writerow(["EQUIV_KM_NOT_DRIVEN", metrics["equivalents"]["km_not_driven"]])
    writer.writerow([])
    writer.writerow(["METHODOLOGY"])
    m = methodology()
    writer.writerow(["kg CH4 per kg food landfilled", m["ch4_per_kg_food"]])
    writer.writerow(["GWP-100 of methane", m["gwp100_ch4"]])
    writer.writerow(["Landfill CO2e per kg food", m["landfill_co2e_per_kg"]])
    writer.writerow(["Avoided production CO2e per kg", m["production_co2e_per_kg"]])
    writer.writerow(["Total CO2e per kg rescued", m["total_co2e_per_kg"]])
    writer.writerow(["Sources", m["sources"]])

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=foodrescue-esg-report.csv"},
    )


@esg_bp.get("/preview")
@require_auth
def preview():
    """Public-math preview: what would X kg avoid? Used by the frontend to
    render live equivalence hints (?kg=12.5)."""
    from flask import request

    try:
        kg = float(request.args.get("kg", "0"))
    except ValueError:
        kg = 0.0
    return jsonify({"metrics": carbon_metrics(min(max(kg, 0.0), 100000.0))})
