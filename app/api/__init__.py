"""Blueprint registry."""

from app.api import admin, dashboard, escalations, health, metrics, patients, worry, mobile, simulation

BLUEPRINTS = (
    health.bp,
    worry.bp,
    patients.bp,
    dashboard.bp,
    escalations.bp,
    metrics.bp,
    admin.bp,
    mobile.bp,
    simulation.bp,
)

__all__ = ["BLUEPRINTS"]
