"""
logistics.py — routing and ETA calculation via Google Maps Distance Matrix,
with a Haversine straight-line fallback when the API is unavailable.
"""

import math
import os

import requests
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv

import db

load_dotenv()

GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

# Conservative urban average used to estimate duration in fallback mode.
FALLBACK_SPEED_KMH = 25.0
REQUEST_TIMEOUT_S = 10


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle (straight-line) distance between two points, in km."""
    radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(a))


def _fallback_route(origin_coords, destination_coords):
    distance_km = haversine_km(
        origin_coords[0], origin_coords[1], destination_coords[0], destination_coords[1]
    )
    duration_min = max(1, int(round(distance_km / FALLBACK_SPEED_KMH * 60)))
    return {
        "distance_km": round(distance_km, 2),
        "duration_min": duration_min,
        "status": "fallback",
    }


def get_route_info(origin_coords, destination_coords):
    """Driving distance/duration between two (lat, lng) tuples.

    Returns {"distance_km": float, "duration_min": int, "status": "ok"|"fallback"}.
    Falls back to Haversine when the key is missing/invalid or the API returns
    no usable result.
    """
    if not GOOGLE_MAPS_KEY:
        return _fallback_route(origin_coords, destination_coords)

    params = {
        "origins": f"{origin_coords[0]},{origin_coords[1]}",
        "destinations": f"{destination_coords[0]},{destination_coords[1]}",
        "mode": "driving",
        "key": GOOGLE_MAPS_KEY,
    }
    try:
        response = requests.get(DISTANCE_MATRIX_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        data = response.json()
        if data.get("status") != "OK":
            return _fallback_route(origin_coords, destination_coords)
        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            # Covers ZERO_RESULTS, NOT_FOUND, etc.
            return _fallback_route(origin_coords, destination_coords)
        return {
            "distance_km": round(element["distance"]["value"] / 1000.0, 2),
            "duration_min": max(1, int(round(element["duration"]["value"] / 60.0))),
            "status": "ok",
        }
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return _fallback_route(origin_coords, destination_coords)


def _point_to_latlng(point):
    """GeoJSON Point stores [longitude, latitude]; routing wants (lat, lng)."""
    lng, lat = point["coordinates"]
    return (float(lat), float(lng))


def get_delivery_eta(batch_id, mover_id):
    """Resolve who is moving (NGO / volunteer / donor), compute their route,
    and persist it onto batch.route_info. Returns the route dict or None."""
    try:
        batch = db.food_batches.find_one({"_id": ObjectId(batch_id)})
        mover = db.users.find_one({"_id": ObjectId(mover_id)})
    except InvalidId:
        return None
    if batch is None or mover is None:
        return None

    pickup = _point_to_latlng(batch["pickup_location"])

    receiver_latlng = None
    if batch.get("receiver_id") is not None:
        receiver = db.users.find_one({"_id": batch["receiver_id"]})
        if receiver and receiver.get("location"):
            receiver_latlng = _point_to_latlng(receiver["location"])

    mode = batch.get("delivery_mode")
    if mode == "donor_self":
        # Donor starts at the pickup point and drives to the receiving NGO.
        route = get_route_info(pickup, receiver_latlng or pickup)
    elif mode == "volunteer" and receiver_latlng is not None:
        # Full volunteer journey: their location -> pickup -> receiving NGO.
        origin = _point_to_latlng(mover["location"]) if mover.get("location") else pickup
        leg1 = get_route_info(origin, pickup)
        leg2 = get_route_info(pickup, receiver_latlng)
        route = {
            "distance_km": round(leg1["distance_km"] + leg2["distance_km"], 2),
            "duration_min": leg1["duration_min"] + leg2["duration_min"],
            "status": "ok" if leg1["status"] == "ok" and leg2["status"] == "ok" else "fallback",
        }
    else:
        # NGO self-pickup (or missing receiver): mover's location -> pickup.
        origin = _point_to_latlng(mover["location"]) if mover.get("location") else pickup
        route = get_route_info(origin, pickup)

    db.food_batches.update_one({"_id": batch["_id"]}, {"$set": {"route_info": route}})
    return route
