"""Flask application factory."""

from __future__ import annotations

import logging

from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from app.api import BLUEPRINTS
from app.auth.jwt import AuthError
from app.config import Config


def create_app(config: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config)

    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Explicit origin allowlist rather than "*" -- the API accepts bearer
    # tokens, and a wildcard would let any page spend a user's session.
    CORS(
        app,
        resources={r"/api/*": {"origins": config.CORS_ORIGINS}},
        allow_headers=["Authorization", "Content-Type"],
        methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        max_age=3600,
    )

    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    # ---- error handling ---------------------------------------------------
    # Uniform JSON errors. A frontend that gets Flask's HTML 500 page where it
    # expected JSON fails in a far more confusing place than the actual bug.

    @app.errorhandler(AuthError)
    def _auth_error(exc: AuthError):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        return (
            jsonify({"error": exc.description, "status": exc.code}),
            exc.code or 500,
        )

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        app.logger.exception("Unhandled exception")
        if config.DEBUG:
            return jsonify({"error": str(exc), "type": type(exc).__name__}), 500
        return jsonify({"error": "Internal server error"}), 500

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "nurse-worry-layer-api",
                "version": "0.1.0",
                "health": "/api/v1/health",
            }
        )

    return app
