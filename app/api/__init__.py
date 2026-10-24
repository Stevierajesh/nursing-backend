"""Blueprint registry."""

from app.api import admin, dashboard, escalations, health, metrics, patients, worry

BLUEPRINTS = (
    health.bp,
    worry.bp,
    patients.bp,
    dashboard.bp,
    escalations.bp,
    metrics.bp,
    admin.bp,
)

__all__ = ["BLUEPRINTS"]
