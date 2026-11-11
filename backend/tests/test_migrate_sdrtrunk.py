"""``tools/migrate_sdrtrunk.py`` against a mock Keyup (stdlib ``http.server``).

The client's whole value is what it does when the server says *no*, so that is what is
tested: a 429 with ``Retry-After`` is slept off and retried, a duplicate is finished not
failed, ``accepting.backlog: false`` parks the run, and the ledger makes a second pass a
no-op.  The sleeps are injected, so the suite is not actually waiting anything out.

The archive tests build real ZIPs of real (tiny) MPEG frames, because the point of the
walk is that it reads members out of an archive without extracting it and gets the same
duration and alias out of the bytes that mutagen would — neither of which a stub proves.

The module is loaded by path because ``tools/`` is not a package — it is a single file
meant to be copied onto a recorder host and run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "migrate_sdrtrunk.py"


def load_tool():  # noqa: ANN201
    spec = importlib.util.spec_from_file_location("migrate_sdrtrunk", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before it is executed: its dataclasses resolve their annotations through
    # ``sys.modules[cls.__module__]``, which is None for a module loaded purely by path.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migrate = load_tool()


# --------------------------------------------------------------------------------------
# recordings that are really MP3s
# --------------------------------------------------------------------------------------

#: one MPEG-2 layer III frame: 32 kb/s, 22050 Hz, mono — byte for byte the header sdrtrunk
#: writes.  104 bytes long, 576 samples of it.
FRAME = bytes([0xFF, 0xF3, 0x40, 0xC4]) + b"\x00" * 100
FRAME_SECONDS = 576 / 22050


def _syncsafe(size: int) -> bytes:
    return bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])


def id3v24(title: str) -> bytes:
    """An ID3v2.4 tag with one ``TIT2``, the way sdrtrunk tags a call."""
    payload = b"\x03" + title.encode()
    frame = b"TIT2" + _syncsafe(len(payload)) + b"\x00\x00" + payload
    return b"ID3\x04\x00\x00" + _syncsafe(len(frame)) + frame


def mp3_bytes(frames: int = 8, title: str | None = None) -> bytes:
    return (id3v24(title) if title else b"") + FRAME * frames


#: four calls, deliberately out of order relative to the archives they live in
NAME_A = "20260829_015213_Example-System_Central_T-Control__TO_24801_FROM_9250041.mp3"
NAME_B = "20260828_055219_Example-System_Central_T-Control__TO_1701_FROM_101010_57_9831120.mp3"
NAME_C = "20260829_165914_Example-System_Central_T-Control__TO_1753.mp3"
NAME_D = "20260830_010101_Example-System_Central_T-Control__TO_1111_FROM_9200007.mp3"


# --------------------------------------------------------------------------------------
# the mock server
# --------------------------------------------------------------------------------------


class MockKeyup:
    """A Keyup that answers the two endpoints the client uses, on a real socket.

    ``script`` is a list of ``(status, body, headers)`` consumed one per upload; when it
    runs out every further upload succeeds.  ``accepting`` flips what ``/api/upload/flow``
    reports.
    """

    def __init__(self, script: list[tuple[int, str, dict[str, str]]] | None = None) -> None:
        self.script = list(script or [])
        self.accepting = True
        self.uploads: list[dict[str, str]] = []
        self.flow_polls = 0
        #: sha256 digests this Keyup already holds — what /api/upload/exists answers with
        self.stored: set[str] = set()
        #: every batch the client asked about, in order
        self.exists_asked: list[list[str]] = []
        #: 404 here is an older Keyup that has no such endpoint at all
        self.exists_status = 200
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: ANN002 - silence the test output
                pass

            def _send(self, status: int, body: str, headers: dict[str, str], ctype: str) -> None:
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                if not self.path.startswith("/api/upload/flow"):
                    self._send(404, "no", {}, "text/plain")
                    return
                with outer._lock:
                    outer.flow_polls += 1
                    accepting = outer.accepting
                body = json.dumps(
                    {
                        "live": 1,
                        "backlog": 2,
                        "audio_seconds": 30.0,
                        "eta_seconds": 15.0,
                        "accepting": {"live": True, "backlog": accepting},
                        "paused_backlog": not accepting,
                    }
                )
                self._send(200, body, {}, "application/json")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                if self.path.startswith("/api/upload/exists"):
                    asked = list((json.loads(raw) or {}).get("sha256") or [])
                    with outer._lock:
                        outer.exists_asked.append(asked)
                        status = outer.exists_status
                        present = [d for d in asked if d in outer.stored]
                    if status != 200:
                        self._send(status, "no", {}, "text/plain")
                        return
                    self._send(200, json.dumps({"present": present}), {}, "application/json")
                    return
                fields = dict(
                    part.split('name="', 1)[1].split('"', 1)[0:1] + [part.rsplit("\r\n\r\n", 1)[-1].strip()]
                    for part in raw.split("--")
                    if 'name="' in part and "filename=" not in part
                )
                with outer._lock:
                    outer.uploads.append(fields)
                    step = outer.script.pop(0) if outer.script else None
                if step is None:
                    self._send(200, "Call imported successfully.", {}, "text/plain")
                    return
                status, body, headers = step
                ctype = "application/json" if body.startswith("{") else "text/plain"
                self._send(status, body, headers, ctype)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):  # noqa: ANN204
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def tree(tmp_path) -> Path:  # noqa: ANN001
    """Three sdrtrunk-shaped recordings and the two things that must be ignored."""
    root = tmp_path / "recordings"
    (root / "20260829").mkdir(parents=True)
    names = [
        "20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3",
        "20260829_030253_Example-System_Central_T-Control__TO_1701.mp3",
        "20260829_165914_Example-System_Central_T-Control__TO_P1157-[1812--1561--1811]_FROM_101010_57_9700005.mp3",
    ]
    for index, name in enumerate(names):
        (root / "20260829" / name).write_bytes(mp3_bytes(frames=4 + index))
    (root / "20260829" / "20260829_030253_Example-System_Central_T-Control__TO_1701.raw.wav").write_bytes(b"raw")
    (root / "20260829" / "notes.txt").write_text("not a call")
    return root


@pytest.fixture
def archive(tmp_path) -> Path:  # noqa: ANN001
    """The share as it really is: a folder per channel, one ZIP per day inside it.

    ``Law/EXAMPLE EVENT 1`` is a level deeper than ``EC FIRE OPS`` and has a loose MP3 sitting
    beside its archive, so one walk covers nesting and the two source kinds mixed.
    """
    root = tmp_path / "share"
    (root / "EC FIRE OPS").mkdir(parents=True)
    (root / "Law" / "EXAMPLE EVENT 1").mkdir(parents=True)
    with zipfile.ZipFile(root / "EC FIRE OPS" / "20260829.zip", "w") as day:
        day.writestr(NAME_A, mp3_bytes(6, title='24801"EC FIRE OPS"'))
        day.writestr(NAME_C, mp3_bytes(9))
        day.writestr("readme.txt", "not a call")
    with zipfile.ZipFile(root / "Law" / "EXAMPLE EVENT 1" / "20260828.zip", "w") as day:
        day.writestr(NAME_B, mp3_bytes(12))
    (root / "Law" / "EXAMPLE EVENT 1" / NAME_D).write_bytes(mp3_bytes(15))
    return root


class FakeSleep:
    """Records what the client wanted to wait without waiting for it."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


# --------------------------------------------------------------------------------------
# filename parsing
# --------------------------------------------------------------------------------------


def test_a_plain_call_name_yields_the_talkgroup_and_the_radio() -> None:
    parts = migrate.parse_name(
        "20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
    )
    assert (parts.talkgroup, parts.source, parts.stamp) == (1753, 9250041, "20260829_015213")


def test_an_older_name_without_the_time_underscore_still_parses() -> None:
    # Older sdrtrunk builds butt the system straight against the time, with no
    # underscore separator (the bulk of the archived backlog looks like this).
    parts = migrate.parse_name(
        "20240120_012207Example_System_Central_T-Control__TO_14801_FROM_4802004.mp3"
    )
    assert (parts.talkgroup, parts.source, parts.stamp) == (14801, 4802004, "20240120_012207")


def test_a_call_with_no_from_has_no_source() -> None:
    parts = migrate.parse_name("20260829_030253_Example-System_Central_T-Control__TO_1701.mp3")
    assert (parts.talkgroup, parts.source) == (1701, None)


def test_a_fully_qualified_radio_reduces_to_its_id() -> None:
    parts = migrate.parse_name(
        "20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3"
    )
    assert parts.source == 9831120


def test_a_patch_call_sends_the_supergroup() -> None:
    parts = migrate.parse_name(
        "20260829_165914_Example-System_Central_T-Control__TO_P1157-[1812--1561--1811]_FROM_101010_57_9700005.mp3"
    )
    assert (parts.talkgroup, parts.patch) == (1157, 1157)


def test_the_collision_suffix_is_stripped() -> None:
    parts = migrate.parse_name(
        "20260828_082515_Example-System_Central_T-Control__TO_1701_FROM_9200007_V2.mp3"
    )
    assert (parts.talkgroup, parts.source) == (1701, 9200007)


def test_an_unrecognized_name_parses_to_nothing() -> None:
    assert migrate.parse_name("something-else.mp3").talkgroup is None


# --------------------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------------------


def test_the_walk_finds_the_mp3s_oldest_first_and_skips_raw_wav(tree) -> None:  # noqa: ANN001
    found = migrate.iter_recordings(tree)
    assert [p.name.split("_")[1] for p in found] == ["015213", "030253", "165914"]
    assert not any(p.name.endswith(".raw.wav") or p.suffix == ".txt" for p in found)


def test_newest_first_reverses_it(tree) -> None:  # noqa: ANN001
    found = migrate.iter_recordings(tree, newest_first=True)
    assert [p.name.split("_")[1] for p in found] == ["165914", "030253", "015213"]


# --------------------------------------------------------------------------------------
# uploading
# --------------------------------------------------------------------------------------


def test_a_clean_run_uploads_every_file_with_the_backlog_priority(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(base_url=server.url, key="kup_test", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(tree), workers=1)

    assert (stats.uploaded, stats.failed) == (3, 0)
    assert {u["priority"] for u in server.uploads} == {"backlog"}
    assert {u["key"] for u in server.uploads} == {"kup_test"}
    # The original filename has to survive: it is what the server's sdrtrunk parser reads.
    assert any("__TO_1753_FROM_9250041" in u["audioName"] for u in server.uploads)
    assert any(u.get("talkgroup") == "1753" for u in server.uploads)
    assert any(u.get("source") == "9250041" for u in server.uploads)
    # dateTime is the call END in an sdrtrunk name and the call START on the wire.
    assert all("dateTime" not in u for u in server.uploads)


def test_send_datetime_opts_into_sending_the_stamp(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(
            base_url=server.url, key="k", send_datetime=True, sleep=FakeSleep()
        )
        migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert server.uploads[0]["dateTime"].isdigit()


def test_a_system_label_is_sent_when_given(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(
            base_url=server.url, key="k", system="Example System", sleep=FakeSleep()
        )
        migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert server.uploads[0]["systemLabel"] == "Example System"


def test_a_429_is_slept_off_and_retried(tree) -> None:  # noqa: ANN001
    body = json.dumps({"error": "queue_full", "class": "backlog", "retry_after": 12})
    sleep = FakeSleep()
    with MockKeyup([(429, body, {"Retry-After": "12"})]) as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=sleep)
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)

    assert (stats.uploaded, stats.failed) == (1, 0)
    assert len(server.uploads) == 2  # refused once, accepted on the retry
    assert 12 in sleep.slept  # exactly what Retry-After asked for
    assert uploader.stats.waited_s == pytest.approx(12.0)


def test_a_507_is_retried_too(tree) -> None:  # noqa: ANN001
    sleep = FakeSleep()
    with MockKeyup([(507, "insufficient storage", {"Retry-After": "30"})]) as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=sleep)
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert stats.uploaded == 1
    assert 30 in sleep.slept


def test_a_retry_after_is_never_slept_longer_than_the_cap(tree) -> None:  # noqa: ANN001
    sleep = FakeSleep()
    with MockKeyup([(429, "queue full, retry later", {"Retry-After": "99999"})]) as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=sleep)
        migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert max(sleep.slept) == migrate.MAX_SLEEP


def test_a_5xx_backs_off_and_retries(tree) -> None:  # noqa: ANN001
    sleep = FakeSleep()
    with MockKeyup([(503, "nope", {}), (502, "nope", {})]) as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=sleep)
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert stats.uploaded == 1
    assert len(sleep.slept) == 2
    assert sleep.slept[1] > sleep.slept[0] / 2  # exponential, jittered


def test_a_duplicate_counts_as_done(tree) -> None:  # noqa: ANN001
    with MockKeyup([(200, "duplicate call rejected", {})]) as server:  # noqa: F841
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert (stats.duplicate, stats.uploaded, stats.failed) == (1, 0, 0)


def test_an_unauthorized_key_fails_without_retrying(tree) -> None:  # noqa: ANN001
    with MockKeyup([(401, "invalid key", {})]) as server:
        uploader = migrate.Uploader(base_url=server.url, key="wrong", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert stats.failed == 1
    assert len(server.uploads) == 1  # no point asking again


def test_the_run_gives_up_after_max_attempts(tree) -> None:  # noqa: ANN001
    script = [(429, "queue full, retry later", {"Retry-After": "5"})] * 10
    with MockKeyup(script) as server:
        uploader = migrate.Uploader(
            base_url=server.url, key="k", max_attempts=3, sleep=FakeSleep()
        )
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)
    assert stats.failed == 1
    assert len(server.uploads) == 3


def test_a_file_with_no_talkgroup_in_its_name_is_skipped(tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "odd"
    root.mkdir()
    (root / "mystery.mp3").write_bytes(b"\xff\xfb\x90d")
    with MockKeyup() as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(root), workers=1)
    assert stats.skipped == 1
    assert server.uploads == []


# --------------------------------------------------------------------------------------
# the flow gate
# --------------------------------------------------------------------------------------


def test_the_client_waits_while_the_server_is_not_accepting_backlog(tree) -> None:  # noqa: ANN001
    sleep = FakeSleep()
    with MockKeyup() as server:
        server.accepting = False

        def unpause(_seconds: float) -> None:
            sleep.slept.append(_seconds)
            server.accepting = True

        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=unpause)
        stats = migrate.run(uploader, migrate.iter_recordings(tree)[:1], workers=1)

    assert stats.uploaded == 1
    assert server.flow_polls >= 2  # noticed, waited, looked again
    assert uploader.stats.waited_s > 0


def test_an_unreachable_flow_endpoint_does_not_stop_the_run(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(
            base_url=server.url + "/nonexistent", key="k", sleep=FakeSleep()
        )
        assert uploader.flow() is None


# --------------------------------------------------------------------------------------
# the precheck (POST /api/upload/exists)
# --------------------------------------------------------------------------------------


def digests_of(recordings) -> list[str]:  # noqa: ANN001
    import hashlib

    return [hashlib.sha256(rec.read_bytes()).hexdigest() for rec in recordings]


def test_a_hash_the_server_already_holds_is_never_uploaded(tree, tmp_path) -> None:  # noqa: ANN001
    ledger_path = tmp_path / "ledger.jsonl"
    files = migrate.iter_recordings(tree)
    already, rest = digests_of(files)[0], digests_of(files)[1:]

    with MockKeyup() as server:
        server.stored = {already}
        uploader = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(uploader, files, workers=1)

    assert (stats.uploaded, stats.duplicate, stats.failed) == (2, 1, 0)
    assert stats.precheck_duplicate == 1
    assert len(server.uploads) == 2  # the third one's bytes were never sent
    assert server.exists_asked and set(server.exists_asked[0]) == {already, *rest}
    entries = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    skipped = [e for e in entries if e["sha256"] == already]
    assert skipped[0]["status"] == "duplicate"
    assert skipped[0]["detail"] == migrate.PRECHECK_DETAIL


def test_a_precheck_duplicate_is_done_for_the_next_run(tree, tmp_path) -> None:  # noqa: ANN001
    ledger_path = tmp_path / "ledger.jsonl"
    files = migrate.iter_recordings(tree)

    with MockKeyup() as server:
        server.stored = set(digests_of(files))
        first = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        assert migrate.run(first, files, workers=1).duplicate == 3

        # The ledger read back off disk: a resumed run skips them before it even hashes.
        second = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(second, files, workers=1)

    assert (stats.skipped, stats.uploaded, stats.duplicate) == (3, 0, 0)
    assert server.uploads == []


def test_an_older_server_without_the_endpoint_just_uploads(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        server.exists_status = 404
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(tree), workers=1)

    assert (stats.uploaded, stats.failed) == (3, 0)
    assert len(server.uploads) == 3
    assert len(server.exists_asked) == 1  # asked once, then stopped asking
    assert uploader.use_precheck is False


def test_a_precheck_error_falls_back_to_uploading(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        server.exists_status = 500
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(tree), workers=1)

    assert (stats.uploaded, stats.failed) == (3, 0)
    assert len(server.uploads) == 3


def test_no_precheck_never_asks(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        server.stored = set(digests_of(migrate.iter_recordings(tree)))
        uploader = migrate.Uploader(
            base_url=server.url, key="k", precheck=False, sleep=FakeSleep()
        )
        stats = migrate.run(uploader, migrate.iter_recordings(tree), workers=1)

    assert server.exists_asked == []
    assert stats.uploaded == 3
    assert stats.precheck_duplicate == 0


def test_the_precheck_survives_more_workers(tree) -> None:  # noqa: ANN001
    files = migrate.iter_recordings(tree)
    with MockKeyup() as server:
        server.stored = set(digests_of(files))
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        stats = migrate.run(uploader, files, workers=3)

    assert (stats.duplicate, stats.precheck_duplicate) == (3, 3)
    assert server.uploads == []


def test_the_cli_takes_no_precheck() -> None:
    assert migrate.build_parser().parse_args(["--no-precheck", "x"]).precheck is False
    assert migrate.build_parser().parse_args(["x"]).precheck is True


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------


def test_the_ledger_makes_a_second_run_a_no_op(tree, tmp_path) -> None:  # noqa: ANN001
    ledger_path = tmp_path / "ledger.jsonl"

    with MockKeyup() as server:
        first = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        migrate.run(first, migrate.iter_recordings(tree), workers=1)
        assert len(server.uploads) == 3

        second = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(second, migrate.iter_recordings(tree), workers=1)

    assert (stats.skipped, stats.uploaded) == (3, 0)
    assert len(server.uploads) == 3  # nothing was sent twice
    lines = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert len(lines) == 3
    assert {entry["status"] for entry in lines} == {"uploaded"}
    assert all(len(entry["sha256"]) == 64 for entry in lines)


def test_a_failure_is_retried_on_the_next_run(tree, tmp_path) -> None:  # noqa: ANN001
    ledger_path = tmp_path / "ledger.jsonl"
    with MockKeyup([(400, "incomplete call data: no audio", {})]) as server:
        first = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        migrate.run(first, migrate.iter_recordings(tree)[:1], workers=1)

        second = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(second, migrate.iter_recordings(tree)[:1], workers=1)
    assert stats.uploaded == 1


def test_a_torn_ledger_line_is_ignored(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"path": "/a.mp3", "status": "uploaded", "sha256": "x"}\n{"path": "/b.mp')
    ledger = migrate.Ledger(path)
    assert ledger.already_done(Path("/a.mp3"))
    assert not ledger.already_done(Path("/b.mp3"))


# --------------------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------------------


def test_dry_run_sends_nothing_and_exits_zero(tree, capsys) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        code = migrate.main(
            ["--url", server.url, "--key", "k", "--dry-run", "--quiet", str(tree)]
        )
        assert server.uploads == []
    assert code == 0
    assert "skipped=3" in capsys.readouterr().err


def test_limit_stops_early(tree) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        code = migrate.main(
            ["--url", server.url, "--key", "k", "--limit", "2", "--quiet", str(tree)]
        )
    assert code == 0
    assert len(server.uploads) == 2


def test_a_failure_exits_non_zero(tree) -> None:  # noqa: ANN001
    with MockKeyup([(401, "invalid key", {})] * 5) as server:
        code = migrate.main(["--url", server.url, "--key", "k", "--quiet", str(tree)])
    assert code == 1


def test_a_missing_key_is_an_argument_error(tree) -> None:  # noqa: ANN001
    assert migrate.main(["--quiet", str(tree)]) == 2


def test_the_rate_cap_paces_the_run(tree) -> None:  # noqa: ANN001
    sleep = FakeSleep()
    with MockKeyup() as server:
        uploader = migrate.Uploader(
            base_url=server.url, key="k", rate_per_min=60.0, sleep=sleep
        )
        migrate.run(uploader, migrate.iter_recordings(tree), workers=1)
    # 60/min is one a second: two of the three uploads wait for their slot.
    assert len([s for s in sleep.slept if s > 0]) >= 2


# --------------------------------------------------------------------------------------
# what the bytes say
# --------------------------------------------------------------------------------------


def test_mp3_duration_sums_the_frame_headers() -> None:
    assert migrate.mp3_duration(mp3_bytes(16)) == pytest.approx(16 * FRAME_SECONDS)


def test_mp3_duration_ignores_the_id3_tag_in_front_of_the_audio() -> None:
    tagged = migrate.mp3_duration(mp3_bytes(16, title='24801"EC FIRE OPS"'))
    assert tagged == pytest.approx(16 * FRAME_SECONDS)


def test_mp3_duration_is_none_when_there_is_no_frame_to_read() -> None:
    assert migrate.mp3_duration(b"not an mp3 at all") is None


def test_id3_alias_reads_the_quoted_name_out_of_tit2() -> None:
    assert migrate.id3_alias(mp3_bytes(2, title='24801"EC FIRE OPS"')) == "EC FIRE OPS"


def test_id3_alias_is_none_when_the_recording_carries_no_alias() -> None:
    assert migrate.id3_alias(mp3_bytes(2)) is None
    assert migrate.id3_alias(mp3_bytes(2, title="24801")) is None


def test_an_alias_printed_in_the_filename_is_read_with_the_talkgroup() -> None:
    parts = migrate.parse_name(
        "20260829_015213_Example-System_Central_T-Control__TO_1701-Fire-Dispatch_FROM_9250041.mp3"
    )
    assert (parts.talkgroup, parts.alias, parts.source) == (1701, "Fire Dispatch", 9250041)


# --------------------------------------------------------------------------------------
# walking the archives
# --------------------------------------------------------------------------------------


def test_the_walk_reads_the_members_of_every_day_zip(archive) -> None:  # noqa: ANN001
    found = migrate.iter_recordings(archive)
    assert [rec.name for rec in found] == [NAME_B, NAME_A, NAME_C, NAME_D]
    # the txt member of the archive is not a call, and neither is a directory entry
    assert all(rec.suffix == ".mp3" for rec in found)


def test_members_are_ordered_by_their_own_stamp_across_archives(archive) -> None:  # noqa: ANN001
    """NAME_B lives in the *second* archive and is still first: the order is the calls'."""
    oldest = migrate.iter_recordings(archive)
    newest = migrate.iter_recordings(archive, newest_first=True)
    assert [rec.name for rec in oldest] == [NAME_B, NAME_A, NAME_C, NAME_D]
    assert [rec.name for rec in newest] == list(reversed([rec.name for rec in oldest]))


def test_a_member_is_keyed_by_its_archive_and_carries_the_channel_folder(archive) -> None:  # noqa: ANN001
    by_name = {rec.name: rec for rec in migrate.iter_recordings(archive)}
    day = archive / "EC FIRE OPS" / "20260829.zip"
    assert by_name[NAME_A].key == f"{day}!{NAME_A}"
    assert by_name[NAME_A].folder == "EC FIRE OPS"
    with zipfile.ZipFile(day) as handle:
        assert by_name[NAME_A].crc32 == handle.getinfo(NAME_A).CRC
    assert by_name[NAME_A].size == len(mp3_bytes(6, title='24801"EC FIRE OPS"'))
    # a loose file beside an archive keeps the plain path as its key, as it always did
    assert by_name[NAME_D].key == str(archive / "Law" / "EXAMPLE EVENT 1" / NAME_D)
    assert by_name[NAME_D].folder == "EXAMPLE EVENT 1"


def test_a_member_is_read_without_extracting_the_archive(archive) -> None:  # noqa: ANN001
    member = next(rec for rec in migrate.iter_recordings(archive) if rec.name == NAME_C)
    blob = member.read_bytes()
    assert blob == mp3_bytes(9)  # exactly that member, nothing around it
    assert len(blob) < (archive / "EC FIRE OPS" / "20260829.zip").stat().st_size


def test_a_single_zip_is_a_source_of_its_own(archive) -> None:  # noqa: ANN001
    found = migrate.iter_recordings(archive / "EC FIRE OPS" / "20260829.zip")
    assert [rec.name for rec in found] == [NAME_A, NAME_C]


def test_a_glob_selects_the_archives_it_matches(archive) -> None:  # noqa: ANN001
    found = migrate.iter_recordings(str(archive / "*" / "2026*.zip"))
    assert [rec.name for rec in found] == [NAME_A, NAME_C]


def test_several_sources_are_walked_together(archive) -> None:  # noqa: ANN001
    found = migrate.iter_recordings(
        archive / "EC FIRE OPS" / "20260829.zip", archive / "Law"
    )
    assert [rec.name for rec in found] == [NAME_B, NAME_A, NAME_C, NAME_D]


def test_a_date_folder_is_not_taken_for_a_channel_name(tree) -> None:  # noqa: ANN001
    assert all(rec.folder is None for rec in migrate.iter_recordings(tree))


def test_a_corrupt_archive_is_reported_and_the_walk_carries_on(archive) -> None:  # noqa: ANN001
    (archive / "EC FIRE OPS" / "20260830.zip").write_bytes(b"PK\x03\x04 and then nonsense")
    errors: list[tuple] = []
    found = migrate.iter_recordings(archive, on_error=lambda p, m: errors.append((p, m)))
    assert len(found) == 4  # every good member still there
    assert [p.name for p, _ in errors] == ["20260830.zip"]


def test_a_source_that_does_not_exist_is_an_error_not_a_crash(tmp_path) -> None:  # noqa: ANN001
    errors: list[tuple] = []
    assert migrate.iter_recordings(tmp_path / "nope", on_error=lambda p, m: errors.append((p, m))) == []
    assert len(errors) == 1


# --------------------------------------------------------------------------------------
# uploading out of the archives
# --------------------------------------------------------------------------------------


def test_every_member_is_uploaded_under_its_own_name(archive) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        stats = migrate.run(uploader, migrate.iter_recordings(archive), workers=2)

    assert (stats.uploaded, stats.failed, stats.skipped) == (4, 0, 0)
    assert {u["audioName"] for u in server.uploads} == {NAME_A, NAME_B, NAME_C, NAME_D}
    assert {u["priority"] for u in server.uploads} == {"backlog"}
    assert stats.sent_bytes == sum(rec.size for rec in migrate.iter_recordings(archive))


def test_the_channel_folder_is_sent_only_when_the_call_has_no_alias(archive) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        migrate.run(uploader, migrate.iter_recordings(archive), workers=1)
    sent = {u["audioName"]: u for u in server.uploads}
    # NAME_A's TIT2 already says "EC FIRE OPS"; overwriting it with the folder would be a
    # downgrade, since talkgroupLabel outranks the ID3 alias on the push path.
    assert "talkgroupLabel" not in sent[NAME_A]
    assert sent[NAME_C]["talkgroupLabel"] == "EC FIRE OPS"
    assert sent[NAME_D]["talkgroupLabel"] == "EXAMPLE EVENT 1"


def test_a_corrupt_member_fails_alone(archive) -> None:  # noqa: ANN001
    """A bit-rotted member: ZipFile checks the CRC as it streams, so it never reaches the API."""
    day = archive / "Law" / "EXAMPLE EVENT 1" / "20260828.zip"
    with zipfile.ZipFile(day, "a") as handle:
        handle.writestr(NAME_D.replace("010101", "020202"), mp3_bytes(3) + b"CORRUPTME")
    raw = bytearray(day.read_bytes())
    raw[raw.index(b"CORRUPTME")] = ord("X")
    day.write_bytes(bytes(raw))

    ledger_path = archive.parent / "ledger.jsonl"
    with MockKeyup() as server:
        uploader = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(uploader, migrate.iter_recordings(archive), workers=1)

    assert (stats.uploaded, stats.failed) == (4, 1)
    assert len(server.uploads) == 4
    entries = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    failed = [e for e in entries if e["status"] == "failed"]
    assert len(failed) == 1 and "020202" in failed[0]["member"]
    assert "BadZipFile" in failed[0]["detail"]


def test_a_corrupt_archive_counts_as_failed_in_the_run(archive, tmp_path) -> None:  # noqa: ANN001
    (archive / "EC FIRE OPS" / "20260830.zip").write_bytes(b"PK\x03\x04 nonsense")
    ledger_path = tmp_path / "ledger.jsonl"
    with MockKeyup() as server:
        code = migrate.main(
            ["--url", server.url, "--key", "k", "--quiet", "--ledger", str(ledger_path),
             str(archive)]
        )
    assert code == 1  # the run reports it
    assert len(server.uploads) == 4  # and uploaded everything else anyway
    entries = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert [e["status"] for e in entries].count("failed") == 1


# --------------------------------------------------------------------------------------
# the ledger, across archives
# --------------------------------------------------------------------------------------


def test_the_ledger_resumes_a_zip_walk_member_by_member(archive, tmp_path) -> None:  # noqa: ANN001
    ledger_path = tmp_path / "ledger.jsonl"
    with MockKeyup() as server:
        first = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path),
            note="//nas/scanner", sleep=FakeSleep(),
        )
        migrate.run(first, migrate.iter_recordings(archive), workers=1)
        assert len(server.uploads) == 4

        second = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(second, migrate.iter_recordings(archive), workers=1)

    assert (stats.skipped, stats.uploaded) == (4, 0)
    assert len(server.uploads) == 4  # nothing was sent twice
    entries = {e["name"]: e for e in
               (json.loads(line) for line in ledger_path.read_text().splitlines())}
    day = archive / "EC FIRE OPS" / "20260829.zip"
    assert entries[NAME_A]["key"] == f"{day}!{NAME_A}"
    assert entries[NAME_A]["member"] == NAME_A
    assert entries[NAME_A]["path"] == str(day)
    assert entries[NAME_A]["folder"] == "EC FIRE OPS"
    assert entries[NAME_A]["size"] == len(mp3_bytes(6, title='24801"EC FIRE OPS"'))
    assert len(entries[NAME_A]["crc32"]) == 8
    assert len(entries[NAME_A]["sha256"]) == 64
    assert entries[NAME_A]["note"] == "//nas/scanner"
    # a loose file keeps the shape a ledger written before archives existed had
    assert entries[NAME_D]["member"] is None


def test_a_call_already_uploaded_loose_is_not_sent_again_once_it_is_zipped(tmp_path) -> None:  # noqa: ANN001
    """The sha256 is the identity; the key only says where it was found this time."""
    ledger_path = tmp_path / "ledger.jsonl"
    loose = tmp_path / "EC FIRE OPS"
    loose.mkdir()
    (loose / NAME_A).write_bytes(mp3_bytes(6))
    with MockKeyup() as server:
        first = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        migrate.run(first, migrate.iter_recordings(loose), workers=1)
        assert len(server.uploads) == 1

        (loose / NAME_A).unlink()
        with zipfile.ZipFile(loose / "20260829.zip", "w") as day:
            day.writestr(NAME_A, mp3_bytes(6))
        second = migrate.Uploader(
            base_url=server.url, key="k", ledger=migrate.Ledger(ledger_path), sleep=FakeSleep()
        )
        stats = migrate.run(second, migrate.iter_recordings(loose), workers=1)

    assert (stats.skipped, stats.uploaded) == (1, 0)
    assert len(server.uploads) == 1


def test_a_failed_member_is_not_marked_done_by_its_ledger_line(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"key": "/a.zip!x.mp3", "status": "failed", "sha256": "abc"}) + "\n"
        + json.dumps({"key": "/a.zip!y.mp3", "status": "uploaded", "sha256": "def"}) + "\n"
    )
    ledger = migrate.Ledger(path)
    assert not ledger.already_done("/a.zip!x.mp3")
    assert not ledger.already_done("/elsewhere.mp3", "abc")
    assert ledger.already_done("/a.zip!y.mp3")
    assert ledger.already_done("/elsewhere.mp3", "def")


# --------------------------------------------------------------------------------------
# dateTime
# --------------------------------------------------------------------------------------


def _epoch(stamp: str) -> int:
    return int(time.mktime(time.strptime(stamp, "%Y%m%d_%H%M%S")))


def test_datetime_is_end_subtracts_the_duration_it_reads_out_of_the_audio(archive) -> None:  # noqa: ANN001
    member = next(rec for rec in migrate.iter_recordings(archive) if rec.name == NAME_C)
    with MockKeyup() as server:
        start = migrate.Uploader(
            base_url=server.url, key="k", send_datetime=True, sleep=FakeSleep()
        )
        migrate.run(start, [member], workers=1)
        end = migrate.Uploader(
            base_url=server.url, key="k", send_datetime=True, datetime_is_end=True,
            sleep=FakeSleep(),
        )
        migrate.run(end, [member], workers=1)

    assert int(server.uploads[0]["dateTime"]) == _epoch("20260829_165914")
    duration = 9 * FRAME_SECONDS
    assert int(server.uploads[1]["dateTime"]) == _epoch("20260829_165914") - round(duration)


def test_datetime_is_end_implies_sending_it_at_all(archive) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        code = migrate.main(
            ["--url", server.url, "--key", "k", "--quiet", "--datetime-is-end", "--limit", "1",
             str(archive)]
        )
    assert code == 0
    assert server.uploads[0]["dateTime"].isdigit()


def test_nothing_is_sent_as_datetime_by_default(archive) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        uploader = migrate.Uploader(base_url=server.url, key="k", sleep=FakeSleep())
        migrate.run(uploader, migrate.iter_recordings(archive), workers=1)
    assert all("dateTime" not in u for u in server.uploads)


# --------------------------------------------------------------------------------------
# the CLI, over the real layout
# --------------------------------------------------------------------------------------


def test_dry_run_counts_every_member_and_uploads_none(archive, capsys) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        code = migrate.main(["--url", server.url, "--key", "k", "--dry-run", "--quiet",
                             str(archive)])
        assert server.uploads == []
    assert code == 0
    err = capsys.readouterr().err
    assert "4 recordings" in err and "in 2 archives" in err
    assert "skipped=4" in err


def test_the_source_note_lands_on_every_ledger_entry(archive, tmp_path) -> None:  # noqa: ANN001
    ledger_path = tmp_path / "ledger.jsonl"
    with MockKeyup() as server:
        migrate.main(["--url", server.url, "--key", "k", "--quiet", "--ledger", str(ledger_path),
                      "--source-note", "smb://nas/scanner mounted ro", str(archive)])
    entries = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert {e["note"] for e in entries} == {"smb://nas/scanner mounted ro"}


def test_two_sources_on_the_command_line(archive) -> None:  # noqa: ANN001
    with MockKeyup() as server:
        code = migrate.main([
            "--url", server.url, "--key", "k", "--quiet",
            str(archive / "EC FIRE OPS" / "20260829.zip"), str(archive / "Law"),
        ])
    assert code == 0
    assert len(server.uploads) == 4


def test_the_progress_line_shows_the_megabytes_and_an_eta(capsys) -> None:  # noqa: ANN001
    stats = migrate.Stats(uploaded=10, duplicate=1, failed=1, sent_bytes=12_500_000)
    migrate._progress_line(stats, 100, 60.0)
    line = capsys.readouterr().err
    assert "12/100" in line and "12 MB" in line and "MB/min" in line and "eta" in line
