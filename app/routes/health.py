"""Operational health and readiness routes."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify

from app.db.pool import connection


bp = Blueprint("health", __name__)
logger = logging.getLogger(__name__)


@bp.get("/healthz")
def healthz() -> tuple[object, int]:
    """Return process liveness without checking external dependencies."""
    return jsonify({"status": "ok"}), 200


@bp.get("/readyz")
def readyz() -> tuple[object, int]:
    """Return readiness after verifying Postgres connectivity."""
    try:
        check_postgres()
    except Exception as exc:
        logger.warning("Readiness check failed dependency=postgres error=%s.", exc)
        return jsonify({"status": "not_ready", "checks": {"postgres": "error"}}), 503

    return jsonify({"status": "ready", "checks": {"postgres": "ok"}}), 200


def check_postgres() -> None:
    """Run a lightweight Postgres query for readiness checks."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
