"""Live spectrum: the recorder's ephemeral FFT ingest, the in-process hub, and the SSE relay.

The ingest authenticates with the same ``upload``-scoped Bearer token the status push uses;
the relay is admin-gated like the other operator SSE.  Nothing here touches the database:
frames live only in :mod:`app.spectrum`'s in-process cache and fan-out.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def _frame(instance: str = "rec-spec", tuner: str = "rtl-0", *, bins: int = 8, ts_ms: int = 1) -> dict:
    return {
        "instance_id": instance,
        "tuner_key": tuner,
        "ts_ms": ts_ms,
        "center_hz": 855_000_000,
        "span_hz": 10_000_000,
        "bins": bins,
        "db": [(-40 + i) for i in range(bins)],
    }


def _seed_tuner(client, upload_token, instance: str = "rec-spec", tuner: str = "rtl-0") -> None:
    """A status push creates the recorder/tuner rows the spectrum ingest requires to exist."""
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": instance, "tuners": [{"tuner_key": tuner}]},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-spectrum", ["upload"], created_by="tests")
    return raw


@pytest.fixture(autouse=True)
def _clear_hub():
    """Each test starts with an empty hub — it is process-wide, not per-request."""
    from app.spectrum import hub

    hub._cache.clear()
    hub._subscribers.clear()
    yield
    hub._cache.clear()
    hub._subscribers.clear()


# ----------------------------------------------------------------------- ingest auth ---


def test_spectrum_needs_upload_token(client) -> None:
    assert client.post("/api/recorder/spectrum", json=_frame()).status_code == 401


def test_spectrum_rejects_non_upload_scope(client, read_token) -> None:
    resp = client.post("/api/recorder/spectrum", json=_frame(), headers=bearer(read_token))
    assert resp.status_code == 403


# ------------------------------------------------------------------ ingest behavior ---


def test_spectrum_ingest_caches_known_tuner(client, upload_token) -> None:
    from app.spectrum import hub

    _seed_tuner(client, upload_token)
    resp = client.post("/api/recorder/spectrum", json=_frame(), headers=bearer(upload_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "accepted": 1, "skipped": 0}

    cached = hub.latest("rec-spec", "rtl-0")
    assert cached is not None
    assert cached["bins"] == 8
    assert cached["center_hz"] == 855_000_000
    assert len(cached["db"]) == 8


def test_spectrum_ignores_unknown_tuner(client, upload_token) -> None:
    from app.spectrum import hub

    # No status push has created this recorder/tuner → the frame has nowhere to belong.
    resp = client.post("/api/recorder/spectrum", json=_frame(instance="nope", tuner="ghost"), headers=bearer(upload_token))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "accepted": 0, "skipped": 1}
    assert hub.latest("nope", "ghost") is None


def test_spectrum_accepts_a_batch(client, upload_token) -> None:
    from app.spectrum import hub

    _seed_tuner(client, upload_token)
    batch = {"frames": [_frame(ts_ms=1), _frame(ts_ms=2), _frame(ts_ms=3)]}
    resp = client.post("/api/recorder/spectrum", json=batch, headers=bearer(upload_token))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "accepted": 3, "skipped": 0}
    # Last-frame-only: the cache holds the newest of the batch.
    assert hub.latest("rec-spec", "rtl-0")["ts_ms"] == 3


def test_spectrum_rejects_oversize_bins(client, upload_token) -> None:
    _seed_tuner(client, upload_token)
    frame = _frame()
    frame["bins"] = 4096  # over MAX_SPECTRUM_BINS
    frame["db"] = [0] * 4096
    assert client.post("/api/recorder/spectrum", json=frame, headers=bearer(upload_token)).status_code == 422


def test_spectrum_rejects_db_length_mismatch(client, upload_token) -> None:
    _seed_tuner(client, upload_token)
    frame = _frame(bins=8)
    frame["db"] = [0, 0, 0]  # length 3 != bins 8
    assert client.post("/api/recorder/spectrum", json=frame, headers=bearer(upload_token)).status_code == 422


def test_spectrum_rejects_out_of_range_db(client, upload_token) -> None:
    _seed_tuner(client, upload_token)
    frame = _frame(bins=3)
    frame["db"] = [0, 200, -5]  # 200 is outside a signed byte
    assert client.post("/api/recorder/spectrum", json=frame, headers=bearer(upload_token)).status_code == 422


def test_spectrum_rejects_oversize_batch(client, upload_token) -> None:
    _seed_tuner(client, upload_token)
    batch = {"frames": [_frame(ts_ms=i) for i in range(64)]}  # over MAX_SPECTRUM_FRAMES
    assert client.post("/api/recorder/spectrum", json=batch, headers=bearer(upload_token)).status_code == 422


def test_spectrum_rejects_long_instance_id(client, upload_token) -> None:
    frame = _frame(instance="i" * 129)
    assert client.post("/api/recorder/spectrum", json=frame, headers=bearer(upload_token)).status_code == 422


# ----------------------------------------------------------------------- the hub ---


def test_hub_publishes_to_matching_subscriber() -> None:
    """A posted frame reaches a subscriber on the same tuner and no other."""
    from app.spectrum import hub

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        want = hub.subscribe("rec-spec", "rtl-0", loop)
        other = hub.subscribe("rec-spec", "rtl-9", loop)  # different tuner
        try:
            hub.ingest(_frame(ts_ms=42))
            frame = await asyncio.wait_for(want.queue.get(), timeout=1.0)
            assert frame["ts_ms"] == 42
            assert other.queue.empty()  # the other tuner heard nothing
        finally:
            hub.unsubscribe(want)
            hub.unsubscribe(other)

    asyncio.run(scenario())


def test_hub_cache_is_bounded() -> None:
    from app.spectrum import MAX_CACHED, hub

    for i in range(MAX_CACHED + 20):
        hub.ingest(_frame(instance=f"rec-{i}", tuner="t0"))
    assert len(hub._cache) == MAX_CACHED
    # The oldest keys were evicted; the newest remains.
    assert hub.latest(f"rec-{MAX_CACHED + 19}", "t0") is not None
    assert hub.latest("rec-0", "t0") is None


# ----------------------------------------------------------------------- SSE relay ---


def test_spectrum_stream_requires_admin(client, read_token) -> None:
    params = {"instance": "rec-spec", "tuner": "rtl-0"}
    # The admin gate runs before the stream body, so an unauth/underscoped GET is refused.
    assert client.get("/api/admin/recorders/spectrum/stream", params=params).status_code == 401
    assert (
        client.get("/api/admin/recorders/spectrum/stream", params=params, headers=bearer(read_token)).status_code
        == 403
    )


class _FakeRequest:
    """A stand-in Request whose connection never drops, so the generator keeps yielding."""

    async def is_disconnected(self) -> bool:
        return False


def test_spectrum_stream_emits_cached_then_live_frame(client, upload_token) -> None:
    """The generator sends the cached frame on connect, then each newly posted frame.

    Driven directly rather than through the TestClient, which would block on the endless
    stream — the same reason the activity/recorder SSE tests only assert the admin gate.
    """
    from app.api.recorders import stream_spectrum
    from app.spectrum import hub

    _seed_tuner(client, upload_token)
    client.post("/api/recorder/spectrum", json=_frame(ts_ms=7), headers=bearer(upload_token))

    async def scenario() -> tuple[bytes, bytes, bytes]:
        resp = await stream_spectrum(_FakeRequest(), "rec-spec", "rtl-0")
        it = resp.body_iterator
        try:
            connected = await anext(it)
            cached = await anext(it)  # subscribes, then emits the last cached frame
            # A frame posted now reaches the live subscriber the generator created.
            hub.ingest(_frame(ts_ms=99))
            live = await anext(it)
            return connected, cached, live
        finally:
            await it.aclose()

    connected, cached, live = asyncio.run(scenario())
    assert connected == b": connected\n\n"
    assert b"event: spectrum" in cached
    assert b'"ts_ms":7' in cached.replace(b" ", b"")
    assert b'"ts_ms":99' in live.replace(b" ", b"")
