"""Alerts, notifications, saved searches and favorites (§4.7)."""

from __future__ import annotations

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


RULE = {
    "name": "Structure fires",
    "kind": "keyword",
    "pattern": "structure fire",
    "talkgroup_ids": [1, 2],
    "channels": ["ntfy"],
    "target": "https://ntfy.sh/keyup",
    "cooldown_s": 120,
}


def test_alert_rules_are_crud(client, archive, admin_token) -> None:
    created = client.post("/api/alerts", json=RULE, headers=bearer(admin_token))
    assert created.status_code == 201
    rule = created.json()
    assert rule["talkgroup_ids"] == [1, 2]
    assert rule["cooldown_s"] == 120
    assert rule["fire_count"] == 0

    edited = client.patch(
        f"/api/alerts/{rule['id']}",
        json={"name": "Structure fires", "kind": "regex", "pattern": "engine \\d+", "enabled": False},
        headers=bearer(admin_token),
    ).json()
    assert edited["kind"] == "regex"
    assert edited["enabled"] is False
    assert edited["talkgroup_ids"] == [1, 2]  # untouched keys survive

    assert len(client.get("/api/alerts", headers=bearer(admin_token)).json()) == 1
    assert client.delete(f"/api/alerts/{rule['id']}", headers=bearer(admin_token)).status_code == 204
    assert client.get("/api/alerts", headers=bearer(admin_token)).json() == []


def test_editing_a_rule_needs_admin_not_merely_write(client, archive, write_token) -> None:
    """H2: a rule is an outbound request this server makes on somebody's say-so."""
    assert client.post("/api/alerts", json=RULE, headers=bearer(write_token)).status_code == 403


def test_a_rule_never_hands_back_its_delivery_secrets(client, archive, session, admin_token) -> None:
    """H1: reads are open, so the webhook URL and its headers must not be readable."""
    from app.alerts import REDACTED

    created = client.post(
        "/api/alerts",
        json={
            **RULE,
            "channels": ["webhook"],
            "channel_config": {
                "webhook": {
                    "url": "https://hooks.example.test/T000/B111/s3cr3t",
                    "headers": {"Authorization": "Bearer hunter2"},
                    "retries": 2,
                }
            },
        },
        headers=bearer(admin_token),
    ).json()
    listed = client.get("/api/alerts", headers=bearer(admin_token)).json()[0]
    body = client.get("/api/alerts", headers=bearer(admin_token)).text
    assert "s3cr3t" not in body
    assert "hunter2" not in body
    assert listed["channel_config"]["webhook"]["url"] == REDACTED
    assert listed["channel_config"]["webhook"]["headers"] == REDACTED
    assert listed["channel_config"]["webhook"]["retries"] == 2  # only the secrets go
    assert listed["target"] == REDACTED

    # Sending the mask back — which is what the form does — keeps the stored secret.
    client.patch(
        f"/api/alerts/{created['id']}",
        json={"name": "Structure fires", "kind": "keyword", **listed},
        headers=bearer(admin_token),
    ).raise_for_status()
    from app.models import AlertRule

    session.expire_all()
    stored = session.get(AlertRule, created["id"])
    assert stored.channel_config["webhook"]["url"] == "https://hooks.example.test/T000/B111/s3cr3t"
    assert stored.channel_config["webhook"]["headers"] == {"Authorization": "Bearer hunter2"}


def test_a_rule_may_only_deliver_over_http(client, archive, admin_token) -> None:
    """H2: private addresses stay allowed; file:// and gopher:// do not."""
    response = client.post(
        "/api/alerts",
        json={**RULE, "target": "file:///etc/passwd"},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422
    assert "http" in response.json()["detail"]
    # A webhook on this LAN is the normal case and stays allowed.
    assert (
        client.post(
            "/api/alerts",
            json={**RULE, "name": "LAN", "target": "http://192.168.1.9:8123/api/webhook/x"},
            headers=bearer(admin_token),
        ).status_code
        == 201
    )


def test_a_rule_regex_is_compiled_and_capped_at_save(client, archive, admin_token) -> None:
    """M2: a pattern that cannot work is refused now, not on every transcript for ever."""
    broken = client.post(
        "/api/alerts",
        json={**RULE, "kind": "regex", "pattern": "engine ("},
        headers=bearer(admin_token),
    )
    assert broken.status_code == 422
    huge = client.post(
        "/api/alerts",
        json={**RULE, "kind": "regex", "pattern": "a" * 501},
        headers=bearer(admin_token),
    )
    assert huge.status_code == 422


def test_notifications_log_deliveries(client, archive, session, write_token) -> None:
    from app.models import AlertRule, Notification

    rule = AlertRule(name="Keyword", kind="keyword", pattern="fire", channels=["webhook"])
    session.add(rule)
    session.commit()
    session.add_all(
        [
            Notification(
                rule_id=rule.id,
                call_id=archive.transcribed_id,
                channel="webhook",
                subject="fire",
                body="Engine 1 responding to the structure fire",
                ok=True,
            ),
            Notification(rule_id=rule.id, channel="webhook", ok=False, error="connection refused"),
        ]
    )
    session.commit()

    body = client.get("/api/notifications", headers=bearer(write_token)).json()
    assert body["total"] == 2
    assert {n["status"] for n in body["items"]} == {"sent", "failed"}
    assert any(n["error"] == "connection refused" for n in body["items"])
    assert any(n["rule_name"] == "Keyword" for n in body["items"])
    assert client.get("/api/notifications", params={"rule": rule.id}, headers=bearer(write_token)).json()["total"] == 2

    # The rule's fire count comes from the log.
    assert client.get("/api/alerts", headers=bearer(write_token)).json()[0]["fire_count"] == 2


def test_saved_searches_round_trip(client, archive, write_token) -> None:
    created = client.post(
        "/api/saved-searches",
        json={"name": "Fire ground", "query": "q=fire&agency=EXAMPLEFD", "shared": True},
        headers=bearer(write_token),
    )
    assert created.status_code == 201
    assert created.json()["query"] == "q=fire&agency=EXAMPLEFD"

    listed = client.get("/api/saved-searches").json()
    assert [s["name"] for s in listed] == ["Fire ground"]

    assert client.delete(
        f"/api/saved-searches/{created.json()['id']}", headers=bearer(write_token)
    ).status_code == 204
    assert client.get("/api/saved-searches").json() == []


def test_favorites_are_idempotent(client, archive, write_token) -> None:
    talkgroup = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]
    first = client.put(
        "/api/favorites", json={"talkgroup_id": talkgroup["id"]}, headers=bearer(write_token)
    ).json()
    assert len(first) == 1
    assert first[0]["tgid"] == 1701
    assert first[0]["muted"] is False

    muted = client.put(
        "/api/favorites",
        json={"talkgroup_id": talkgroup["id"], "muted": True},
        headers=bearer(write_token),
    ).json()
    assert len(muted) == 1  # updated, not duplicated
    assert muted[0]["muted"] is True

    assert client.delete(
        f"/api/favorites/{talkgroup['id']}", headers=bearer(write_token)
    ).status_code == 204
    assert client.get("/api/favorites").json() == []


def test_favoriting_an_unknown_talkgroup_is_a_404(client, archive, write_token) -> None:
    response = client.put("/api/favorites", json={"talkgroup_id": 999999}, headers=bearer(write_token))
    assert response.status_code == 404


def test_alert_rules_and_the_delivery_log_need_a_credential(client, archive) -> None:  # noqa: ANN001, ARG001
    # A rule is the operator's own configuration — which channel it watches and where it
    # sends — not part of the archive a guest is shown, so reading one needs `read`.
    assert client.get("/api/alerts").status_code == 401
    assert client.get("/api/notifications").status_code == 401


def test_a_read_credential_is_enough_to_see_the_rules(client, archive, read_token) -> None:  # noqa: ANN001, ARG001
    assert client.get("/api/alerts", headers=bearer(read_token)).status_code == 200
    assert client.get("/api/notifications", headers=bearer(read_token)).status_code == 200
