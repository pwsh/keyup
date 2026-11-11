"""Admin → Exports: the trunked-system gate, the draft test button, and the flag copy.

Three questions this suite answers, all of them about things an operator does *before*
anything is published:

* ``GET /admin/exports`` says which systems Broadcastify **Calls** could ever publish, so
  the page can refuse to arm a switch that has nothing to send.
* the two test buttons test the values on the screen, including the ones not saved yet —
  the whole reason to press one is to find out whether the key just typed in is right —
  and testing a draft never writes it anywhere.
* ``POST /admin/talkgroups/export-flags/copy`` moves one per-talkgroup checkbox onto
  another, which is how an operator carries an existing Broadcastify selection over to the
  new Calls flag without several hundred clicks.
"""

from __future__ import annotations

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def _systems(client) -> list[dict]:  # noqa: ANN001
    return client.get("/api/systems").json()


# --------------------------------------------------------------------------------------
# which systems Calls may publish
# --------------------------------------------------------------------------------------


def test_the_status_lists_the_trunked_systems(client, archive, session, admin_token, read_token) -> None:  # noqa: ANN001, ARG001
    """The Calls card's whole precondition, reported rather than inferred by the SPA."""
    from app.models import System

    system = session.scalars(select_systems()).first()
    system.protocol = "P25 Phase 1"
    session.commit()

    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    assert [row["name"] for row in body["trunked_systems"]] == [system.name]

    # …and an operator saying "no, that one is conventional" empties the list, which is
    # what disables the switch on the page.
    response = client.patch(
        f"/api/systems/{system.id}", json={"trunked": False}, headers=bearer(admin_token)
    )
    assert response.status_code == 200
    assert response.json()["is_trunked"] is False
    assert session.get(System, system.id) is not None
    assert client.get("/api/admin/exports", headers=bearer(read_token)).json()["trunked_systems"] == []


def test_a_system_with_no_protocol_is_not_offered_to_calls(client, archive, session, read_token) -> None:  # noqa: ANN001, ARG001
    """Fails closed: "nobody has said" is not "yes" where somebody else's archive is concerned."""
    system = session.scalars(select_systems()).first()
    system.protocol = None
    system.trunked = None
    session.commit()
    assert client.get("/api/admin/exports", headers=bearer(read_token)).json()["trunked_systems"] == []


def select_systems():  # noqa: ANN201 - a tiny helper so the import stays local
    from sqlalchemy import select

    from app.models import System

    return select(System).order_by(System.id)


# --------------------------------------------------------------------------------------
# testing the draft
# --------------------------------------------------------------------------------------


def test_testing_openmhz_uses_the_unsaved_short_name(client, admin_token) -> None:  # noqa: ANN001
    """The complaint this exists for: "not set" until I clicked save."""
    stored = client.post("/api/admin/exports/openmhz/test", headers=bearer(admin_token)).json()
    assert stored["ok"] is False
    assert "OPENMHZ_API_KEY" in stored["detail"]
    assert stored["used_draft"] is False

    drafted = client.post(
        "/api/admin/exports/openmhz/test",
        json={"overrides": {"openmhz_api_key": "typed-just-now", "openmhz_short_name": "examplesys"}},
        headers=bearer(admin_token),
    ).json()
    assert drafted["used_draft"] is True
    assert drafted["draft_keys"] == ["openmhz_api_key", "openmhz_short_name"]
    # It got past "which credential is missing" — whatever the network then said.
    assert "OPENMHZ_API_KEY" not in drafted["detail"]


def test_a_draft_is_never_persisted(client, admin_token, read_token) -> None:  # noqa: ANN001
    """A test is a read. Nothing it was handed may survive it."""
    client.post(
        "/api/admin/exports/openmhz/test",
        json={"overrides": {"openmhz_short_name": "not-saved"}},
        headers=bearer(admin_token),
    )
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert rows["openmhz_short_name"]["is_default"] is True
    assert rows["openmhz_short_name"]["value"] != "not-saved"


def test_a_draft_may_not_carry_another_services_key(client, admin_token) -> None:  # noqa: ANN001
    """Otherwise the test body would be a second, unaudited settings API."""
    response = client.post(
        "/api/admin/exports/openmhz/test",
        json={"overrides": {"broadcastify_api_key": "someone-elses"}},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422
    assert "openmhz" in response.json()["detail"]

    response = client.post(
        "/api/admin/exports/openmhz/test",
        json={"overrides": {"secret_key": "nice-try"}},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422


def test_a_draft_value_is_coerced_the_way_a_save_would(client, admin_token) -> None:  # noqa: ANN001
    """An enum that a save would refuse is refused here too, rather than silently ignored."""
    response = client.post(
        "/api/admin/exports/broadcastify/test",
        json={"overrides": {"broadcastify_environment": "sandbox"}},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422


def test_testing_broadcastify_with_a_draft_environment(client, admin_token) -> None:  # noqa: ANN001
    """The draft decides which credential set is tested, exactly as saving it would."""
    body = client.post(
        "/api/admin/exports/broadcastify/test",
        json={"overrides": {"broadcastify_environment": "development"}},
        headers=bearer(admin_token),
    ).json()
    assert body["used_draft"] is True
    assert body["environment"] == "development"


def test_the_feed_test_takes_a_draft_too(client, admin_token, read_token) -> None:  # noqa: ANN001
    """Same button, same complaint, same fix — and still nothing is switched on."""
    body = client.post(
        "/api/admin/exports/broadcastify-feed/test",
        json={
            "overrides": {
                "broadcastify_feed_server": "",
                "broadcastify_feed_mount": "testmount",
            }
        },
        headers=bearer(admin_token),
    ).json()
    assert body["used_draft"] is True
    assert body["draft_keys"] == ["broadcastify_feed_mount"]  # the blank one means "unchanged"
    feed = client.get("/api/admin/exports", headers=bearer(read_token)).json()["broadcastify_feed"]
    assert feed["enabled"] is False


def test_a_test_still_needs_admin(client) -> None:  # noqa: ANN001
    assert client.post("/api/admin/exports/openmhz/test", json={"overrides": {}}).status_code in (
        401,
        403,
    )


# --------------------------------------------------------------------------------------
# copying one flag onto another
# --------------------------------------------------------------------------------------


def _talkgroups(client) -> dict[int, dict]:  # noqa: ANN001
    return {row["tgid"]: row for row in client.get("/api/talkgroups").json()["items"]}


def test_copying_broadcastify_onto_the_calls_flag(client, archive, session, admin_token) -> None:  # noqa: ANN001, ARG001
    """The migration an operator does once: make Calls what Broadcastify already was."""
    system = session.scalars(select_systems()).first()
    system.trunked = True
    session.commit()

    rows = _talkgroups(client)
    one = next(iter(rows.values()))
    client.patch(
        f"/api/talkgroups/{one['id']}",
        json={"export_broadcastify": False},
        headers=bearer(admin_token),
    )

    result = client.post(
        "/api/admin/talkgroups/export-flags/copy",
        json={"source": "export_broadcastify", "destination": "export_bcfy_calls", "all": True},
        headers=bearer(admin_token),
    )
    assert result.status_code == 200
    body = result.json()
    assert body["considered"] == len(rows)
    assert body["updated"] == 1
    assert body["skipped_not_trunked"] == 0
    after = _talkgroups(client)
    assert after[one["tgid"]]["export_bcfy_calls"] is False
    assert all(
        row["export_bcfy_calls"] is True for tgid, row in after.items() if tgid != one["tgid"]
    )


def test_a_copy_never_ticks_calls_on_a_conventional_system(client, archive, session, admin_token) -> None:  # noqa: ANN001, ARG001
    """The rule wins over the operator's intent, and says how often it had to."""
    system = session.scalars(select_systems()).first()
    system.trunked = False
    session.commit()

    body = client.post(
        "/api/admin/talkgroups/export-flags/copy",
        json={"source": "export_broadcastify", "destination": "export_bcfy_calls", "all": True},
        headers=bearer(admin_token),
    ).json()
    assert body["skipped_not_trunked"] == body["considered"] > 0
    assert all(row["export_bcfy_calls"] is False for row in _talkgroups(client).values())


def test_a_copy_can_be_a_selection(client, archive, session, admin_token) -> None:  # noqa: ANN001, ARG001
    system = session.scalars(select_systems()).first()
    system.trunked = True
    session.commit()
    rows = _talkgroups(client)
    picked = [row["id"] for row in list(rows.values())[:1]]
    client.post(
        "/api/admin/talkgroups/export-flags/copy",
        json={"source": "export_bcfy_calls", "destination": "export_openmhz", "ids": picked},
        headers=bearer(admin_token),
    )
    body = client.post(
        "/api/admin/talkgroups/export-flags/copy",
        json={"source": "export_bcfy_calls", "destination": "export_openmhz", "ids": picked},
        headers=bearer(admin_token),
    ).json()
    assert body["considered"] == 1
    assert body["unchanged"] == 1


def test_a_copy_refuses_nonsense(client, archive, admin_token) -> None:  # noqa: ANN001, ARG001
    """Same column twice, no target at all, both at once, an unknown column, an unknown id."""
    for payload in (
        {"source": "export_openmhz", "destination": "export_openmhz", "all": True},
        {"source": "export_openmhz", "destination": "export_bcfy_calls"},
        {"source": "export_openmhz", "destination": "export_bcfy_calls", "ids": [1], "all": True},
        {"source": "transcribe", "destination": "export_bcfy_calls", "all": True},
    ):
        response = client.post(
            "/api/admin/talkgroups/export-flags/copy", json=payload, headers=bearer(admin_token)
        )
        assert response.status_code == 422, payload

    missing = client.post(
        "/api/admin/talkgroups/export-flags/copy",
        json={"source": "export_openmhz", "destination": "export_bcfy_calls", "ids": [999_999]},
        headers=bearer(admin_token),
    )
    assert missing.status_code == 404


def test_copying_needs_admin(client) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/talkgroups/export-flags/copy",
        json={"source": "export_openmhz", "destination": "export_bcfy_calls", "all": True},
    )
    assert response.status_code in (401, 403)


# --------------------------------------------------------------------------------------
# the flag is held false where Calls could never publish
# --------------------------------------------------------------------------------------


def test_editing_a_talkgroup_cannot_tick_calls_on_a_conventional_system(
    client, archive, session, admin_token
) -> None:  # noqa: ANN001, ARG001
    system = session.scalars(select_systems()).first()
    system.trunked = False
    session.commit()
    row = next(iter(_talkgroups(client).values()))

    patched = client.patch(
        f"/api/talkgroups/{row['id']}",
        json={"export_bcfy_calls": True},
        headers=bearer(admin_token),
    )
    assert patched.status_code == 200
    assert patched.json()["export_bcfy_calls"] is False

    bulk = client.post(
        "/api/talkgroups/bulk",
        json={"ids": [row["id"]], "changes": {"export_bcfy_calls": True}},
        headers=bearer(admin_token),
    )
    assert bulk.status_code == 200
    assert bulk.json()["items"][0]["export_bcfy_calls"] is False


def test_the_same_edit_is_allowed_on_a_trunked_system(client, archive, session, admin_token) -> None:  # noqa: ANN001, ARG001
    system = session.scalars(select_systems()).first()
    system.trunked = True
    session.commit()
    row = next(iter(_talkgroups(client).values()))
    patched = client.patch(
        f"/api/talkgroups/{row['id']}",
        json={"export_bcfy_calls": False},
        headers=bearer(admin_token),
    )
    assert patched.json()["export_bcfy_calls"] is False
    patched = client.patch(
        f"/api/talkgroups/{row['id']}",
        json={"export_bcfy_calls": True},
        headers=bearer(admin_token),
    )
    assert patched.json()["export_bcfy_calls"] is True


# --------------------------------------------------------------------------------------
# what the services answered
# --------------------------------------------------------------------------------------


def test_the_status_counts_duplicates_and_the_responses_it_stored(client, archive, session, read_token) -> None:  # noqa: ANN001
    """The number the operator came for: how many of those skips were "already have it".

    Duplicates are counted from ``skip_reason`` rather than from ``response``, so the
    thousands of rows written before the response columns existed still count; the
    24-hour histogram reads ``response`` and buckets it, so a run of skew rejections shows
    as one line with a count rather than one line per call.
    """
    from datetime import UTC, datetime, timedelta

    from app.exporters.broadcastify import DUPLICATE_REASON
    from app.models import CallExport

    now = datetime.now(UTC)
    rows = [
        # two duplicates: one recent, one from last week (all-time only)
        ("skipped", DUPLICATE_REASON, "1 SKIPPED---ALREADY-RECEIVED-THIS-CALL", 1, now),
        ("skipped", DUPLICATE_REASON, None, None, now - timedelta(days=7)),
        # a skip that is *our* decision, not their answer — never a duplicate
        ("skipped", "encrypted", None, None, now),
        ("uploaded", None, "0 abc123", 0, now),
        (
            "failed",
            None,
            "1 REJECTED-CALL-SKEW-TOO-LONG-4172-See:https://www.broadcastify.com/skew",
            1,
            now,
        ),
        ("failed", None, "1 REJECTED-CALL-SKEW-TOO-LONG-91-See:https://www.broadcastify.com/skew", 1, now),
    ]
    for call_id, (status, reason, response, code, when) in zip(archive.call_ids, rows, strict=False):
        session.add(
            CallExport(
                call_id=call_id,
                service="broadcastify",
                status=status,
                skip_reason=reason,
                response=response,
                response_code=code,
                updated_at=when,
            )
        )
    session.commit()

    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    calls = next(row for row in body["services"] if row["service"] == "broadcastify")

    assert calls["duplicates"] == 2
    assert calls["duplicates_24h"] == 1
    assert calls["responses_24h"] == {
        "1 SKIPPED": 1,
        "0 OK": 1,
        # the two skews differ only in their seconds, and count as one class
        "1 REJECTED-CALL-SKEW-TOO-LONG": 2,
    }
    # …and a row's raw answer travels with it, for the table's tooltip.
    recent = {row["id"]: row for row in body["recent"]}
    assert any(row["response"] == "0 abc123" for row in recent.values())

    openmhz = next(row for row in body["services"] if row["service"] == "openmhz")
    assert (openmhz["duplicates"], openmhz["duplicates_24h"], openmhz["responses_24h"]) == (0, 0, {})


def test_the_status_counts_the_calls_that_were_already_too_late(client, archive, session, read_token) -> None:  # noqa: ANN001
    """The other half of the duplicate number: skips this end took before sending anything.

    A duplicate is somebody else publishing first; a late skip is this deployment not
    reaching the archive inside Broadcastify's skew window. Both are `skipped` rows and
    only the reason tells them apart, so the card shows the two side by side.
    """
    from datetime import UTC, datetime, timedelta

    from app.exporters.broadcastify import LATE_REASON
    from app.models import CallExport

    now = datetime.now(UTC)
    rows = [
        ("skipped", f"{LATE_REASON} (18.2 s after call end)", now),
        ("skipped", f"{LATE_REASON} (31.7 s after call end)", now - timedelta(hours=2)),
        # outside the window, so it counts nowhere on this card
        ("skipped", f"{LATE_REASON} (22.0 s after call end)", now - timedelta(days=3)),
        ("skipped", "encrypted", now),
    ]
    for call_id, (status, reason, when) in zip(archive.call_ids, rows, strict=False):
        session.add(
            CallExport(
                call_id=call_id,
                service="broadcastify",
                status=status,
                skip_reason=reason,
                updated_at=when,
            )
        )
    session.commit()

    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    calls = next(row for row in body["services"] if row["service"] == "broadcastify")
    openmhz = next(row for row in body["services"] if row["service"] == "openmhz")

    assert calls["late_24h"] == 2
    assert calls["duplicates_24h"] == 0
    assert openmhz["late_24h"] == 0


def test_the_status_counts_the_second_copies_it_declined_to_upload(client, archive, session, read_token) -> None:  # noqa: ANN001
    """"Duplicates avoided": our own second copy of one transmission, never sent.

    Two recorders record every call and reconciliation can move the primary after the first
    copy has been published, so the promoted copy must be refused (docs/EXPORTERS.md, "One
    upload per transmission"). It is a different fact from `duplicates_24h` — that one is
    Broadcastify saying it already holds a call somebody else sent — and both cards show it.
    """
    from datetime import UTC, datetime, timedelta

    from app.exporters.ledger import duplicate_upload_reason
    from app.models import CallExport

    now = datetime.now(UTC)
    rows = [
        ("broadcastify", "skipped", duplicate_upload_reason(41), now),
        ("openmhz", "skipped", duplicate_upload_reason(41), now - timedelta(hours=3)),
        # outside the window, and a skip of our own that is not this one
        ("broadcastify", "skipped", duplicate_upload_reason(9), now - timedelta(days=2)),
        ("broadcastify", "skipped", "encrypted", now),
    ]
    for call_id, (service, status, reason, when) in zip(archive.call_ids, rows, strict=False):
        session.add(
            CallExport(
                call_id=call_id,
                service=service,
                status=status,
                skip_reason=reason,
                updated_at=when,
            )
        )
    session.commit()

    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    calls = next(row for row in body["services"] if row["service"] == "broadcastify")
    openmhz = next(row for row in body["services"] if row["service"] == "openmhz")

    assert calls["duplicate_uploads_24h"] == 1
    assert openmhz["duplicate_uploads_24h"] == 1
    # …and it is counted apart from the service's own "already received this call".
    assert (calls["duplicates_24h"], calls["late_24h"]) == (0, 0)


# --------------------------------------------------------------------------------------
# recorder-direct upload, on the card
# --------------------------------------------------------------------------------------


def _calls(body: dict) -> dict:
    return next(row for row in body["services"] if row["service"] == "broadcastify")


def test_the_card_says_who_uploads_and_starts_at_keyup(client, read_token) -> None:  # noqa: ANN001
    """The shipped answer, and the one the fallback always returns to."""
    calls = _calls(client.get("/api/admin/exports", headers=bearer(read_token)).json())
    assert calls["upload_at"] == "keyup"
    assert calls["by_recorder_24h"] == 0
    assert calls["claimed_24h"] == 0
    # OpenMHz has no such choice and must not be made to look as though it does.
    openmhz = next(
        row for row in client.get("/api/admin/exports", headers=bearer(read_token)).json()["services"] if row["service"] == "openmhz"
    )
    assert openmhz["upload_at"] == "keyup"


def test_the_card_counts_recorder_uploads_and_claims(client, session, archive, read_token) -> None:  # noqa: ANN001, ARG001
    """The pair that answers "is it actually running", counted from the ledger itself."""
    from app import runtime_settings
    from app.exporters.claims import CLAIMED_REASON, KEYUP
    from app.models import CallExport, Setting

    session.merge(Setting(key="broadcastify_upload_at", value="recorder"))
    session.commit()
    runtime_settings.invalidate()

    calls = session.scalars(select_calls()).all()[:3]
    assert len(calls) == 3
    session.add_all(
        [
            # one a recorder sent…
            CallExport(call_id=calls[0].id, service="broadcastify", status="uploaded", uploaded_by="trunkd-01"),
            # …one this host sent, which must not be counted as a recorder's…
            CallExport(call_id=calls[1].id, service="broadcastify", status="uploaded", uploaded_by=KEYUP),
            # …and one this host stood down on.
            CallExport(
                call_id=calls[2].id,
                service="broadcastify",
                status="skipped",
                skip_reason=f"{CLAIMED_REASON} trunkd-02",
            ),
        ]
    )
    session.commit()

    body = _calls(client.get("/api/admin/exports", headers=bearer(read_token)).json())
    assert body["upload_at"] == "recorder"
    assert body["by_recorder_24h"] == 1
    assert body["claimed_24h"] == 1
    runtime_settings.invalidate()


def select_calls():  # noqa: ANN201 - a tiny helper so the import stays local
    from sqlalchemy import select

    from app.models import Call

    return select(Call).order_by(Call.id)


# --------------------------------------------------------------------------------------
# the ledger figures are cached; the configuration beside them is not
# --------------------------------------------------------------------------------------


def test_the_ledger_figures_are_cached_and_a_retry_clears_them(client, session, archive, admin_token, read_token) -> None:  # noqa: ANN001
    """A minute-old tally is fine; a row the operator just re-queued must show at once."""
    from app.exporters import gate
    from app.models import CallExport

    failed = CallExport(call_id=archive.call_ids[0], service="openmhz", status="failed", attempts=5)
    session.add(failed)
    session.commit()

    def openmhz(body: dict) -> dict:
        return next(row for row in body["services"] if row["service"] == "openmhz")

    first = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    assert (openmhz(first)["failed"], openmhz(first)["pending"]) == (1, 0)
    assert [row["call_id"] for row in first["recent"]] == [archive.call_ids[0]]

    # A row written behind the API's back (the exporter worker is another process) waits
    # for the cache to expire…
    session.add(CallExport(call_id=archive.call_ids[1], service="openmhz", status="uploaded"))
    session.commit()
    cached = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    assert (openmhz(cached)["failed"], openmhz(cached)["uploaded"]) == (1, 0)
    assert len(cached["recent"]) == 1

    # …but the configuration half is read fresh: switching a service on shows at once.
    assert openmhz(cached)["enabled_at"] is None
    gate.set_watermark(session, "openmhz")
    session.commit()
    assert openmhz(client.get("/api/admin/exports", headers=bearer(read_token)).json())["enabled_at"] is not None

    # The retry route drops the entry, so the page shows the new queue on its next poll.
    retried = client.post(f"/api/admin/exports/openmhz/ledger/{failed.id}/retry", headers=bearer(admin_token))
    assert retried.status_code == 200
    after = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    assert (openmhz(after)["failed"], openmhz(after)["pending"], openmhz(after)["uploaded"]) == (
        0,
        1,
        1,
    )
    assert {row["call_id"] for row in after["recent"]} == set(archive.call_ids[:2])
