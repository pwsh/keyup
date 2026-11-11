"""Operations endpoints: ingest, jobs, backends, settings, retention, users, metrics (§4.7)."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def test_admin_ingest_reports_the_ledger_and_runs(client, archive, read_token) -> None:
    body = client.get("/api/admin/ingest", headers=bearer(read_token)).json()
    status = body["status"]
    assert status["calls"] == len(archive.call_ids)
    assert status["ledger"]["ingested"] == len(archive.call_ids)
    assert status["audio_root"]
    assert status["lag_s"] > 0
    assert isinstance(body["runs"], list)


def test_ingest_file_ledger_is_listable_and_filterable(client, archive, read_token) -> None:
    files = client.get("/api/admin/ingest/files", headers=bearer(read_token)).json()
    assert files["total"] == len(archive.call_ids)
    assert files["items"][0]["status"] == "ingested"
    assert files["items"][0]["call_id"] is not None
    assert client.get("/api/admin/ingest/files", params={"status": "failed"}, headers=bearer(read_token)).json()["total"] == 0
    assert client.get("/api/admin/ingest/files", params={"q": "PATCH"}, headers=bearer(read_token)).json()["total"] == 1


def test_retrying_a_ledger_row_needs_admin(client, archive, write_token, admin_token, read_token) -> None:
    row = client.get("/api/admin/ingest/files", headers=bearer(read_token)).json()["items"][0]
    assert client.post(
        f"/api/admin/ingest/files/{row['id']}/retry", headers=bearer(write_token)
    ).status_code == 403
    retried = client.post(f"/api/admin/ingest/files/{row['id']}/retry", headers=bearer(admin_token))
    assert retried.status_code == 200
    assert retried.json()["status"] in ("ingested", "duplicate")


def test_rescan_is_queued_once(client, archive, admin_token, tmp_path) -> None:
    first = client.post(
        "/api/admin/ingest/rescan", params={"root": str(tmp_path)}, headers=bearer(admin_token)
    )
    assert first.status_code == 200
    assert first.json()["queued"] is True
    assert first.json()["root"] == str(tmp_path)


def test_jobs_list_carries_counts_in_a_header(client, archive, read_token) -> None:
    response = client.get("/api/admin/jobs", headers=bearer(read_token))
    assert response.status_code == 200
    import json

    counts = json.loads(response.headers["X-Job-Counts"])
    assert counts
    body = response.json()
    assert body["total"] == sum(counts.values())
    assert body["items"][0]["call_id"]


def test_requeue_moves_failed_jobs_back_to_the_queue(client, archive, session, admin_token) -> None:
    from app.models import Call, TranscriptionJob

    job = session.query(TranscriptionJob).first()
    job.status = "failed"
    job.attempts = 3
    job.last_error = "backend unreachable"
    session.commit()

    response = client.post("/api/admin/jobs/requeue", json={}, headers=bearer(admin_token))
    assert response.json() == {"requeued": 1}
    session.expire_all()
    assert session.get(TranscriptionJob, job.id).status == "queued"
    assert session.get(TranscriptionJob, job.id).attempts == 0
    assert session.get(Call, job.call_id).transcript_status == "queued"


def test_requeue_moves_every_parked_job_whatever_parked_it(client, archive, session, admin_token) -> None:  # noqa: ANN001
    """The operator's requeue is the override: it does not care why a job was parked.

    The automatic pass (§5.2) only ever moves ``parked_reason='backend_unavailable'``; this
    one is what a person reaches for when they have read the error and want it retried
    anyway, so a content failure has to move too.
    """
    from app.models import TranscriptionJob

    jobs = session.query(TranscriptionJob).limit(2).all()
    assert len(jobs) == 2
    outage, content = jobs
    outage.status, outage.attempts = "failed", 3
    outage.last_error, outage.parked_reason = "NoBackendAvailableError: …", "backend_unavailable"
    content.status, content.attempts = "failed", 3
    content.last_error, content.parked_reason = "RuntimeError: bad audio", None
    session.commit()

    response = client.post("/api/admin/jobs/requeue", json={}, headers=bearer(admin_token))
    assert response.json() == {"requeued": 2}
    session.expire_all()
    for job in (outage, content):
        row = session.get(TranscriptionJob, job.id)
        assert (row.status, row.attempts, row.last_error, row.parked_reason) == (
            "queued",
            0,
            None,
            None,
        )


def test_requeue_can_be_narrowed_to_named_jobs(client, archive, session, admin_token) -> None:  # noqa: ANN001
    from app.models import TranscriptionJob

    jobs = session.query(TranscriptionJob).limit(2).all()
    for job in jobs:
        job.status, job.attempts = "failed", 3
    session.commit()

    response = client.post(
        "/api/admin/jobs/requeue", json={"job_ids": [jobs[0].id]}, headers=bearer(admin_token)
    )
    assert response.json() == {"requeued": 1}
    session.expire_all()
    assert session.get(TranscriptionJob, jobs[0].id).status == "queued"
    assert session.get(TranscriptionJob, jobs[1].id).status == "failed"


def test_backends_report_what_the_worker_published(client, archive, admin_token) -> None:
    # The counters belong to the worker process; this endpoint reads its table, and asks for
    # `admin` because the same rows carry the operator's overrides.
    assert client.get("/api/admin/backends").status_code == 401
    backends = client.get("/api/admin/backends", headers=bearer(admin_token)).json()
    assert backends
    entry = next(b for b in backends if b["name"] == "whisper")
    assert entry["engine"] == "openai_compatible"
    assert entry["model"]
    # Nothing has published: health is unknown rather than a probe's verdict, and the
    # endpoint says why instead of showing a stale green dot.
    assert entry["healthy"] is None
    assert entry["detail"] == "worker not reporting"
    assert entry["queue_depth"] >= 0


def test_settings_expose_env_defaults_and_accept_overrides(client, archive, admin_token, read_token) -> None:
    rows = client.get("/api/admin/settings", headers=bearer(read_token)).json()
    by_key = {r["key"]: r for r in rows}
    assert by_key["stt_language"]["value"] == "en"
    assert by_key["stt_language"]["is_default"] is True
    assert by_key["stt_vad"]["kind"] == "bool"
    # Deployment-level values are deliberately not settable from the UI.
    assert "database_url" not in by_key
    assert "secret_key" not in by_key

    updated = client.put(
        "/api/admin/settings", json={"stt_language": "es"}, headers=bearer(admin_token)
    ).json()
    assert next(r for r in updated if r["key"] == "stt_language")["value"] == "es"
    assert next(r for r in updated if r["key"] == "stt_language")["is_default"] is False

    cleared = client.put(
        "/api/admin/settings", json={"stt_language": ""}, headers=bearer(admin_token)
    ).json()
    assert next(r for r in cleared if r["key"] == "stt_language")["is_default"] is True


def test_a_saved_bool_reads_back_as_lowercase(client, archive, admin_token, read_token) -> None:
    """A bool is JSONB, so `str(True)` is `"True"` — which the Switch (it compares against
    `"true"`) read as off, so a saved toggle looked like it cleared.  The API must serialize
    it lowercase, on both `value` and `default_value`, for every bool."""
    rows = client.get("/api/admin/settings", headers=bearer(read_token)).json()
    for row in rows:
        if row["kind"] == "bool":
            assert row["value"] in {"true", "false"}, row["key"]
            assert row["default_value"] in {"true", "false"}, row["key"]

    saved = client.put(
        "/api/admin/settings", json={"stt_normalize": "true"}, headers=bearer(admin_token)
    ).json()
    normalize = next(r for r in saved if r["key"] == "stt_normalize")
    assert normalize["value"] == "true"
    assert normalize["is_default"] is False


def test_settings_refuse_a_key_outside_the_whitelist(client, archive, admin_token) -> None:
    response = client.put(
        "/api/admin/settings", json={"database_url": "postgres://x"}, headers=bearer(admin_token)
    )
    assert response.status_code == 422
    assert "database_url" in response.json()["detail"]


def test_settings_reject_a_value_of_the_wrong_type(client, archive, admin_token) -> None:
    response = client.put(
        "/api/admin/settings", json={"transcribe_max_attempts": "many"}, headers=bearer(admin_token)
    )
    assert response.status_code == 422


def test_settings_only_list_keys_something_actually_reads(client, archive, read_token) -> None:
    """A knob that needed a restart to take effect would be worse than no knob."""
    keys = {r["key"] for r in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert "auth_require_read" in keys
    # These were settable but nothing ever read the override back.
    assert keys.isdisjoint({"stt_batch_size", "api_page_size_max", "ingest_watch", "live_status_seconds"})


def test_a_database_override_closes_the_archive_to_anonymous_readers(
    client, archive, admin_token
) -> None:
    """The whole point of H3: a row in `settings` changes behavior, not just the UI."""
    assert client.get("/api/calls").status_code == 200

    client.put(
        "/api/admin/settings", json={"auth_require_read": "true"}, headers=bearer(admin_token)
    ).raise_for_status()
    try:
        assert client.get("/api/calls").status_code == 401
        # …and a credential still gets in.
        assert client.get("/api/calls", headers=bearer(admin_token)).status_code == 200
    finally:
        client.put(
            "/api/admin/settings", json={"auth_require_read": ""}, headers=bearer(admin_token)
        )
    assert client.get("/api/calls").status_code == 200


def test_retention_policies_are_crud(client, archive, admin_token, read_token) -> None:
    assert client.get("/api/admin/retention/policies", headers=bearer(read_token)).json() == []
    created = client.post(
        "/api/admin/retention/policies",
        json={"scope": "global", "keep_days": 365, "keep_audio_days": 30},
        headers=bearer(admin_token),
    ).json()
    assert len(created) == 1
    policy = created[0]
    assert policy["scope_label"] == "Everything"
    assert policy["keep_audio_days"] == 30

    # The shorter path the SPA uses is the same handler.
    assert client.get("/api/admin/retention", headers=bearer(read_token)).json() == created

    updated = client.post(
        "/api/admin/retention/policies",
        json={"id": policy["id"], "scope": "global", "keep_days": 90},
        headers=bearer(admin_token),
    ).json()
    assert updated[0]["keep_days"] == 90

    assert client.delete(
        f"/api/admin/retention/policies/{policy['id']}", headers=bearer(admin_token)
    ).status_code == 204
    assert client.get("/api/admin/retention/policies", headers=bearer(read_token)).json() == []


def test_retention_run_answers_501_without_the_module(client, archive, admin_token, monkeypatch) -> None:
    """`app.retention` is the worker's; the API says 501 rather than pretending."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # noqa: ANN001, ANN202
        if name == "app.retention":
            raise ImportError("not in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    response = client.post("/api/admin/retention/run", headers=bearer(admin_token))
    assert response.status_code == 501
    assert "retention" in response.json()["detail"]


def test_tokens_are_minted_once_and_revocable(client, archive, admin_token) -> None:
    created = client.post(
        "/api/admin/tokens",
        json={"label": "nightly-pull", "scopes": ["read"]},
        headers=bearer(admin_token),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith("kup_")
    assert body["scopes"] == ["read"]

    listed = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    minted = next(t for t in listed if t["label"] == "nightly-pull")
    assert "token" not in minted  # never shown again
    assert minted["prefix"].startswith("kup_")

    # It works…
    assert client.get("/api/auth/me", headers=bearer(body["token"])).status_code == 200
    # …until it is revoked.
    assert client.post(
        f"/api/admin/tokens/{minted['id']}/revoke", headers=bearer(admin_token)
    ).status_code == 204
    assert client.get("/api/auth/me", headers=bearer(body["token"])).status_code == 401


def _mint(client, admin_token: str, label: str, scopes: list[str] | None = None) -> dict:  # noqa: ANN001
    created = client.post(
        "/api/admin/tokens",
        json={"label": label, "scopes": scopes or ["read"]},
        headers=bearer(admin_token),
    )
    assert created.status_code == 201
    return created.json()


def test_a_revoked_token_can_be_deleted_but_a_live_one_cannot(client, archive, admin_token) -> None:
    """Revoking keeps the row; deleting is the second, deliberate act that removes it.

    The two-step is the whole design: a revoked row is the audit trail, and an admin list
    that fills up with dead keys is the reason to sweep it — but never in one click on a
    key something might still be using.
    """
    minted = _mint(client, admin_token, "sweepable")

    # Still live: the API says so and names the cure rather than deleting anything.
    refused = client.delete(f"/api/admin/tokens/{minted['id']}", headers=bearer(admin_token))
    assert refused.status_code == 409
    assert "revoke it first" in refused.json()["detail"]
    listed = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    assert any(t["id"] == minted["id"] for t in listed)

    assert client.post(
        f"/api/admin/tokens/{minted['id']}/revoke", headers=bearer(admin_token)
    ).status_code == 204
    assert client.delete(
        f"/api/admin/tokens/{minted['id']}", headers=bearer(admin_token)
    ).status_code == 204

    listed = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    assert not any(t["id"] == minted["id"] for t in listed)
    # And the row really is gone, so a second delete is a 404 rather than a silent success.
    assert client.delete(
        f"/api/admin/tokens/{minted['id']}", headers=bearer(admin_token)
    ).status_code == 404


def test_deleting_a_token_is_admin_only(client, archive, admin_token, write_token) -> None:
    minted = _mint(client, admin_token, "not-yours")
    client.post(f"/api/admin/tokens/{minted['id']}/revoke", headers=bearer(admin_token))
    assert client.delete(
        f"/api/admin/tokens/{minted['id']}", headers=bearer(write_token)
    ).status_code == 403
    assert client.post(
        f"/api/admin/tokens/{minted['id']}/revoke", headers=bearer(write_token)
    ).status_code == 403


def test_a_token_cannot_delete_itself(client, archive, admin_token) -> None:
    """A caller cannot pull the rug from under itself: authenticating proves it is not revoked.

    Deleting needs a revoked row, and a revoked token fails auth — so the request that would
    delete the caller's own key never reaches the handler with a credential.
    """
    admin = next(
        t
        for t in client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
        if not t["revoked_at"]
    )
    assert client.delete(
        f"/api/admin/tokens/{admin['id']}", headers=bearer(admin_token)
    ).status_code == 409  # live, so it is refused

    # Revoke it and the credential itself stops working, which is the other half of the guard.
    assert client.post(
        f"/api/admin/tokens/{admin['id']}/revoke", headers=bearer(admin_token)
    ).status_code == 204
    assert client.delete(
        f"/api/admin/tokens/{admin['id']}", headers=bearer(admin_token)
    ).status_code == 401


def test_deleting_a_token_unbinds_the_recorder_that_held_it(
    client, archive, admin_token, session  # noqa: ANN001
) -> None:
    """``recorders.token_id`` is a report, not a permission — so it is nulled, not a blocker.

    The recorder keeps its history and re-binds on its next contact with whatever token it
    is given; refusing the delete because a recorder once used the key would make the tidy
    impossible on exactly the rows most worth tidying.
    """
    from app.auth import create_api_token
    from app.models import Recorder

    row, raw = create_api_token(session, "recorder-key", ["upload"], created_by="tests")
    session.commit()
    client.get("/api/recorder/desired-state", params={"instance": "rec-y"}, headers=bearer(raw))

    listed = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    assert next(t for t in listed if t["id"] == row.id)["used_by_recorders"] == ["rec-y"]

    assert client.post(
        f"/api/admin/tokens/{row.id}/revoke", headers=bearer(admin_token)
    ).status_code == 204
    assert client.delete(f"/api/admin/tokens/{row.id}", headers=bearer(admin_token)).status_code == 204

    session.expire_all()
    recorder = session.query(Recorder).filter_by(instance_id="rec-y").one()
    assert recorder.token_id is None  # unbound, not deleted


def test_a_token_can_be_minted_with_an_expiry(client, archive, admin_token) -> None:
    """The API has always accepted ``expires_at``; the row has to carry it back out again.

    A recorder key handed to somebody for a weekend is the case this exists for, and a
    minted expiry that the listing does not report is one nobody can check afterwards.
    """
    created = client.post(
        "/api/admin/tokens",
        json={
            "label": "weekend-loan",
            "scopes": ["upload"],
            "expires_at": "2026-09-30T00:00:00Z",
        },
        headers=bearer(admin_token),
    )
    assert created.status_code == 201
    assert created.json()["expires_at"].startswith("2026-09-30T00:00:00")

    listed = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    minted = next(t for t in listed if t["label"] == "weekend-loan")
    assert minted["expires_at"].startswith("2026-09-30T00:00:00")

    # An expiry is optional, and a token minted without one has none.
    forever = client.post(
        "/api/admin/tokens", json={"label": "permanent"}, headers=bearer(admin_token)
    )
    assert forever.json()["expires_at"] is None


def test_a_token_row_names_the_recorders_holding_it(client, archive, admin_token, session) -> None:  # noqa: ANN001
    """``recorders.token_id`` is written on first contact; the listing is where it is read.

    It is what turns "revoking this token breaks something" from a guess into a sentence
    on the row.
    """
    from app.auth import create_api_token

    row, raw = create_api_token(session, "the-recorder", ["upload"], created_by="tests")
    session.commit()

    before = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    assert next(t for t in before if t["id"] == row.id)["used_by_recorders"] == []

    client.get("/api/recorder/desired-state", params={"instance": "rec-x"}, headers=bearer(raw))

    after = client.get("/api/admin/tokens", headers=bearer(admin_token)).json()
    assert next(t for t in after if t["id"] == row.id)["used_by_recorders"] == ["rec-x"]
    # Every other token still says nothing holds it.
    assert all(t["used_by_recorders"] == [] for t in after if t["id"] != row.id)


def test_users_can_be_created_and_edited(client, archive, admin_token) -> None:
    created = client.post(
        "/api/admin/users",
        json={"username": "operator", "password": "a-long-enough-password", "is_admin": True},
        headers=bearer(admin_token),
    )
    assert created.status_code == 201
    user = created.json()
    assert user["is_admin"] is True
    assert "admin" in user["scopes"]

    duplicate = client.post(
        "/api/admin/users",
        json={"username": "OPERATOR", "password": "another-password"},
        headers=bearer(admin_token),
    )
    assert duplicate.status_code == 409

    demoted = client.patch(
        f"/api/admin/users/{user['id']}", json={"is_admin": False}, headers=bearer(admin_token)
    ).json()
    assert demoted["is_admin"] is False
    assert "admin" not in demoted["scopes"]

    # The new password works.
    client.patch(
        f"/api/admin/users/{user['id']}", json={"password": "brand-new-password"},
        headers=bearer(admin_token),
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "brand-new-password"},
        headers={"X-Requested-With": "keyup", "Origin": "http://testserver"},
    )
    assert login.status_code == 200


def test_metrics_are_prometheus_text(client, archive, read_token) -> None:
    assert client.get("/api/metrics").status_code == 401
    response = client.get("/api/metrics", headers=bearer(read_token))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE keyup_calls_total gauge" in body
    assert f"keyup_calls_total {len(archive.call_ids)}" in body
    assert "keyup_transcription_jobs{status=" in body
    assert "keyup_ingest_lag_seconds " in body
    assert "keyup_api_requests_total{method=" in body


def test_status_reports_health_and_counts(client, archive, read_token) -> None:
    body = client.get("/api/status", headers=bearer(read_token)).json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["counts"]["calls"] == len(archive.call_ids)
    assert body["ingest"]["calls"] == len(archive.call_ids)
    assert body["uptime_s"] >= 0


# --------------------------------------------------------------------------------------
# upload flow control
# --------------------------------------------------------------------------------------


def test_pause_and_resume_backlog_flip_the_runtime_setting(client, archive, admin_token, read_token) -> None:  # noqa: ANN001, ARG001
    from app.ingest import flow

    flow.invalidate()
    paused = client.post("/api/admin/upload/pause-backlog", headers=bearer(admin_token))
    assert paused.status_code == 200
    body = paused.json()
    assert body["paused_backlog"] is True
    assert body["queue"]["accepting"]["backlog"] is False
    assert body["queue"]["accepting"]["live"] is True

    rows = {r["key"]: r for r in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert rows["upload_pause_backlog"]["value"] == "true"
    assert rows["upload_pause_backlog"]["is_default"] is False
    # The status probe agrees, which is what a monitor and the SPA badge read.
    assert client.get("/api/status").json()["queue"]["paused_backlog"] is True

    resumed = client.post("/api/admin/upload/resume-backlog", headers=bearer(admin_token))
    assert resumed.json()["paused_backlog"] is False
    assert resumed.json()["queue"]["accepting"]["backlog"] is True


def test_pausing_backlog_needs_the_admin_scope(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    # Anonymous is refused by the mutation guard before the scope check ever runs.
    assert client.post("/api/admin/upload/pause-backlog").status_code in (401, 403)
    assert client.post(
        "/api/admin/upload/pause-backlog", headers=bearer(write_token)
    ).status_code == 403


def test_the_upload_caps_are_settable_at_runtime(client, archive, admin_token) -> None:  # noqa: ANN001, ARG001
    updated = client.put(
        "/api/admin/settings",
        json={"upload_max_queue_backlog": "50", "upload_backlog_max_eta_seconds": "120"},
        headers=bearer(admin_token),
    ).json()
    by_key = {r["key"]: r for r in updated}
    assert by_key["upload_max_queue_backlog"]["value"] == "50"
    assert by_key["upload_max_queue_backlog"]["is_default"] is False
    assert by_key["upload_backlog_max_eta_seconds"]["value"] == "120.0"


def test_reconcile_stats_reports_the_pairs_and_the_rules(
    client, archive, session, settings, read_token  # noqa: ANN001
) -> None:
    """``GET /admin/reconcile/stats`` — how much of the archive is second copies."""
    from app.ingest.reconcile import reconcile_pair
    from app.models import Call

    primary = session.get(Call, archive.call_ids[0])
    other = session.get(Call, archive.call_ids[1])
    other.started_at = primary.started_at
    other.ended_at = primary.ended_at
    other.talkgroup_ids = list(primary.talkgroup_ids)
    other.audio_state = "purged"
    session.flush()
    reconcile_pair(session, other, primary, settings)
    session.commit()

    body = client.get("/api/admin/reconcile/stats", headers=bearer(read_token)).json()

    assert body["enabled"] is True
    assert body["pairs"] == 1
    assert body["duplicates"] == 1
    assert body["rules"] == {"audio": 1}
    assert body["by_source"]["file"] == {"primary": 1, "duplicate": 1}
    assert body["settings"]["clock_skew_seconds"] == 2.0
    assert body["last_pass_at"] is None


def test_admin_exports_reports_both_services_disabled(client, archive, read_token) -> None:  # noqa: ANN001, ARG001
    """The default the deployment ships with: both exporters off, no watermark, no rows."""
    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()

    services = {s["service"]: s for s in body["services"]}
    assert set(services) == {"broadcastify", "openmhz"}
    for entry in services.values():
        assert entry["enabled"] is False
        assert entry["enabled_at"] is None
        assert entry["uploaded"] == 0
        assert entry["pending"] == 0
        assert entry["last_upload_at"] is None
    assert body["max_attempts"] >= 1


def test_admin_exports_follows_the_runtime_switch(client, archive, admin_token, read_token) -> None:  # noqa: ANN001, ARG001
    """Flipping the setting through the Admin API is what the endpoint reports back."""
    put = client.put(
        "/api/admin/settings",
        json={"export_openmhz_enabled": "true"},
        headers=bearer(admin_token),
    )
    assert put.status_code == 200
    by_key = {row["key"]: row for row in put.json()}
    assert by_key["export_openmhz_enabled"]["value"] == "true"
    # …and the other one is untouched and still on its default.
    assert by_key["export_broadcastify_enabled"]["value"] == "false"
    assert by_key["export_broadcastify_enabled"]["is_default"] is True

    services = {s["service"]: s for s in client.get("/api/admin/exports", headers=bearer(read_token)).json()["services"]}
    assert services["openmhz"]["enabled"] is True
    assert services["broadcastify"]["enabled"] is False
    # The watermark is the worker's to stamp, not the API's: still None until it cycles.
    assert services["openmhz"]["enabled_at"] is None


def test_admin_exports_reports_the_selection_and_the_ledger(client, archive, admin_token, read_token) -> None:  # noqa: ANN001
    """Mode, systems, 24-hour tallies and the recent rows all come back on the status read."""
    put = client.put(
        "/api/admin/settings",
        json={
            "export_openmhz_talkgroup_mode": "allow",
            "export_openmhz_systems": "1,2",
        },
        headers=bearer(admin_token),
    )
    assert put.status_code == 200

    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    services = {s["service"]: s for s in body["services"]}
    assert services["openmhz"]["talkgroup_mode"] == "allow"
    assert services["openmhz"]["systems"] == [1, 2]
    assert services["broadcastify"]["talkgroup_mode"] == "all"
    assert services["openmhz"]["queue_depth"] == 0
    assert services["openmhz"]["counts_24h"] == {}
    assert body["recent"] == []
    # Nothing here may switch a service on.
    assert all(entry["enabled"] is False for entry in services.values())


def test_an_unknown_talkgroup_mode_is_refused(client, admin_token) -> None:  # noqa: ANN001
    """The enum is validated on write rather than silently stored and read as `all`."""
    response = client.put(
        "/api/admin/settings",
        json={"export_openmhz_talkgroup_mode": "sometimes"},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422
    assert "all, allow, deny" in response.json()["detail"]


def test_the_export_credentials_are_never_read_back(client, admin_token, read_token) -> None:  # noqa: ANN001
    """`secret` kind: stored, never returned — only whether one is set."""
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert rows["openmhz_api_key"]["kind"] == "secret"
    assert rows["openmhz_api_key"]["value"] == ""

    client.put("/api/admin/settings", json={"openmhz_api_key": "sekrit"}, headers=bearer(admin_token))
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert rows["openmhz_api_key"]["value"] == ""
    assert rows["openmhz_api_key"]["is_default"] is False


def test_testing_an_unknown_service_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    response = client.post("/api/admin/exports/pizza/test", headers=bearer(admin_token))
    assert response.status_code == 404


def test_testing_a_service_with_no_credentials_says_which_are_missing(client, admin_token) -> None:  # noqa: ANN001
    """No network is touched when nothing is configured, and nothing is ever uploaded."""
    response = client.post("/api/admin/exports/openmhz/test", headers=bearer(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "OPENMHZ_API_KEY" in body["detail"]
    assert body["checked"]


def test_retrying_a_parked_export(client, session, archive, admin_token, read_token) -> None:  # noqa: ANN001
    """A failed row goes back in the queue; an uploaded one is refused with a 409."""
    from app.models import CallExport

    failed = CallExport(
        call_id=archive.call_ids[0],
        service="openmhz",
        status="failed",
        attempts=5,
        last_error="openmhz returned HTTP 500",
    )
    uploaded = CallExport(
        call_id=archive.call_ids[1],
        service="openmhz",
        status="uploaded",
        attempts=1,
        uploaded_at=datetime.now(UTC),
    )
    session.add_all([failed, uploaded])
    session.commit()

    ok = client.post(
        f"/api/admin/exports/openmhz/ledger/{failed.id}/retry", headers=bearer(admin_token)
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "pending"
    assert ok.json()["attempts"] == 0
    assert ok.json()["call_id"] == archive.call_ids[0]

    refused = client.post(
        f"/api/admin/exports/openmhz/ledger/{uploaded.id}/retry", headers=bearer(admin_token)
    )
    assert refused.status_code == 409

    wrong_service = client.post(
        f"/api/admin/exports/broadcastify/ledger/{failed.id}/retry", headers=bearer(admin_token)
    )
    assert wrong_service.status_code == 404

    recent = client.get("/api/admin/exports", headers=bearer(read_token)).json()["recent"]
    assert {row["call_id"] for row in recent} == set(archive.call_ids[:2])
    assert any(row["talkgroup"] for row in recent)


def test_retrying_an_export_needs_admin(client, session, archive) -> None:  # noqa: ANN001
    from app.models import CallExport

    row = CallExport(call_id=archive.call_ids[0], service="openmhz", status="failed", attempts=5)
    session.add(row)
    session.commit()

    assert client.post(f"/api/admin/exports/openmhz/ledger/{row.id}/retry").status_code in (401, 403)
