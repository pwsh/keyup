"""Health alerts through the API: the two new alert kinds save, and /api/status lists raised health."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.conftest import requires_db

pytestmark = requires_db


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_and_emergency_rules_save(client, admin_token) -> None:  # noqa: ANN001
    for body in (
        {"name": "Keyup health", "kind": "health", "pattern": "recorder_silent, ingest_lag"},
        {"name": "Emergencies", "kind": "emergency", "pattern": "law, fire, -1507"},
    ):
        created = client.post(
            "/api/alerts",
            json={**body, "channels": ["ntfy"], "channel_config": {"ntfy": {"topic": "keyup"}}},
            headers=bearer(admin_token),
        )
        assert created.status_code == 201, created.text
        assert created.json()["kind"] == body["kind"]
    kinds = sorted(rule["kind"] for rule in client.get("/api/alerts", headers=bearer(admin_token)).json())
    assert kinds == ["emergency", "health"]


def test_status_lists_the_raised_conditions(client, session) -> None:  # noqa: ANN001
    from app.models import HealthCondition

    now = datetime.now(UTC)
    session.add_all(
        [
            HealthCondition(
                key="recorder_silent:signal",
                check="recorder_silent",
                active=True,
                raised_at=now,
                checked_at=now,
                value=12.0,
                message="recorder signal has not reported for 12 min (threshold 5 min)",
            ),
            HealthCondition(key="ingest_lag", check="ingest_lag", active=False, checked_at=now, value=0.2),
        ]
    )
    session.commit()

    body = client.get("/api/status").json()
    assert [(h["key"], h["check"], h["value"]) for h in body["health"]] == [
        ("recorder_silent:signal", "recorder_silent", 12.0)
    ]
