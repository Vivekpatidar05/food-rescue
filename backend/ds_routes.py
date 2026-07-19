"""
ds_routes.py — Data-science API endpoints.

    GET  /api/predict-surplus    7-day per-zone surplus forecast (auth required)
    GET  /api/forecast-accuracy  walk-forward backtest of the forecaster (auth)
    GET  /api/demand-patterns    weekday x hour demand surface     (auth)
    GET  /api/run-audit          behaviour/fraud audit           (auth + admin)
    POST /api/run-audit          same, explicit trigger
    POST /api/parse-food         NLP preview of a raw food description (auth)

Admin gate: if ADMIN_EMAILS is set in .env (comma-separated), only those
accounts may run the audit — it exposes account-level behaviour data. When
unset (local development) any authenticated user is allowed.
"""

from flask import Blueprint, jsonify, request

import analytics
import fraud_detection
import nlp_engine
import time_series
from auth import admin_allowed as _admin_allowed
from auth import require_auth

ds_bp = Blueprint("ds", __name__, url_prefix="/api")

MAX_PARSE_CHARS = 1000


@ds_bp.get("/predict-surplus")
@require_auth
def predict_surplus():
    """7-day forecast of surplus kg per city zone, with a suggested volunteer
    pre-positioning split. Cached ~6h; ?refresh=1 forces retraining."""
    result = time_series.predict_surplus(refresh=request.args.get("refresh") == "1")
    if result.get("error"):
        return jsonify(result), 503
    return jsonify(result)


@ds_bp.get("/forecast-accuracy")
@require_auth
def forecast_accuracy():
    """Walk-forward backtest: how well the surplus forecaster predicts days it
    never trained on, versus a seasonal-naive baseline. Cached ~6h; ?refresh=1
    re-runs the validation."""
    result = time_series.backtest(refresh=request.args.get("refresh") == "1")
    if result.get("error"):
        return jsonify(result), 503
    return jsonify(result)


@ds_bp.get("/demand-patterns")
@require_auth
def demand_patterns():
    """Weekday x hour surplus surface — the operational demand heatmap."""
    return jsonify(analytics.demand_patterns())


@ds_bp.route("/run-audit", methods=["GET", "POST"])
@require_auth
def run_audit():
    """Unsupervised trust audit over Matches/Users: flags accounts whose
    delivery timelines are statistically impossible."""
    if not _admin_allowed():
        return jsonify({"error": "Audit access is restricted to admins"}), 403
    return jsonify(fraud_detection.run_audit())


@ds_bp.post("/parse-food")
@require_auth
def parse_food():
    """Preview what the NLP engine extracts from a raw description — lets the
    frontend show parsed tags before the donor confirms a broadcast."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or data.get("food_description") or "")
    return jsonify({"parsed": nlp_engine.parse_food_description(text[:MAX_PARSE_CHARS])})
