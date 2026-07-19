"""
time_series.py — Predictive supply forecasting (Deliverable 3).

Trains one Holt-Winters (triple exponential smoothing) model per city zone on
the daily kg of surplus food broadcast there — weekly seasonality (period 7)
captures the weekend wedding/event spike the simulator baked into history —
and produces a 7-day forecast so volunteer drivers can be pre-positioned in
the zones about to generate the most surplus.

Model choice: statsmodels ExponentialSmoothing. Prophet needs a compiled Stan
backend that does not ship wheels for this interpreter (Python 3.14 on
Windows); Holt-Winters handles level + trend + weekly seasonality, which is
all a 7-day horizon needs. Zones with thin history fall back to a
seasonal-naive model (mean of the same weekday over recent weeks).

Two things separate this from a bare `.forecast()` call, and both matter for a
forecast anyone is meant to act on:

  * Prediction intervals. Every zone/day forecast ships an 80% interval derived
    from the model's in-sample residual spread, widened with the forecast
    horizon (sigma * sqrt(step)). A point estimate with no uncertainty band is
    a guess dressed up as a fact.
  * Walk-forward backtesting (`backtest`). We never claim accuracy we have not
    measured: the model is validated on held-out days it never saw, scored with
    WAPE / MAE / RMSE, and compared against a seasonal-naive baseline so the
    "skill score" states how much the model actually beats the naive guess.

Exposed via GET /api/predict-surplus and GET /api/forecast-accuracy
(ds_routes.py). Results are cached for 6 hours per process; pass ?refresh=1 to
force retraining.
"""

import math
import os
import threading
import warnings
from datetime import timedelta

import numpy as np
import pandas as pd

import db
from zones import assign_zone, get_zones

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    HAS_STATSMODELS = True
except ImportError:  # pragma: no cover
    HAS_STATSMODELS = False

HORIZON_DAYS = 7
TRAIN_WINDOW_DAYS = 240   # recent history only: keeps fits fast and current
MIN_HW_DAYS = 28          # below this, Holt-Winters is meaningless
SEASONAL_PERIOD = 7
CACHE_TTL = timedelta(hours=6)
DEFAULT_FLEET_SIZE = 20

# 80% prediction interval — z for the two-sided 80% band of a normal residual.
PI_Z = 1.2816
PI_LEVEL = 80

# Walk-forward backtest: hold out this many successive HORIZON-day blocks off
# the end of history, refit before each, and score the forecast on days the
# model never saw.
BACKTEST_FOLDS = 3

_cache = {"result": None, "at": None}
_cache_lock = threading.Lock()
_backtest_cache = {"result": None, "at": None}
_backtest_lock = threading.Lock()


def build_daily_frame():
    """Daily surplus kg per zone from ALL broadcasts (synthetic + live).
    Surplus generated is what we forecast, so expired batches count too."""
    rows = []
    cursor = db.food_batches.find(
        {"created_at": {"$ne": None}},
        {"created_at": 1, "quantity_kg": 1, "zone": 1, "pickup_location": 1},
    )
    for batch in cursor:
        zone = batch.get("zone")
        if not zone:
            coords = (batch.get("pickup_location") or {}).get("coordinates")
            if not coords:
                continue
            zone = assign_zone(coords[0], coords[1])
        stamp = pd.Timestamp(batch["created_at"])
        if stamp.tz is not None:
            stamp = stamp.tz_localize(None)
        rows.append(
            {"date": stamp.normalize(), "zone": zone,
             "kg": float(batch.get("quantity_kg") or 0.0)}
        )
    if not rows:
        return pd.DataFrame(columns=["date", "zone", "kg"])
    return pd.DataFrame(rows)


RAGGED_EDGE_MAX = HORIZON_DAYS   # never trim more than a week off the tail
RAGGED_EDGE_FRACTION = 0.3       # a day under 30% of typical volume is incomplete


def effective_index(frame, full_index):
    """Trim the 'ragged edge' — trailing days whose citywide volume is far below
    the recent norm because the data for them is still arriving (batches open,
    deliveries unconfirmed). Forecasting or scoring off a near-empty 'today'
    over-predicts every time; production pipelines anchor on the last COMPLETE
    day, so we do too. Data-driven (relative to the 90-day median), capped at a
    week — not a magic constant tuned to any one dataset."""
    if len(full_index) < 30:
        return full_index
    city = frame.groupby("date")["kg"].sum().reindex(full_index, fill_value=0.0)
    typical = float(city.iloc[-90:][city.iloc[-90:] > 0].median() or 0.0)
    if typical <= 0:
        return full_index
    end = len(city)
    floor = max(len(city) - RAGGED_EDGE_MAX, 0)
    for i in range(len(city) - 1, floor - 1, -1):
        if city.iloc[i] < RAGGED_EDGE_FRACTION * typical:
            end = i
        else:
            break
    return full_index[:max(end, 30)]


def _zone_series(frame, zone_name, full_index):
    """Daily kg series for one zone, zero-filled over the full date range."""
    zone_frame = frame[frame["zone"] == zone_name]
    series = zone_frame.groupby("date")["kg"].sum()
    return series.reindex(full_index, fill_value=0.0)


def _finite_std(values):
    """Population std, guarded against NaN/inf from degenerate inputs."""
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return 0.0
    std = float(np.std(arr))
    return std if math.isfinite(std) else 0.0


def _naive_resid_std(recent):
    """In-sample residual spread of the seasonal-naive rule: how far each recent
    day sits from its own weekday's mean. Drives the fallback's interval."""
    if len(recent) < SEASONAL_PERIOD + 1:
        return _finite_std(recent.to_numpy())
    weekday_mean = recent.groupby(recent.index.weekday).transform("mean")
    return _finite_std((recent - weekday_mean).to_numpy())


def _seasonal_naive(series, horizon):
    """Fallback: forecast each future day as the mean of that weekday over
    the last 6 weeks (or the overall mean when even that is missing).
    Returns (values, residual_std)."""
    recent = series.iloc[-42:]
    overall = float(recent.mean()) if len(recent) else 0.0
    values = []
    last_date = series.index[-1]
    for step in range(1, horizon + 1):
        weekday = (last_date + timedelta(days=step)).weekday()
        same_weekday = recent[recent.index.weekday == weekday]
        values.append(float(same_weekday.mean()) if len(same_weekday) else overall)
    return values, _naive_resid_std(recent)


def _forecast_zone(series, horizon):
    """(values, model_name, residual_std) for one zone's daily-kg series.

    residual_std is the in-sample one-step residual spread; the caller widens
    it by sqrt(step) to form each day's prediction interval."""
    train = series.iloc[-TRAIN_WINDOW_DAYS:]
    if HAS_STATSMODELS and len(train) >= MIN_HW_DAYS and float(train.std()) > 0.0:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # convergence chatter
                model = ExponentialSmoothing(
                    train.to_numpy(),
                    trend="add",
                    damped_trend=True,
                    seasonal="add",
                    seasonal_periods=SEASONAL_PERIOD,
                    initialization_method="estimated",
                ).fit(optimized=True)
            forecast = model.forecast(horizon)
            resid = train.to_numpy() - np.asarray(model.fittedvalues, dtype=float)
            return (
                [max(0.0, float(value)) for value in forecast],
                "holt-winters",
                _finite_std(resid),
            )
        except Exception:
            pass  # singular fits on degenerate series -> fall through
    values, resid_std = _seasonal_naive(series, horizon)
    return values, "seasonal-naive", resid_std


def _interval(step, resid_std):
    """Half-width of the 80% prediction interval `step` days ahead. Uncertainty
    compounds with the horizon, so the band widens as sqrt(step)."""
    return PI_Z * resid_std * math.sqrt(step)


def _allocate_fleet(zone_totals, fleet_size):
    """Split the volunteer fleet across zones proportionally to forecast
    surplus (largest-remainder rounding so the counts sum to fleet_size)."""
    total = sum(zone_totals.values())
    if total <= 0:
        return {zone: 0 for zone in zone_totals}
    quotas = {zone: fleet_size * kg / total for zone, kg in zone_totals.items()}
    allocation = {zone: int(quota) for zone, quota in quotas.items()}
    leftover = fleet_size - sum(allocation.values())
    for zone, _ in sorted(
        quotas.items(), key=lambda item: item[1] - int(item[1]), reverse=True
    )[:leftover]:
        allocation[zone] += 1
    return allocation


def _compute():
    frame = build_daily_frame()
    if frame.empty:
        return {
            "error": "No broadcast history to train on — run seed_data.py first.",
            "zones": [],
        }

    full_index = pd.date_range(frame["date"].min(), frame["date"].max(), freq="D")
    full_index = effective_index(frame, full_index)  # anchor on last complete day
    horizon_dates = [
        (full_index[-1] + timedelta(days=step)).strftime("%Y-%m-%d")
        for step in range(1, HORIZON_DAYS + 1)
    ]
    fleet_size = int(os.getenv("DS_FLEET_SIZE", str(DEFAULT_FLEET_SIZE)))

    zone_results, zone_totals, zone_resid_std = [], {}, {}
    for zone in get_zones():
        series = _zone_series(frame, zone["name"], full_index)
        values, model_name, resid_std = _forecast_zone(series, HORIZON_DAYS)
        zone_resid_std[zone["name"]] = resid_std
        total = round(sum(values), 1)
        zone_totals[zone["name"]] = total
        daily = []
        for step, (date, value) in enumerate(zip(horizon_dates, values), start=1):
            half = _interval(step, resid_std)
            daily.append(
                {
                    "date": date,
                    "predicted_kg": round(value, 1),
                    "lower_kg": round(max(0.0, value - half), 1),
                    "upper_kg": round(value + half, 1),
                }
            )
        zone_results.append(
            {
                "zone": zone["name"],
                "center": {"longitude": zone["lng"], "latitude": zone["lat"]},
                "model": model_name,
                "total_predicted_kg": total,
                "peak_day": max(daily, key=lambda day: day["predicted_kg"])["date"],
                "daily": daily,
            }
        )

    allocation = _allocate_fleet(zone_totals, fleet_size)
    zone_results.sort(key=lambda z: z["total_predicted_kg"], reverse=True)
    for rank, result in enumerate(zone_results, start=1):
        result["rank"] = rank
        result["suggested_volunteers"] = allocation[result["zone"]]

    # Citywide band: zone forecasts are treated as independent, so variances add
    # (sigma_city = sqrt(sum sigma_zone^2)) rather than the bounds summing — the
    # citywide interval is correctly tighter than stacking each zone's band.
    citywide_sigma = math.sqrt(sum(std * std for std in zone_resid_std.values()))
    citywide = []
    for index, date in enumerate(horizon_dates):
        point = sum(z["daily"][index]["predicted_kg"] for z in zone_results)
        half = _interval(index + 1, citywide_sigma)
        citywide.append(
            {
                "date": date,
                "predicted_kg": round(point, 1),
                "lower_kg": round(max(0.0, point - half), 1),
                "upper_kg": round(point + half, 1),
            }
        )

    return {
        "generated_at": db.serialize(db.utcnow()),
        "horizon_days": HORIZON_DAYS,
        "history_days": int(len(full_index)),
        "training_window_days": min(TRAIN_WINDOW_DAYS, int(len(full_index))),
        "fleet_size": fleet_size,
        "interval_level": PI_LEVEL,
        "citywide_daily": citywide,
        "zones": zone_results,
    }


def predict_surplus(refresh=False):
    """Cached 7-day per-zone surplus forecast. Thread-safe: Flask serves
    requests from a thread pool and training must run at most once."""
    with _cache_lock:
        fresh = (
            _cache["result"] is not None
            and _cache["at"] is not None
            and db.utcnow() - _cache["at"] < CACHE_TTL
        )
        if fresh and not refresh:
            return _cache["result"]
        result = _compute()
        _cache["result"], _cache["at"] = result, db.utcnow()
        return result


# ---------------------------------------------------------------------------
# Walk-forward backtesting — measure accuracy on days the model never saw.
# ---------------------------------------------------------------------------

def _wape(actual, predicted):
    """Weighted absolute percentage error = sum|a-p| / sum|a|. Robust for the
    intermittent, zero-heavy demand where plain MAPE blows up (divide-by-zero
    on quiet days). Reported as a percentage."""
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    denom = float(np.sum(np.abs(actual)))
    if denom <= 0:
        return 0.0
    return round(100.0 * float(np.sum(np.abs(actual - predicted))) / denom, 1)


def _rmse(actual, predicted):
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    return round(float(np.sqrt(np.mean((actual - predicted) ** 2))), 2)


def _mae(actual, predicted):
    actual, predicted = np.asarray(actual, float), np.asarray(predicted, float)
    return round(float(np.mean(np.abs(actual - predicted))), 2)


def _spearman(a, b):
    """Spearman rank correlation (Pearson on ranks). No scipy dependency."""
    ra, rb = pd.Series(a).rank(), pd.Series(b).rank()
    if ra.std() == 0 or rb.std() == 0:
        return 0.0
    return round(float(np.corrcoef(ra, rb)[0, 1]), 3)


def _backtest_compute():
    frame = build_daily_frame()
    if frame.empty:
        return {"error": "No broadcast history to backtest — run seed_data.py first.",
                "folds": []}

    full_index = pd.date_range(frame["date"].min(), frame["date"].max(), freq="D")
    full_index = effective_index(frame, full_index)  # score on complete days only
    zones = get_zones()
    zone_series = {z["name"]: _zone_series(frame, z["name"], full_index) for z in zones}

    # Accuracy is measured at the scale the forecast is consumed: citywide daily
    # surplus (sum over zones). Per-zone daily counts are dominated by sampling
    # noise; aggregating to the city exposes the weekly seasonal signal the model
    # is built to capture — and is the number provisioning decisions ride on.
    city_actual, city_model, city_naive = [], [], []
    rank_rhos = []   # per-fold agreement between predicted & actual zone ranking
    folds, overlay = [], []

    for fold in range(BACKTEST_FOLDS):
        cut = len(full_index) - fold * HORIZON_DAYS       # exclusive end of test
        test_start = cut - HORIZON_DAYS
        if test_start < MIN_HW_DAYS:                       # not enough to train
            break
        test_index = full_index[test_start:cut]
        fold_actual, fold_model, fold_naive = [], [], []

        for name, series in zone_series.items():
            train = series.iloc[:test_start]
            actual = series.iloc[test_start:cut].to_numpy()
            m_vals, _, _ = _forecast_zone(train, HORIZON_DAYS)
            n_vals, _ = _seasonal_naive(train, HORIZON_DAYS)
            fold_actual.append(actual)
            fold_model.append(np.asarray(m_vals, float))
            fold_naive.append(np.asarray(n_vals, float))

        # Does the model rank zones by 7-day surplus the way reality did? That
        # ranking — not daily point accuracy — is what the fleet split rides on.
        rank_rhos.append(
            _spearman(
                [a.sum() for a in fold_actual], [m.sum() for m in fold_model]
            )
        )

        stacked_actual = np.sum(fold_actual, axis=0)
        stacked_model = np.clip(np.sum(fold_model, axis=0), 0.0, None)
        stacked_naive = np.clip(np.sum(fold_naive, axis=0), 0.0, None)
        city_actual.extend(stacked_actual.tolist())
        city_model.extend(stacked_model.tolist())
        city_naive.extend(stacked_naive.tolist())

        folds.append(
            {
                "trained_through": full_index[test_start - 1].strftime("%Y-%m-%d"),
                "tested": [d.strftime("%Y-%m-%d") for d in test_index],
                "wape": _wape(stacked_actual, stacked_model),
                "mae": _mae(stacked_actual, stacked_model),
            }
        )
        if fold == 0:  # most-recent fold powers the predicted-vs-actual overlay
            overlay = [
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "actual_kg": round(float(a), 1),
                    "predicted_kg": round(float(p), 1),
                }
                for d, a, p in zip(test_index, stacked_actual, stacked_model)
            ]

    if not folds:
        return {"error": "Not enough history to backtest — seed more days first.",
                "folds": []}

    model_metrics = {
        "wape": _wape(city_actual, city_model),
        "mae": _mae(city_actual, city_model),
        "rmse": _rmse(city_actual, city_model),
    }
    baseline_metrics = {
        "wape": _wape(city_actual, city_naive),
        "mae": _mae(city_actual, city_naive),
        "rmse": _rmse(city_actual, city_naive),
    }
    # Skill vs the naive baseline: fraction of the baseline's error the model
    # removes. Positive = the model earns its complexity; <=0 = it does not.
    skill = 0.0
    if baseline_metrics["wape"] > 0:
        skill = round(1.0 - model_metrics["wape"] / baseline_metrics["wape"], 3)

    zone_rank_accuracy = round(float(np.mean(rank_rhos)), 3) if rank_rhos else 0.0

    return {
        "generated_at": db.serialize(db.utcnow()),
        "horizon_days": HORIZON_DAYS,
        "folds_evaluated": len(folds),
        "points_evaluated": len(city_actual),
        "evaluation_scale": "citywide daily surplus (kg/day)",
        "metric_help": {
            "wape": "Weighted abs % error on daily citywide kg (lower better)",
            "mae": "Mean abs error, kg/day citywide",
            "rmse": "Root mean squared error, kg/day citywide",
            "zone_rank_accuracy": "Spearman rho: how well the 7-day zone ranking "
            "matches reality (this is what the fleet split uses)",
            "skill_score": "Fraction of the naive baseline's error removed",
        },
        "model": {"name": "holt-winters", **model_metrics},
        "baseline": {"name": "seasonal-naive", **baseline_metrics},
        "skill_score": skill,
        "zone_rank_accuracy": zone_rank_accuracy,
        "folds": folds,
        "overlay": overlay,
    }


def backtest(refresh=False):
    """Cached walk-forward accuracy report. Same 6h TTL as the forecast."""
    with _backtest_lock:
        fresh = (
            _backtest_cache["result"] is not None
            and _backtest_cache["at"] is not None
            and db.utcnow() - _backtest_cache["at"] < CACHE_TTL
        )
        if fresh and not refresh:
            return _backtest_cache["result"]
        result = _backtest_compute()
        _backtest_cache["result"], _backtest_cache["at"] = result, db.utcnow()
        return result


if __name__ == "__main__":
    import json

    print(json.dumps(predict_surplus(refresh=True), indent=2))
    print(json.dumps(backtest(refresh=True), indent=2))
