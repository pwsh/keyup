"""Defining speech-to-text backends from the GUI: the rows, the rule, and the key.

Three properties this suite exists to hold:

* **the seeding rule** — ``STT_BACKENDS`` seeds the table only while it is empty, and after
  that the database wins.  Both halves matter: the first makes an upgrade invisible, and the
  second stops a stale ``.env`` on the host from quietly reverting an operator's edit.
* **the key is write-only** — accepted, stored, and never present in any response, however
  the row is fetched.
* **history is never broken** — deleting a backend that any transcript names keeps the row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ActivityEvent, SttBackendConfig, Transcript
from app.stt import config_store
from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def admin(token: str) -> dict[str, str]:
    return {**bearer(token), **CSRF_HEADERS}


def backend_body(**overrides) -> dict:  # noqa: ANN003
    body = {
        "name": "gpu-box",
        "kind": "openai_compatible",
        "base_url": "http://gpu-box:8000",
        "api_key": "sk-secret-value",
        "model": "Systran/faster-whisper-large-v3",
        "concurrency": 3,
        "priority": 5,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------------------
# the precedence rule
# --------------------------------------------------------------------------------------


def test_an_empty_table_still_runs_on_the_environment(session) -> None:  # noqa: ANN001
    """Nothing written yet: ``STT_BACKENDS`` is the configuration, exactly as before."""
    assert session.scalar(select(SttBackendConfig.name)) is None
    entries = config_store.entries(session)
    assert [entry["name"] for entry in entries] == ["whisper"]


def test_the_environment_seeds_only_while_the_table_is_empty(session) -> None:  # noqa: ANN001
    assert config_store.seed_from_env(session) == 1
    session.commit()
    row = session.get(SttBackendConfig, "whisper")
    assert row.source == "env"
    # A second pass writes nothing, whatever the environment says: the table now owns this.
    row.model = "an-operator-chose-this"
    session.commit()
    assert config_store.seed_from_env(session) == 0
    session.commit()
    assert session.get(SttBackendConfig, "whisper").model == "an-operator-chose-this"


def test_the_database_wins_over_the_environment(session) -> None:  # noqa: ANN001
    session.add(
        SttBackendConfig(name="local", kind="faster_whisper_local", model="small", priority=1)
    )
    session.commit()
    names = [entry["name"] for entry in config_store.entries(session)]
    assert names == ["local"], "STT_BACKENDS must not be consulted once a row exists"


def test_a_deleted_backend_leaves_the_routing_list(session) -> None:  # noqa: ANN001
    from datetime import UTC, datetime

    session.add(SttBackendConfig(name="keep", kind="faster_whisper_local", model="small"))
    session.add(
        SttBackendConfig(
            name="gone",
            kind="faster_whisper_local",
            model="small",
            deleted_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert [entry["name"] for entry in config_store.entries(session)] == ["keep"]


def test_the_version_advances_on_every_write(session) -> None:  # noqa: ANN001
    assert config_store.version(session) == 0
    assert config_store.bump(session) == 1
    assert config_store.bump(session) == 2
    session.commit()
    assert config_store.version(session) == 2


# --------------------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------------------


def test_creating_a_backend_seeds_the_environment_first(client, admin_token, session) -> None:  # noqa: ANN001
    """Adding the second backend must not be how you lose the first one."""
    response = client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    assert response.status_code == 201, response.text
    names = {row.name for row in session.scalars(select(SttBackendConfig))}
    assert names == {"whisper", "gpu-box"}
    assert session.get(SttBackendConfig, "whisper").source == "env"
    assert session.get(SttBackendConfig, "gpu-box").source == "db"


def test_a_created_backend_appears_on_the_page_and_bumps_the_version(
    client, admin_token, session
) -> None:  # noqa: ANN001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    page = client.get("/api/admin/stt/backends", headers=bearer(admin_token)).json()
    row = next(item for item in page["backends"] if item["name"] == "gpu-box")
    assert row["kind"] == "openai_compatible"
    assert row["priority"] == 5
    assert row["editable"] is True
    assert row["config_source"] == "db"
    assert page["config_version"] >= 1
    # Nothing has published a stats row, so the worker has applied nothing yet.
    assert page["applied_config_version"] is None
    assert page["from_environment"] is False


def test_the_api_key_is_stored_and_never_echoed(client, admin_token, session) -> None:  # noqa: ANN001
    created = client.post(
        "/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token)
    )
    assert "sk-secret-value" not in created.text
    assert created.json()["has_api_key"] is True
    assert "api_key" not in created.json(), "the field is absent, not blanked"

    page = client.get("/api/admin/stt/backends", headers=bearer(admin_token))
    assert "sk-secret-value" not in page.text
    assert '"api_key"' not in page.text

    # …but it really is stored, because the router needs it.
    assert session.get(SttBackendConfig, "gpu-box").api_key == "sk-secret-value"


def test_an_edit_keeps_the_key_unless_it_is_cleared(client, admin_token, session) -> None:  # noqa: ANN001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    body = backend_body(base_url="http://gpu-box:9000")
    body.pop("api_key")
    response = client.put(
        "/api/admin/stt/backends/gpu-box", json=body, headers=admin(admin_token)
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    row = session.get(SttBackendConfig, "gpu-box")
    assert row.base_url == "http://gpu-box:9000"
    assert row.api_key == "sk-secret-value", "an omitted key means 'leave it alone'"

    client.put(
        "/api/admin/stt/backends/gpu-box",
        json=backend_body(api_key=""),
        headers=admin(admin_token),
    )
    session.expire_all()
    assert session.get(SttBackendConfig, "gpu-box").api_key == ""


def test_an_edit_bumps_the_version_and_records_activity(client, admin_token, session) -> None:  # noqa: ANN001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    before = config_store.version(session)
    client.put(
        "/api/admin/stt/backends/gpu-box",
        json=backend_body(concurrency=8),
        headers=admin(admin_token),
    )
    session.expire_all()
    assert config_store.version(session) == before + 1
    assert session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "stt.backend_updated")
    ).one()


def test_a_backend_needs_a_model_and_a_url_for_its_kind(client, admin_token) -> None:  # noqa: ANN001
    no_model = client.post(
        "/api/admin/stt/backends", json=backend_body(model=""), headers=admin(admin_token)
    )
    assert no_model.status_code == 422
    no_url = client.post(
        "/api/admin/stt/backends", json=backend_body(base_url=""), headers=admin(admin_token)
    )
    assert no_url.status_code == 422
    # …but a local engine legitimately has no URL.
    local = client.post(
        "/api/admin/stt/backends",
        json=backend_body(name="cpu", kind="faster_whisper_local", base_url=""),
        headers=admin(admin_token),
    )
    assert local.status_code == 201, local.text


def test_a_duplicate_name_is_a_conflict(client, admin_token) -> None:  # noqa: ANN001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    again = client.post(
        "/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token)
    )
    assert again.status_code == 409


# --------------------------------------------------------------------------------------
# delete, and the history it must not break
# --------------------------------------------------------------------------------------


def test_deleting_an_unused_backend_removes_the_row(client, admin_token, session) -> None:  # noqa: ANN001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    response = client.delete("/api/admin/stt/backends/gpu-box", headers=admin(admin_token))
    assert response.status_code == 200, response.text
    assert response.json()["removed"] is True
    assert response.json()["retained"] is False
    session.expire_all()
    assert session.get(SttBackendConfig, "gpu-box") is None


def test_deleting_a_backend_transcripts_name_keeps_it(
    client, admin_token, session, archive
) -> None:  # noqa: ANN001, ARG001
    """The archive's history is the point of the archive; a delete must never cost it."""
    transcript = session.scalars(select(Transcript)).first()
    assert transcript is not None
    client.post(
        "/api/admin/stt/backends",
        json=backend_body(name=transcript.backend_name),
        headers=admin(admin_token),
    )
    response = client.delete(
        f"/api/admin/stt/backends/{transcript.backend_name}", headers=admin(admin_token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retained"] is True
    assert body["removed"] is False
    assert body["transcripts"] == 1

    session.expire_all()
    row = session.get(SttBackendConfig, transcript.backend_name)
    assert row is not None, "the row that names the engine behind a transcript must survive"
    assert row.deleted_at is not None
    assert row.enabled is False
    # …and it is out of the routing list, which is what the operator actually asked for.
    assert transcript.backend_name not in {
        entry["name"] for entry in config_store.entries(session)
    }


def test_deleting_an_unknown_backend_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    assert client.delete("/api/admin/stt/backends/nope", headers=admin(admin_token)).status_code == 404


def test_recreating_a_retained_name_revives_the_same_row(
    client, admin_token, session, archive
) -> None:  # noqa: ANN001, ARG001
    transcript = session.scalars(select(Transcript)).first()
    name = transcript.backend_name
    client.post("/api/admin/stt/backends", json=backend_body(name=name), headers=admin(admin_token))
    client.delete(f"/api/admin/stt/backends/{name}", headers=admin(admin_token))
    again = client.post(
        "/api/admin/stt/backends",
        json=backend_body(name=name, base_url="http://elsewhere:8000"),
        headers=admin(admin_token),
    )
    assert again.status_code == 201, again.text
    session.expire_all()
    row = session.get(SttBackendConfig, name)
    assert row.deleted_at is None
    assert row.base_url == "http://elsewhere:8000"


# --------------------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------------------


class FakeHealth:
    def __init__(self, ok: bool, detail: str) -> None:
        self.ok = ok
        self.detail = detail


class FakeBackend:
    """Stands in for a transcription server: answers health, and 'transcribes'."""

    name = "gpu-box"

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    def health(self):  # noqa: ANN201
        return FakeHealth(self.healthy, "ok" if self.healthy else "connection refused")

    def transcribe(self, path, options):  # noqa: ANN001, ANN201, ARG002
        from app.stt.types import TranscriptResult

        return TranscriptResult(
            text="engine one responding",
            model="a-model",
            language="en",
            engine="openai_compatible",
            backend_name="gpu-box",
        )


@pytest.fixture
def fake_build(monkeypatch):  # noqa: ANN001, ANN201
    """Replace ``build_backends`` so a probe never touches a real server."""

    def install(backend) -> None:  # noqa: ANN001
        import app.stt.router as router_module

        monkeypatch.setattr(router_module, "build_backends", lambda *a, **k: [backend])  # noqa: ARG005

    return install


def test_a_probe_reports_health_and_a_timed_clip(
    client, admin_token, session, archive, fake_build
) -> None:  # noqa: ANN001, ARG001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    fake_build(FakeBackend())
    response = client.post("/api/admin/stt/backends/gpu-box/probe", headers=admin(admin_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    assert body["detail"] == "ok"
    assert body["health_s"] is not None
    assert body["clip_call_id"] is not None, "the archive's own audio is what gets timed"
    assert body["latency_s"] is not None
    assert body["rtf"] is not None
    assert body["text"] == "engine one responding"
    assert body["model"] == "a-model"
    assert session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "stt.backend_probed")
    ).one()


def test_a_failed_probe_is_a_result_not_an_error(
    client, admin_token, archive, fake_build
) -> None:  # noqa: ANN001, ARG001
    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    fake_build(FakeBackend(healthy=False))
    body = client.post("/api/admin/stt/backends/gpu-box/probe", headers=admin(admin_token)).json()
    assert body["ok"] is False
    assert body["detail"] == "connection refused"
    assert body["benchmark_skipped"] == "the health check failed"
    assert body["latency_s"] is None


def test_probing_an_unknown_backend_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    response = client.post("/api/admin/stt/backends/nope/probe", headers=admin(admin_token))
    assert response.status_code == 404


def test_a_local_engine_is_health_checked_but_not_benchmarked(
    client, admin_token, archive, fake_build
) -> None:  # noqa: ANN001, ARG001
    """A local engine runs *in the process that asks*, and the API is not that process.

    Timing one here would load the model into a web worker, contend with the transcription
    worker for the same device, and report a number for a path the API never takes.  The
    health check transfers; the benchmark does not.
    """
    created = client.post(
        "/api/admin/stt/backends",
        json=backend_body(name="on-the-box", kind="faster_whisper_local"),
        headers=admin(admin_token),
    )
    assert created.status_code in (200, 201), created.text
    fake_build(FakeBackend())
    body = client.post(
        "/api/admin/stt/backends/on-the-box/probe", headers=admin(admin_token)
    ).json()
    assert body["ok"] is True, body
    assert body["detail"] == "ok"
    assert body["health_s"] is not None
    assert body["benchmark_skipped"] == "local engines are probed by the worker"
    assert body["latency_s"] is None
    assert body["rtf"] is None
    assert body["clip_call_id"] is None, "no clip was transcribed at all"


def test_only_two_probes_run_at_once(client, admin_token, archive, fake_build) -> None:  # noqa: ANN001, ARG001
    """A probe holds a web worker for up to 45 seconds; an unbounded one takes the API down."""
    from app.api import admin_config

    client.post("/api/admin/stt/backends", json=backend_body(), headers=admin(admin_token))
    fake_build(FakeBackend())
    assert admin_config.PROBE_SLOTS.acquire(blocking=False)
    assert admin_config.PROBE_SLOTS.acquire(blocking=False)
    try:
        response = client.post(
            "/api/admin/stt/backends/gpu-box/probe", headers=admin(admin_token)
        )
        assert response.status_code == 429, response.text
        assert "already running" in response.json()["detail"]
    finally:
        admin_config.PROBE_SLOTS.release()
        admin_config.PROBE_SLOTS.release()
    # …and the slot is given back, so the next operator is not locked out.
    assert (
        client.post("/api/admin/stt/backends/gpu-box/probe", headers=admin(admin_token)).status_code
        == 200
    )


# --------------------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/admin/stt/backends", {"name": "x", "model": "m", "base_url": "http://x"}),
        ("put", "/api/admin/stt/backends/whisper", {"model": "m", "base_url": "http://x"}),
        ("delete", "/api/admin/stt/backends/whisper", None),
        ("post", "/api/admin/stt/backends/whisper/probe", None),
    ],
)
def test_every_write_needs_an_admin(client, write_token, method, path, body) -> None:  # noqa: ANN001
    call = getattr(client, method)
    kwargs = {"json": body} if body is not None else {}
    assert call(path, headers=CSRF_HEADERS, **kwargs).status_code == 401
    assert call(path, headers=admin(write_token), **kwargs).status_code == 403
