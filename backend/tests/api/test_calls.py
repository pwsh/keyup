"""`GET /api/calls` and everything hanging off one call (§4.1)."""

from __future__ import annotations

import pytest

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def test_list_returns_every_ingested_call_with_both_clocks(client, archive) -> None:
    response = client.get("/api/calls")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(archive.call_ids)
    assert response.headers["X-Total-Count"] == str(body["total"])
    call = body["items"][0]
    # Every timestamp twice: UTC on the wire, local for anything a human reads (§3).
    assert call["started_at"].endswith("Z") or "+00:00" in call["started_at"]
    assert "Z" not in call["started_local"]
    assert call["audio_url"] == f"/api/calls/{call['id']}/audio"
    assert call["system_name"] == "Example System"


def test_a_row_carries_the_talkgroups_classification(client, archive) -> None:
    """``category`` and ``service_tag`` both ride along on the row the table draws: the
    Talkgroup join is already there for the alias, so this is a selected column, not a
    second query."""
    call = client.get("/api/calls", params={"talkgroup": 1701}).json()["items"][0]
    assert (call["category"], call["service_tag"]) == ("fire", "Fire Dispatch")

    other = client.get("/api/calls", params={"talkgroup": 1601}).json()["items"][0]
    assert other["service_tag"] is None


def test_default_sort_is_newest_first(client, archive) -> None:
    items = client.get("/api/calls").json()["items"]
    starts = [c["started_at"] for c in items]
    assert starts == sorted(starts, reverse=True)
    ascending = client.get("/api/calls", params={"sort": "started_at"}).json()["items"]
    assert [c["id"] for c in ascending] == [c["id"] for c in reversed(items)]


def test_unknown_sort_key_is_a_422(client, archive) -> None:
    response = client.get("/api/calls", params={"sort": "-nonsense"})
    assert response.status_code == 422
    assert "nonsense" in response.json()["detail"]


def test_talkgroup_filter_is_patch_aware(client, archive) -> None:
    """A patch call carries every member tgid, so filtering on a member finds it (§1.4)."""
    patch = client.get(f"/api/calls/{archive.patch_call_id}").json()
    assert patch["call_type"] == "patch"
    member = patch["talkgroup_ids"][1]
    assert member != patch["tgid"]

    found = client.get("/api/calls", params={"talkgroup": member}).json()
    assert archive.patch_call_id in [c["id"] for c in found["items"]]


def test_talkgroup_filter_is_the_on_air_number_and_never_the_primary_key(
    client, session, archive
) -> None:
    """A pk that happens to be another channel's tgid must not drag that channel in.

    ``?talkgroup=`` used to accept the primary key as well, and the two number spaces
    overlap: production had 43 talkgroups whose ``id`` equalled some other talkgroup's
    ``tgid``, so picking one channel in the SPA returned a second, unrelated one.
    """
    from app.models import Talkgroup

    call = client.get("/api/calls", params={"talkgroup": 1701}).json()["items"][0]
    pk = call["talkgroup_id"]

    # The collision, made real: a *different* channel whose on-air number is that pk.
    row = session.get(Talkgroup, pk)
    session.add(Talkgroup(system_id=row.system_id, tgid=pk, alias="COLLIDES WITH A PK"))
    session.commit()

    by_pk = client.get("/api/calls", params={"talkgroup": pk}).json()
    assert call["id"] not in [c["id"] for c in by_pk["items"]]
    # And the colliding channel has no traffic of its own, so the answer is empty.
    assert by_pk["items"] == []


def test_filters_narrow_the_set(client, archive) -> None:
    everything = client.get("/api/calls").json()["total"]

    assert client.get("/api/calls", params={"talkgroup": 1701}).json()["total"] < everything
    assert client.get("/api/calls", params={"call_type": "patch"}).json()["total"] == 1
    assert client.get("/api/calls", params={"transcript_status": "done"}).json()["total"] == 1
    assert client.get("/api/calls", params={"quality": "good"}).json()["total"] == 1
    assert client.get("/api/calls", params={"agency": "EXAMPLEFD"}).json()["total"] >= 1
    assert client.get("/api/calls", params={"category": "fire"}).json()["total"] >= 1
    assert client.get("/api/calls", params={"unit": "E1"}).json()["total"] == 1
    assert client.get("/api/calls", params={"min_duration": 0}).json()["total"] == everything
    assert client.get("/api/calls", params={"min_duration": 10_000}).json()["total"] == 0
    assert client.get("/api/calls", params={"starred": True}).json()["total"] == 0


def test_agency_filter_accepts_a_short_name(client, archive) -> None:
    by_code = client.get("/api/calls", params={"agency": "EXAMPLEFD"}).json()["total"]
    by_short = client.get("/api/calls", params={"agency": "ccfd"}).json()["total"]
    assert by_short == by_code >= 1


def test_time_of_day_and_dow_filters_use_the_local_clock(client, archive) -> None:
    call = client.get("/api/calls", params={"sort": "started_at"}).json()["items"][0]
    local_hour = int(call["started_local"][11:13])
    window = {"time_from": f"{local_hour:02d}:00", "time_to": f"{local_hour:02d}:59"}
    assert call["id"] in [c["id"] for c in client.get("/api/calls", params=window).json()["items"]]

    from datetime import datetime

    dow = int(datetime.fromisoformat(call["started_local"]).strftime("%w"))
    found = client.get("/api/calls", params={"dow": dow}).json()
    assert call["id"] in [c["id"] for c in found["items"]]
    assert client.get("/api/calls", params={"dow": 9}).status_code == 422


def test_date_filters_accept_a_bare_local_date(client, archive) -> None:
    """`date_to=<a date>` means through the end of that local day, not its midnight."""
    day = client.get("/api/calls", params={"sort": "started_at"}).json()["items"][0]["started_local"][:10]
    same_day = client.get("/api/calls", params={"date_from": day, "date_to": day}).json()
    assert same_day["total"] >= 1
    assert all(c["started_local"].startswith(day) for c in same_day["items"])


def test_duplicates_are_hidden_unless_asked_for(client, archive, session) -> None:
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    other = session.get(Call, archive.call_ids[1])
    other.duplicate_of = call.id
    session.commit()

    assert other.id not in [c["id"] for c in client.get("/api/calls").json()["items"]]
    with_dupes = client.get("/api/calls", params={"include_duplicates": True}).json()
    assert other.id in [c["id"] for c in with_dupes["items"]]


def test_has_audio_splits_present_from_metadata_only(client, archive, session) -> None:
    """`has_audio` is opt-in: it separates the rows whose recording is on hand from the
    metadata-only ones (an encrypted call, or one aged past retention)."""
    from app.models import Call

    everything = client.get("/api/calls").json()["total"]
    call = session.get(Call, archive.call_ids[0])
    call.audio_state = "purged"
    session.commit()

    # Absent, the filter changes nothing — every row is still listed.
    assert client.get("/api/calls").json()["total"] == everything

    present = client.get("/api/calls", params={"has_audio": True}).json()
    assert present["total"] == everything - 1
    assert call.id not in [c["id"] for c in present["items"]]

    metadata_only = client.get("/api/calls", params={"has_audio": False}).json()
    assert metadata_only["total"] == 1
    assert [c["id"] for c in metadata_only["items"]] == [call.id]


def test_quality_filter_selects_and_hides_levels(client, archive, session) -> None:
    """`quality` repeats for OR, so a single value picks one level and good+suspect is the
    "hide noise" view the calls toolbar offers."""
    from app.models import Call

    good, suspect, noise = (session.get(Call, cid) for cid in archive.call_ids[:3])
    good.transcript_quality = "good"
    suspect.transcript_quality = "suspect"
    noise.transcript_quality = "noise"
    session.commit()

    only_noise = {c["id"] for c in client.get("/api/calls", params={"quality": "noise"}).json()["items"]}
    assert noise.id in only_noise
    assert good.id not in only_noise and suspect.id not in only_noise

    hide_noise = {
        c["id"]
        for c in client.get(
            "/api/calls", params=[("quality", "good"), ("quality", "suspect")]
        ).json()["items"]
    }
    assert {good.id, suspect.id} <= hide_noise
    assert noise.id not in hide_noise


def test_cursor_pagination_walks_the_whole_set_without_repeats(client, archive) -> None:
    seen: list[int] = []
    cursor = None
    for _ in range(10):
        params = {"page_size": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/calls", params=params).json()
        seen.extend(c["id"] for c in page["items"])
        cursor = page["next_cursor"]
        if not cursor or not page["items"]:
            break
    assert sorted(seen) == sorted(archive.call_ids)
    assert len(seen) == len(set(seen))


def test_cursor_is_refused_on_a_sort_it_cannot_key(client, archive) -> None:
    cursor = client.get("/api/calls", params={"page_size": 1}).json()["next_cursor"]
    response = client.get("/api/calls", params={"page_size": 1, "sort": "-duration", "cursor": cursor})
    assert response.status_code == 422
    assert "cursor" in response.json()["detail"]

    assert client.get("/api/calls", params={"cursor": "not-a-cursor"}).status_code == 422


def test_facets_count_the_same_filtered_set(client, archive) -> None:
    facets = client.get("/api/calls/facets").json()
    assert facets["total"] == len(archive.call_ids)
    assert sum(b["count"] for b in facets["talkgroup"]) >= facets["total"]
    assert {b["key"] for b in facets["transcript_status"]}
    assert len(facets["hour"]) >= 1
    # Clicking a facet value must reproduce its count — which only works if the bucket key
    # is the **on-air tgid**, the one thing ``?talkgroup=`` speaks.  It used to be
    # ``calls.talkgroup_id``, the primary key, so a click asked about a different channel.
    bucket = facets["talkgroup"][0]
    assert int(bucket["key"]) in set(archive.by_tgid)
    listed = client.get("/api/calls", params={"talkgroup": int(bucket["key"])}).json()
    # ``>=`` and not ``==``: the bucket counts a call under its own talkgroup, while the
    # filter is patch-aware, so a tgid that is also a patch member finds one or two more.
    assert listed["total"] >= bucket["count"]


def test_detail_carries_the_transcript_neighbors_and_raw_metadata(client, archive) -> None:
    detail = client.get(f"/api/calls/{archive.transcribed_id}").json()
    assert detail["transcript"]["text"].startswith("Engine 1")
    assert len(detail["transcript"]["segments"]) == 2
    assert detail["transcript_count"] == 1
    assert detail["raw"]["file_name"].endswith(".mp3")
    assert detail["raw"]["sha256"]
    assert detail["talkgroup"]["tgid"] == detail["tgid"]
    # Three calls share talkgroup 1701, so this one has a neighbor on at least one side.
    assert detail["prev_call_id"] is not None or detail["next_call_id"] is not None


def test_a_discarded_call_serializes(client, archive, session) -> None:
    """A change that added `audio_state='discarded'` left `CallOut` naming three values.

    Every call the discard pass had taken then answered its own detail endpoint with an
    HTTP 500 out of pydantic — 13 % of recent calls, and exactly the ones an operator
    clicks when something sounds wrong.  Reproduced on two recorded calls.
    """
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    call.audio_state = "discarded"
    session.commit()

    detail = client.get(f"/api/calls/{call.id}")
    assert detail.status_code == 200
    assert detail.json()["audio_state"] == "discarded"
    # …and the list it is reached from, which serializes the same model.
    listed = client.get("/api/calls")
    assert listed.status_code == 200
    assert "discarded" in [c["audio_state"] for c in listed.json()["items"]]


def test_detail_404s_for_a_call_that_does_not_exist(client, archive) -> None:
    response = client.get("/api/calls/999999")
    assert response.status_code == 404
    assert response.json() == {"detail": "no call 999999"}


def test_audio_serves_ranges_and_an_etag(client, archive) -> None:
    call_id = archive.call_ids[0]
    full = client.get(f"/api/calls/{call_id}/audio")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, max-age=31536000, immutable"
    etag = full.headers["etag"]
    assert etag.startswith('"') and len(etag) == 66  # a quoted sha256
    size = int(full.headers["content-length"])

    partial = client.get(f"/api/calls/{call_id}/audio", headers={"Range": "bytes=0-1023"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 0-1023/{size}"
    assert len(partial.content) == 1024
    assert partial.content == full.content[:1024]

    suffix = client.get(f"/api/calls/{call_id}/audio", headers={"Range": "bytes=-512"})
    assert suffix.status_code == 206
    assert suffix.content == full.content[-512:]

    assert client.get(f"/api/calls/{call_id}/audio", headers={"If-None-Match": etag}).status_code == 304
    unsatisfiable = client.get(f"/api/calls/{call_id}/audio", headers={"Range": f"bytes={size + 1}-"})
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == f"bytes */{size}"


def test_audio_download_flag_sets_a_filename(client, archive) -> None:
    response = client.get(f"/api/calls/{archive.call_ids[0]}/audio", params={"download": 1})
    assert response.headers["content-disposition"].startswith("attachment; filename=")


def test_waveform_is_computed_then_cached(client, archive, tmp_path, monkeypatch) -> None:
    from app import waveform

    monkeypatch.setattr(waveform, "cache_dir", lambda settings=None: tmp_path)
    call_id = archive.call_ids[0]
    first = client.get(f"/api/calls/{call_id}/waveform")
    assert first.status_code == 200
    body = first.json()
    assert body["call_id"] == call_id
    assert len(body["peaks"]) == 200
    assert max(body["peaks"]) == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in body["peaks"])
    assert (tmp_path / f"{call_id}.json").is_file()
    assert client.get(f"/api/calls/{call_id}/waveform").json() == body


def test_transcripts_endpoint_lists_them(client, archive) -> None:
    rows = client.get(f"/api/calls/{archive.transcribed_id}/transcripts").json()
    assert len(rows) == 1
    assert rows[0]["is_primary"] is True
    assert rows[0]["rtf"] is not None  # duration / processing_ms


def test_patch_edits_notes_tags_and_star(client, archive, write_token) -> None:
    call_id = archive.call_ids[0]
    response = client.patch(
        f"/api/calls/{call_id}",
        json={"notes": "checked", "tags": ["review", "review", " keep "], "starred": True},
        headers=bearer(write_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["notes"] == "checked"
    assert body["tags"] == ["keep", "review"]  # de-duplicated, trimmed, sorted
    assert body["starred"] is True
    assert client.get("/api/calls", params={"tag": "keep"}).json()["total"] == 1
    assert client.get("/api/calls", params={"starred": True}).json()["total"] == 1


def test_transcript_correction_keeps_the_engine_text(client, archive, write_token) -> None:
    call_id = archive.transcribed_id
    corrected = "Engine 1 responding to the structure fire on Cherry Street, heavy smoke showing."
    response = client.patch(
        f"/api/calls/{call_id}", json={"transcript_text": corrected}, headers=bearer(write_token)
    )
    assert response.status_code == 200
    assert response.json()["transcript_text"] == corrected

    transcript = client.get(f"/api/calls/{call_id}/transcripts").json()[0]
    assert transcript["edited_by_user"] is True
    assert transcript["edited_text"] == corrected
    assert transcript["text"] == corrected
    assert "Cherry Court" in transcript["original_text"]  # the engine's output survives

    # …and the correction is searchable, because search_tsv is generated from the call.
    assert client.get("/api/search", params={"q": "Cherry Street"}).json()["total"] == 1


def test_transcribe_enqueues_a_job(client, archive, write_token, session) -> None:
    from app.models import TranscriptionJob

    call_id = archive.call_ids[0]
    session.query(TranscriptionJob).delete()
    session.commit()

    response = client.post(
        f"/api/calls/{call_id}/transcribe", json={"backend": "stt-gpu"}, headers=bearer(write_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    job = session.get(TranscriptionJob, body["job_id"])
    assert job.call_id == call_id
    assert job.backend_name == "stt-gpu"

    # A second request finds the open job rather than making another.
    again = client.post(f"/api/calls/{call_id}/transcribe", json={}, headers=bearer(write_token))
    assert again.json()["job_id"] is None
    assert "already queued" in again.json()["detail"]


def test_force_transcribe_bypasses_the_policy(client, archive, write_token, session) -> None:
    from app.models import Call, Talkgroup

    call = session.get(Call, archive.call_ids[0])
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.transcribe = False
    session.commit()

    refused = client.post(f"/api/calls/{call.id}/transcribe", json={}, headers=bearer(write_token))
    assert refused.json()["job_id"] is None
    forced = client.post(
        f"/api/calls/{call.id}/transcribe", json={"force": True}, headers=bearer(write_token)
    )
    assert forced.json()["job_id"] is not None


def test_delete_needs_admin_and_leaves_the_audio(client, archive, write_token, admin_token) -> None:
    call_id = archive.call_ids[0]
    path = client.get(f"/api/calls/{call_id}").json()["raw"]["file_path"]
    assert client.delete(f"/api/calls/{call_id}", headers=bearer(write_token)).status_code == 403
    assert client.delete(f"/api/calls/{call_id}", headers=bearer(admin_token)).status_code == 204
    assert client.get(f"/api/calls/{call_id}").status_code == 404

    from pathlib import Path

    from app.config import get_settings

    assert (Path(get_settings().audio_root) / path).is_file()


def test_playlist_is_an_extended_m3u(client, archive) -> None:
    response = client.get("/api/calls/playlist.m3u", params={"limit": 3})
    assert response.status_code == 200
    lines = response.text.strip().splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXTINF:")
    assert "/api/calls/" in lines[2] and lines[2].endswith("/audio")


def test_a_cookie_mutation_needs_the_csrf_header(client, archive, api_user, user_password) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    without = client.patch(f"/api/calls/{archive.call_ids[0]}", json={"starred": True})
    assert without.status_code == 403
    assert "X-Requested-With" in without.json()["detail"] or "x-requested-with" in without.json()["detail"]
    with_header = client.patch(
        f"/api/calls/{archive.call_ids[0]}", json={"starred": True}, headers=CSRF_HEADERS
    )
    assert with_header.status_code == 200


def test_audio_refuses_a_call_whose_path_escapes_the_archive(client, archive, session) -> None:
    """`calls.file_path` is data, and data does not get to name arbitrary files."""
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    call.file_path = "../../../etc/hostname"
    session.commit()

    assert client.get(f"/api/calls/{call.id}/audio").status_code == 404
    assert client.get(f"/api/calls/{call.id}/waveform").status_code == 404


def test_audio_is_gone_once_retention_has_purged_it(client, archive, session) -> None:
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    call.audio_state = "purged"
    session.commit()

    assert client.get(f"/api/calls/{call.id}/audio").status_code == 410


def test_the_content_disposition_filename_is_sanitized(client, archive, session) -> None:
    """L3: a stored filename must not be able to write its own header parameters."""
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    call.file_name = 'evil"; attachment; filename="owned.mp3'
    session.commit()

    disposition = client.get(f"/api/calls/{call.id}/audio").headers["content-disposition"]
    assert disposition.count('"') == 2
    assert "owned" in disposition and ";" not in disposition[disposition.index('"') :]


def test_a_cursor_page_reports_no_total(client, archive) -> None:
    """Counting the filtered set on every page of an infinite scroll is a full scan."""
    first = client.get("/api/calls", params={"page_size": 2}).json()
    assert first["total"] == len(archive.call_ids)
    assert first["total_capped"] is False

    second = client.get("/api/calls", params={"page_size": 2, "cursor": first["next_cursor"]})
    body = second.json()
    assert body["total"] is None
    assert "x-total-count" not in second.headers
    assert len(body["items"]) == 2


def test_a_cursor_cannot_continue_a_sort_it_does_not_encode(client, archive) -> None:
    """M8: the cursor is `(started_at, id)`; paging by recorded_at with it is a lie."""
    cursor = client.get("/api/calls", params={"page_size": 2}).json()["next_cursor"]
    refused = client.get("/api/calls", params={"cursor": cursor, "sort": "-recorded_at"})
    assert refused.status_code == 422
    assert "started_at" in refused.json()["detail"]
    assert client.get("/api/calls", params={"cursor": cursor, "sort": "-started_local"}).status_code == 200


def _walk_cursor(client, params: dict) -> list[int]:  # noqa: ANN001
    seen: list[int] = []
    cursor = None
    for _ in range(50):
        page = client.get("/api/calls", params={**params, **({"cursor": cursor} if cursor else {})}).json()
        seen.extend(c["id"] for c in page["items"])
        cursor = page["next_cursor"]
        if not cursor or not page["items"]:
            break
    return seen


@pytest.mark.parametrize("sort", ["-started_at", "started_at", "-started_local", "started_local"])
def test_a_cursor_walk_through_tied_start_times_is_the_offset_order(
    client, archive, session, sort  # noqa: ANN001
) -> None:
    """The keyset predicate gained a redundant ``started_at <=`` (``>=``) bound so the
    planner has an index range.  The bound is inclusive on purpose — rows that share the
    cursor's instant are still told apart by ``id`` — so three calls recorded in the same
    microsecond must each come back exactly once, in the order the offset listing has."""
    from app.models import Call

    anchor = session.get(Call, archive.call_ids[2])
    for call_id in archive.call_ids[3:5]:
        call = session.get(Call, call_id)
        call.started_at, call.started_local = anchor.started_at, anchor.started_local
    session.commit()

    expected = [c["id"] for c in client.get("/api/calls", params={"sort": sort, "page_size": 50}).json()["items"]]
    assert _walk_cursor(client, {"sort": sort, "page_size": 1}) == expected
    assert _walk_cursor(client, {"sort": sort, "page_size": 2}) == expected


def test_the_cursor_predicate_is_an_index_range(client, archive) -> None:  # noqa: ANN001, ARG001
    """The OR alone gave the planner nothing to range over: page N of a scroll walked
    ``ix_calls_started_at`` from the newest row and filtered 4 M rows away (5.2 s on
    production for a page from mid-2024).  The compiled SQL must carry the plain bound as a
    top-level conjunct, which is what becomes the ``Index Cond``."""
    import dataclasses
    from datetime import UTC, datetime

    from sqlalchemy.dialects.postgresql import psycopg

    from app.api.filters import CallFilters, cursor_condition, encode_cursor

    cursor = encode_cursor(datetime(2024, 5, 31, 23, 59, 45, tzinfo=UTC), 1234857)

    def sql(sort: str) -> str:
        f = dataclasses.replace(CallFilters(), sort=sort)
        # psycopg 3, the driver the app runs on: it casts every bind, hence the ``::TIMESTAMP``.
        return str(cursor_condition(f, cursor).compile(dialect=psycopg.dialect()))

    bound = "%(started_at_1)s::TIMESTAMP WITH TIME ZONE AND ("
    assert sql("-started_at").startswith(f"calls.started_at <= {bound}")
    assert sql("started_at").startswith(f"calls.started_at >= {bound}")


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "-started_at"},
        {"sort": "started_at"},
        {"sort": "-duration"},
        {"sort": "talkgroup"},
        {"sort": "-quality"},
        {"sort": "radio"},
        {"sort": "-started_at", "include_duplicates": True},
        {"sort": "-started_at", "talkgroup": 1701},
    ],
)
def test_offset_pages_are_the_one_big_page_cut_up(client, archive, params) -> None:  # noqa: ANN001
    """A page past the first is a deferred join (ids first, then the rows).  Cut into
    pages of two, every sort — the nullable ``NULLS LAST`` keys included — must give back
    the same rows, with the same contents, in the same order as one unpaged request."""
    whole = client.get("/api/calls", params={**params, "page_size": 50}).json()
    pages: list[dict] = []
    for number in range(1, 10):
        body = client.get("/api/calls", params={**params, "page_size": 2, "page": number}).json()
        assert body["total"] == whole["total"]
        pages.extend(body["items"])
        if len(body["items"]) < 2:
            break
    assert [c["id"] for c in pages] == [c["id"] for c in whole["items"]]
    assert pages == whole["items"]


def test_an_offset_page_joins_only_the_rows_it_returns() -> None:
    """The skipped rows are only ever ``calls.id``: the LIMIT/OFFSET sits inside the id
    subquery, and the denormalized columns are selected outside it."""
    from sqlalchemy.dialects import postgresql

    from app.api.filters import CallFilters, PageParams
    from app.services.calls import _deferred_page

    sql = str(_deferred_page(CallFilters(), PageParams(page=101, page_size=50)).compile(dialect=postgresql.dialect()))
    inner = sql[sql.index("FROM (SELECT") : sql.index(") AS page_ids")]
    assert inner.startswith("FROM (SELECT calls.id AS id \nFROM calls")
    assert "LIMIT" in inner and "OFFSET" in inner
    assert "systems.name" not in inner and "transcript_text" not in inner
    assert "JOIN calls ON calls.id = page_ids.id" in sql


def test_the_total_stops_at_the_counting_cap(client, archive, monkeypatch) -> None:
    from app.api import filters as filters_module
    from app.services import calls as calls_service

    monkeypatch.setattr(
        calls_service, "capped_count", lambda s, f: filters_module.capped_count(s, f, cap=2)
    )
    body = client.get("/api/calls", params={"page_size": 2}).json()
    assert body["total"] == 2
    assert body["total_capped"] is True


def test_recomputing_a_waveform_needs_a_credential(client, archive, write_token) -> None:
    """Reading the cache is cheap; `refresh=1` is an ffmpeg decode per request."""
    call_id = archive.call_ids[0]
    assert client.get(f"/api/calls/{call_id}/waveform").status_code in (200, 503)
    assert client.get(f"/api/calls/{call_id}/waveform", params={"refresh": 1}).status_code == 401
    with_credential = client.get(
        f"/api/calls/{call_id}/waveform", params={"refresh": 1}, headers=bearer(write_token)
    )
    assert with_credential.status_code in (200, 503)


# --------------------------------------------------------------------------------------
# reconciliation (docs/RECONCILIATION.md §6)
# --------------------------------------------------------------------------------------


def test_the_detail_shows_the_other_copies_and_why_this_one_won(
    client, archive, session, settings  # noqa: ANN001
) -> None:
    """``GET /calls/{id}`` carries the pair: the hidden copies, scored, and the ruling."""
    from app.ingest.reconcile import reconcile_pair
    from app.models import Call

    primary = session.get(Call, archive.call_ids[0])
    other = session.get(Call, archive.call_ids[1])
    # Put the second recording over the first one's slice, as a second recorder would.
    other.started_at = primary.started_at
    other.ended_at = primary.ended_at
    other.talkgroup_ids = list(primary.talkgroup_ids)
    other.audio_state = "purged"
    session.flush()
    reconcile_pair(session, other, primary, settings)
    session.commit()

    detail = client.get(f"/api/calls/{primary.id}").json()

    assert [copy["id"] for copy in detail["duplicates"]] == [other.id]
    copy = detail["duplicates"][0]
    assert copy["audio_state"] == "purged"
    assert copy["score"] < 10_000  # no audio, so it never cleared the first rank
    assert detail["reconcile_reason"]["rule"] == "audio"
    assert detail["reconciled_at"] is not None
    # and the hidden copy is still reachable, and says the same thing from its side
    hidden = client.get(f"/api/calls/{other.id}").json()
    assert hidden["duplicate_of"] == primary.id


def test_make_primary_swaps_the_pair(client, archive, session, settings, write_token) -> None:  # noqa: ANN001
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

    response = client.post(
        f"/api/calls/{other.id}/make-primary", headers=bearer(write_token) | CSRF_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["duplicate_of"] is None
    assert response.json()["reconcile_reason"]["rule"] == "manual"
    session.expire_all()
    assert session.get(Call, primary.id).duplicate_of == other.id
    # the listing follows: the new primary is visible, the old one is not
    listed = [c["id"] for c in client.get("/api/calls").json()["items"]]
    assert other.id in listed
    assert primary.id not in listed


def test_make_primary_refuses_a_call_that_is_already_primary(
    client, archive, write_token  # noqa: ANN001
) -> None:
    response = client.post(
        f"/api/calls/{archive.call_ids[0]}/make-primary",
        headers=bearer(write_token) | CSRF_HEADERS,
    )
    assert response.status_code == 409


def test_make_primary_needs_a_credential(client, archive) -> None:  # noqa: ANN001
    """No credential, no swap — and a browser-shaped request is refused before that."""
    assert client.post(f"/api/calls/{archive.call_ids[0]}/make-primary").status_code == 403
    refused = client.post(f"/api/calls/{archive.call_ids[0]}/make-primary", headers=CSRF_HEADERS)
    assert refused.status_code == 401


def test_the_default_sort_does_not_ask_for_nulls_last(client, archive) -> None:  # noqa: ANN001, ARG001
    """``ORDER BY started_at DESC`` — and nothing after it that hides the index.

    ``calls.started_at`` is ``NOT NULL``, so ``NULLS LAST`` changed no row order; what it
    did change was the plan.  PostgreSQL's ``DESC`` btree is ``DESC NULLS FIRST``, so
    ``DESC NULLS LAST`` does not match ``ix_calls_started_at`` and the planner fell back to
    a parallel sequential scan over three million rows, six hash joins and a top-N sort —
    2.9 s measured on production to return fifty rows, against 1.4 ms once the clause was
    gone (the ``calls.id`` tiebreak becomes an incremental sort on top of the index scan).

    The assertion is on the compiled SQL rather than on a timing, and it covers the
    ascending case too: the archiver client that asks for the oldest call once an hour hit
    exactly the same plan.
    """
    import dataclasses

    from sqlalchemy.dialects import postgresql

    from app.api.filters import CallFilters, order_by

    def sql(sort: str) -> list[str]:
        f = dataclasses.replace(CallFilters(), sort=sort)
        return [str(o.compile(dialect=postgresql.dialect())) for o in order_by(f)]

    assert sql("-started_at") == ["calls.started_at DESC", "calls.id DESC"]
    assert sql("started_at") == ["calls.started_at ASC", "calls.id ASC"]
    assert sql("-recorded_at") == ["calls.recorded_at DESC", "calls.id DESC"]

    # A nullable sort key keeps NULLS LAST: there the ordering genuinely differs, and
    # "unknown talkgroup" belongs at the end of the list rather than the top of it.
    assert sql("-talkgroup") == ["calls.talkgroup_id DESC NULLS LAST", "calls.id DESC"]
    assert sql("quality") == ["calls.transcript_quality ASC NULLS LAST", "calls.id ASC"]


def test_both_sort_directions_still_order_correctly(client, archive) -> None:  # noqa: ANN001, ARG001
    """The plan changed; the answer did not."""
    newest = client.get("/api/calls", params={"sort": "-started_at", "page_size": 50}).json()["items"]
    oldest = client.get("/api/calls", params={"sort": "started_at", "page_size": 50}).json()["items"]

    assert [c["id"] for c in oldest] == [c["id"] for c in reversed(newest)]
    starts = [c["started_at"] for c in newest]
    assert starts == sorted(starts, reverse=True)


# --------------------------------------------------------------------------------------
# a hand-typed caption is a transcript
# --------------------------------------------------------------------------------------


def test_a_caption_on_an_untranscribed_call_survives_the_job_and_the_backfill(
    client, session, settings, write_token, tmp_path  # noqa: ANN001
) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.flag_hallucinations import flag_hallucinations
    from app.models import Call, Transcript, TranscriptionJob
    from app.stt.postprocess import assess
    from app.stt.worker import SttWorker
    from tests.test_stt_worker import FakeRouter, make_result, seed_call

    # Thirty seconds and "Thank you.": exactly what the backfill would null if it judged
    # the call's text rather than the engine's.
    call, job_row = seed_call(session, root=tmp_path, duration=30.0)
    caption = "Thank you."
    response = client.patch(
        f"/api/calls/{call.id}", json={"transcript_text": caption}, headers=bearer(write_token)
    )
    assert response.status_code == 200
    assert response.json()["transcript_status"] == "done"

    session.expire_all()
    manual = session.execute(select(Transcript).where(Transcript.call_id == call.id)).scalar_one()
    assert (manual.engine, manual.is_primary, manual.edited_by_user) == ("manual", True, True)
    assert manual.edited_text == caption

    # the job that was queued before the caption now runs and completes
    worker = SttWorker(
        router=FakeRouter(make_result()),
        settings=settings,
        session_factory=SessionLocal,
        worker_name="test-worker",
        audio_root=tmp_path,
    )
    job = worker.claim()
    assert job is not None and job.id == job_row.id
    result = make_result("Engine 1 responding to Riverton.", duration=30.0)
    worker.complete(job, result, assess(result, 30.0))

    session.expire_all()
    stored = session.get(Call, call.id)
    assert (stored.transcript_text, stored.transcript_status) == (caption, "done")
    rows = session.scalars(
        select(Transcript).where(Transcript.call_id == call.id).order_by(Transcript.id)
    ).all()
    assert [(r.engine, r.is_primary) for r in rows] == [("manual", True), ("openai_compatible", False)]
    assert session.get(TranscriptionJob, job_row.id).status == "done"

    report = flag_hallucinations(session, dry_run=False)
    assert report["flagged"] == 0
    session.expire_all()
    assert session.get(Call, call.id).transcript_text == caption

    transcripts = client.get(f"/api/calls/{call.id}/transcripts").json()
    assert {t["engine"] for t in transcripts} == {"manual", "openai_compatible"}


def test_correcting_a_call_with_no_primary_makes_the_corrected_row_primary(
    client, session, write_token  # noqa: ANN001
) -> None:
    from sqlalchemy import select

    from app.models import Transcript
    from tests.test_stt_worker import seed_call

    call, _ = seed_call(session)
    session.add(Transcript(call_id=call.id, engine="openai_compatible", text_="Engine one", is_primary=False))
    session.commit()

    response = client.patch(
        f"/api/calls/{call.id}", json={"transcript_text": "Engine 1"}, headers=bearer(write_token)
    )
    assert response.status_code == 200

    session.expire_all()
    row = session.execute(select(Transcript).where(Transcript.call_id == call.id)).scalar_one()
    assert (row.is_primary, row.edited_by_user, row.edited_text) == (True, True, "Engine 1")


def test_patching_a_missing_call_is_a_404(client, write_token) -> None:  # noqa: ANN001
    response = client.patch("/api/calls/987654321", json={"notes": "x"}, headers=bearer(write_token))
    assert response.status_code == 404


def test_a_guest_is_not_told_which_stt_server_wrote_a_transcript(client, session, archive, read_token) -> None:  # noqa: ANN001
    """The same line as ``GET /stats/transcription``: which speech-to-text servers the operator
    runs is not public.  The engine and model describe the words and stay."""
    from sqlalchemy import select

    from app.models import Transcript

    for row in session.scalars(select(Transcript).where(Transcript.call_id == archive.transcribed_id)):
        row.backend_name = "stt-gpu"
    session.commit()
    detail_url = f"/api/calls/{archive.transcribed_id}"

    guest_detail = client.get(detail_url)
    guest_list = client.get(f"{detail_url}/transcripts")
    assert guest_detail.status_code == guest_list.status_code == 200
    assert guest_detail.json()["transcript"]["backend_name"] is None
    assert guest_detail.json()["transcript"]["engine"]
    assert all(item["backend_name"] is None for item in guest_list.json())
    assert "stt-gpu" not in guest_detail.text + guest_list.text

    auth = {"Authorization": f"Bearer {read_token}"}
    assert client.get(detail_url, headers=auth).json()["transcript"]["backend_name"] == "stt-gpu"
    assert {item["backend_name"] for item in client.get(f"{detail_url}/transcripts", headers=auth).json()} == {
        "stt-gpu"
    }
