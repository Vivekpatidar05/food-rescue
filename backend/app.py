"""
app.py — Flask application factory for the FoodRescue API.

Run from the backend/ directory:  python app.py
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()  # must run before any module reads os.getenv

from flask import Flask, jsonify
from flask_cors import CORS

import auth
import db
import ngo_routes
from admin_routes import admin_bp
from auth import auth_bp
from donor_routes import donor_bp
from ds_routes import ds_bp
from esg import esg_bp
from gamification import gamification_bp
from integrations import integrations_bp
from media_routes import media_bp
from ngo_routes import ngo_bp
from social_routes import social_bp
from stats_routes import stats_bp
from volunteer_routes import volunteer_bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("food-rescue")

# Big enough for a 4 MB proof photo as base64 (+ JSON overhead), small enough
# to shut down memory-exhaustion uploads.
MAX_CONTENT_LENGTH = 8 * 1024 * 1024

_workers_started = False


def _start_background_workers():
    """Start the 5-minute enforcement/expiry scheduler exactly once."""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    ngo_routes.start_enforcement_scheduler()


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    # Explicit origins only — never a wildcard in production.
    origins = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:5500").split(",")
        if origin.strip()
    ]
    CORS(app, resources={r"/*": {"origins": origins}}, supports_credentials=True)

    app.register_blueprint(auth_bp)
    app.register_blueprint(donor_bp)
    app.register_blueprint(ngo_bp)
    app.register_blueprint(volunteer_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(ds_bp)
    app.register_blueprint(gamification_bp)
    app.register_blueprint(social_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(integrations_bp)
    app.register_blueprint(esg_bp)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "service": "food-rescue-api",
                "time": db.serialize(db.utcnow()),
            }
        )

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(413)
    def payload_too_large(_error):
        return jsonify({"error": "Upload too large (max 8 MB request)"}), 413

    @app.errorhandler(500)
    def server_error(_error):
        return jsonify({"error": "Internal server error"}), 500

    db.initialize()
    auth.ensure_admin_account()
    _start_background_workers()
    logger.info("FoodRescue API initialized (CORS origins: %s)", ", ".join(origins))
    return app


if __name__ == "__main__":
    try:
        application = create_app()
    except db.DatabaseUnavailable as exc:
        # A dead database is an operator problem, not a Python problem — show
        # the fix, not a stack trace.
        logger.error("FoodRescue API cannot start.\n\n%s", exc)
        raise SystemExit(1)

    debug = os.getenv("FLASK_ENV", "production") == "development"
    port = int(os.getenv("PORT", "5000"))
    if debug:
        # use_reloader=False keeps the background scheduler thread singular.
        application.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
    else:
        try:
            # Production: a real WSGI server (pip install waitress). The Flask
            # dev server is single-threaded and not hardened for the internet.
            from waitress import serve

            logger.info("Serving with waitress on port %s", port)
            serve(application, host="0.0.0.0", port=port, threads=8)
        except ImportError:
            logger.warning(
                "waitress is not installed — falling back to the Flask dev "
                "server. Run: pip install waitress"
            )
            application.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
