"""The site channel audit: observations in, RadioReference alongside, the report out.

The scenario built in :func:`audited_site` is one that exercises every category at once —
a site that has failed over to its backup control channel, a traffic channel nobody
documented, a documented channel nobody has heard for a month, a recorder configured for
the wrong frequency, and two neighbors of which Keyup knows one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.models import AdjacentSite, AlertRule, Notification, RecorderObservation, Site, SiteFrequency, System
from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)

CONTROL = 851_050_000  # RadioReference's primary
BACKUP = 851_787_500  # RadioReference's alternate — live today
VOICE_KNOWN = 856_262_500  # documented and heard
VOICE_STALE = 856_912_500  # documented, last heard a month ago
VOICE_NEW = 856_712_500  # heard, undocumented
VOICE_GONE = 857_000_000  # documented, never heard
CONFIGURED_ONLY = 852_000_000  # in the recorder's config, not on the air


def observation(
    *,
    observed_at: datetime,
    control: int,
    secondary: list[int] | None = None,
    traffic: list[tuple[int, int]] | None = None,
    configured: list[int] | None = None,
    site: str = "Central",
    site_number: int = 1,
    adjacent: list[dict] | None = None,
) -> dict:
    return {
        "recorder": "trunkd",
        "recorder_version": "0.0.1",
        "observed_at": observed_at.isoformat(),
        "window_s": 300,
        "system": {"label": "Example System", "wacn": 101010, "system_id": 52, "nac": 52},
        "site": {"label": site, "rfss": 10, "site": site_number, "lra": 10},
        "control": {"frequency_hz": control, "channel": "1-3", "tsbk_rate": 32.1},
        "secondary_control": [
            {"frequency_hz": f, "channel": "1-638", "service_class": ["backup_control"]}
            for f in (secondary or [])
        ],
        "configured_control": configured or [],
        "bands": [
            {
                "id": 1,
                "base_hz": 851_012_500,
                "spacing_hz": 12_500,
                "offset_hz": -45_000_000,
                "bandwidth_hz": 12_500,
                "tdma": False,
            }
        ],
        "traffic": [
            {
                "frequency_hz": frequency,
                "channel": "1-456",
                "grants": grants,
                "calls": grants,
                "first_seen": observed_at.isoformat(),
                "last_seen": observed_at.isoformat(),
            }
            for frequency, grants in (traffic or [])
        ],
        "adjacent_sites": adjacent or [],
    }


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


#: RR's tag table as ``getTag(0)`` returns it — ids are RR's to number, so nothing in the
#: app may hardcode them and the fake numbers them differently from RR on purpose.
RR_TAG_TABLE = [
    {"tagId": 41, "tagDescr": "Law Dispatch"},
    {"tagId": 42, "tagDescr": "Fire Dispatch"},
    {"tagId": 43, "tagDescr": "Interop"},
    {"tagId": 44, "tagDescr": "Law Tac"},
]


class FakeRadioReference:
    """The four reads :mod:`app.rr.sync` makes, answered from a literal."""

    def __init__(
        self,
        sites: list[dict],
        talkgroups: list[dict] | None = None,
        tag_table: list[dict] | None = None,
    ) -> None:
        self.sites = sites
        self.talkgroups = talkgroups or []
        self.tag_table = RR_TAG_TABLE if tag_table is None else tag_table
        self.calls: list[tuple[str, int]] = []

    def trs_details(self, sid: int) -> dict:
        self.calls.append(("details", sid))
        return {"wacn": 101010, "sysid": 52, "nac": 52, "sName": "Example System"}

    def trs_sites(self, sid: int) -> list[dict]:
        self.calls.append(("sites", sid))
        return self.sites

    def trs_talkgroups(self, sid: int) -> list[dict]:
        self.calls.append(("talkgroups", sid))
        return self.talkgroups

    def tags(self) -> list[dict]:
        self.calls.append(("tags", 0))
        return self.tag_table


def rr_sites() -> list[dict]:
    """What RadioReference says about the two sites, in RR's own units (MHz, ``use``)."""
    return [
        {
            "siteId": 9001,
            "siteNumber": 1,
            "siteDescr": "Example County simulcast",
            "rfss": 10,
            "nac": 52,
            "siteFreqs": [
                {"freq": CONTROL / 1e6, "use": "d", "lcn": 3, "colorCode": None, "chanId": 1},
                {"freq": BACKUP / 1e6, "use": "a", "lcn": 638, "colorCode": None, "chanId": 1},
                {"freq": VOICE_KNOWN / 1e6, "use": "", "lcn": 100, "colorCode": None, "chanId": 1},
                {"freq": VOICE_STALE / 1e6, "use": None, "lcn": 101, "colorCode": None, "chanId": 1},
                {"freq": VOICE_GONE / 1e6, "use": "", "lcn": 102, "colorCode": None, "chanId": 1},
            ],
        },
        {
            "siteId": 9002,
            "siteNumber": 2,
            "siteDescr": "West County simulcast",
            "rfss": 10,
            "nac": 52,
            "siteFreqs": [{"freq": 851.6, "use": "d", "lcn": 1, "colorCode": None, "chanId": 1}],
        },
    ]


def post(client, token: str, document: dict):  # noqa: ANN001, ANN201
    return client.post("/api/recorder/observations", json=document, headers=bearer(token))


# --------------------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------------------


def test_observations_need_a_token_with_the_upload_scope(client, session, read_token, upload_token) -> None:
    document = observation(observed_at=NOW, control=CONTROL)

    assert client.post("/api/recorder/observations", json=document).status_code == 401
    assert client.post(
        "/api/recorder/observations", json=document, headers=bearer("kup_not-a-token")
    ).status_code == 401
    assert post(client, read_token, document).status_code == 403
    assert post(client, upload_token, document).status_code == 200


def test_an_observation_that_names_no_system_is_a_bad_request(client, session, upload_token) -> None:
    """An empty ``system.label`` with nothing to fall back on is the recorder's mistake.

    ``resolve_system_id`` refuses to invent a system and raises ``UnknownSystem``; the
    endpoint has to answer that the way the call push does — 400 and a sentence — rather
    than let it escape as a 500 that reads like the archive broke.
    """
    document = observation(observed_at=NOW, control=CONTROL)
    document["system"] = {"label": ""}

    response = post(client, upload_token, document)

    assert response.status_code == 400
    assert "no system" in response.json()["detail"]
    assert session.scalars(select(RecorderObservation)).all() == []


def test_an_observation_becomes_a_site_and_its_channel_inventory(client, session, upload_token) -> None:
    body = post(
        client,
        upload_token,
        observation(
            observed_at=NOW,
            control=CONTROL,
            secondary=[BACKUP],
            traffic=[(VOICE_NEW, 12)],
            configured=[CONTROL, CONFIGURED_ONLY],
            adjacent=[
                {
                    "system_id": 57,
                    "rfss": 20,
                    "site": 20,
                    "lra": 20,
                    "control_frequency_hz": 851_087_500,
                    "channel": "1-6",
                    "flags": {"conventional": False, "failure": False, "valid": True},
                }
            ],
        ),
    ).json()

    assert body["ok"] is True
    assert body["frequencies_new"] == 5  # control, secondary, traffic, two configured
    assert body["adjacent_new"] == 1

    site = session.scalars(select(Site).where(Site.name == "Central")).one()
    system = session.get(System, site.system_id)
    assert (system.wacn, system.system_id, system.nac) == (101010, 52, 52)
    assert (site.site_number, site.rfss, site.lra, site.nac) == (1, 10, 10, 52)

    rows = {
        (row.frequency_hz, row.source): row
        for row in session.scalars(select(SiteFrequency).where(SiteFrequency.site_id == site.id))
    }
    assert rows[(CONTROL, "observed")].role == "primary_control"
    assert rows[(BACKUP, "observed")].role == "alternate_control"
    assert rows[(VOICE_NEW, "observed")].role == "voice"
    assert rows[(VOICE_NEW, "observed")].grant_count == 12
    # The recorder's own config is a source of its own, and it is a *control* frequency list.
    assert rows[(CONTROL, "configured")].role == "primary_control"
    assert rows[(CONFIGURED_ONLY, "configured")].role == "primary_control"

    stored = session.scalars(select(RecorderObservation)).one()
    assert stored.payload["bands"][0]["base_hz"] == 851_012_500
    assert stored.window_s == 300


def test_repeated_windows_accumulate_rather_than_replace(client, session, upload_token) -> None:
    for minutes, grants in ((0, 12), (5, 7), (10, 3)):
        post(
            client,
            upload_token,
            observation(
                observed_at=NOW + timedelta(minutes=minutes),
                control=CONTROL,
                traffic=[(VOICE_NEW, grants)],
                adjacent=[{"system_id": 57, "rfss": 20, "site": 20, "control_frequency_hz": 851_087_500}],
            ),
        )

    site = session.scalars(select(Site).where(Site.name == "Central")).one()
    voice = session.scalars(
        select(SiteFrequency).where(
            SiteFrequency.site_id == site.id,
            SiteFrequency.frequency_hz == VOICE_NEW,
            SiteFrequency.source == "observed",
        )
    ).one()
    assert voice.observation_count == 3
    assert voice.grant_count == 22
    assert voice.first_seen_at == NOW
    assert voice.last_seen_at == NOW + timedelta(minutes=10)

    neighbor = session.scalars(select(AdjacentSite)).one()
    assert neighbor.observation_count == 3
    assert session.scalars(select(RecorderObservation)).all().__len__() == 3


def test_a_document_without_a_site_is_kept_but_not_normalized(client, session, upload_token) -> None:
    document = observation(observed_at=NOW, control=CONTROL)
    document["site"] = None
    body = post(client, upload_token, document).json()

    assert body["site_id"] is None
    assert "not normalized" in body["note"]
    assert session.scalars(select(SiteFrequency)).all() == []
    assert session.scalars(select(RecorderObservation)).one().payload["control"]["frequency_hz"] == CONTROL


# --------------------------------------------------------------------------------------
# RadioReference
# --------------------------------------------------------------------------------------


def test_rr_sync_maps_roles_matches_sites_and_never_overwrites_a_human_alias(
    client, session, settings, upload_token
) -> None:
    from app.models import Talkgroup
    from app.rr.sync import sync_due

    post(client, upload_token, observation(observed_at=NOW, control=CONTROL))
    post(client, upload_token, observation(observed_at=NOW, control=851_600_000, site="Westside", site_number=2))
    session.expire_all()

    system = session.scalars(select(System)).one()
    session.add_all(
        [
            Talkgroup(system_id=system.id, tgid=1101, alias="EC EVENT 6"),
            Talkgroup(system_id=system.id, tgid=1102, alias=None),
        ]
    )
    session.commit()

    fake = FakeRadioReference(
        rr_sites(),
        talkgroups=[
            {"tgDec": 1101, "tgAlpha": "RR NAME", "tgDescr": "RR description"},
            {"tgDec": 1102, "tgAlpha": "EC LAW 1", "tgDescr": "Countywide law"},
        ],
    )
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}, "rr_sync_hours": 24})
    summaries = sync_due(session, settings=tuned, client=fake, force=True, now=NOW)
    session.commit()

    assert [summary.rr_system_id for summary in summaries] == [7376]
    summary = summaries[0]
    assert (summary.sites_seen, summary.sites_matched) == (2, 2)
    assert summary.unmatched == []
    assert fake.calls == [("details", 7376), ("sites", 7376), ("talkgroups", 7376)]

    central = session.scalars(select(Site).where(Site.name == "Central")).one()
    assert central.rr_site_id == 9001
    assert central.rr_synced_at == NOW
    documented = {
        row.frequency_hz: row.role
        for row in session.scalars(
            select(SiteFrequency).where(
                SiteFrequency.site_id == central.id, SiteFrequency.source == "radioreference"
            )
        )
    }
    assert documented == {
        CONTROL: "primary_control",
        BACKUP: "alternate_control",
        VOICE_KNOWN: "voice",
        VOICE_STALE: "voice",
        VOICE_GONE: "voice",
    }

    named = {row.tgid: (row.alias, row.description) for row in session.scalars(select(Talkgroup))}
    assert named[1101] == ("EC EVENT 6", "RR description")  # the human alias survives
    assert named[1102] == ("EC LAW 1", "Countywide law")  # the empty one is filled in

    # A second pass inside RR_SYNC_HOURS is not made at all.
    fake.calls.clear()
    assert sync_due(session, settings=tuned, client=fake, now=NOW + timedelta(hours=1)) == []
    assert fake.calls == []


def test_rr_sync_files_a_talkgroup_under_rrs_service_tag_without_overruling_an_operator(
    client, session, settings, upload_token
) -> None:
    """The fill-only rule, applied to the classification as well as to the words.

    Four channels, one per case the sync has to get right: a blank one takes both RR's tag
    and the category derived from it; a channel an operator already filed under ``interop``
    keeps that bucket even though RR calls it law; a channel that already carries a service
    tag is not re-tagged; and a tag this build has never heard of is still recorded — in
    ``service_tag``, verbatim — while ``category`` is left alone rather than guessed at.
    """
    from app.models import Talkgroup
    from app.rr.sync import sync_due

    post(client, upload_token, observation(observed_at=NOW, control=CONTROL))
    session.expire_all()

    system = session.scalars(select(System)).one()
    session.add_all(
        [
            Talkgroup(system_id=system.id, tgid=1201),  # nothing set: RR fills both
            Talkgroup(system_id=system.id, tgid=1202, category="interop"),  # operator's bucket
            Talkgroup(system_id=system.id, tgid=1203, service_tag="Interop", category="interop"),
            Talkgroup(system_id=system.id, tgid=1204),  # RR tag nothing here knows
        ]
    )
    session.commit()

    fake = FakeRadioReference(
        rr_sites(),
        talkgroups=[
            # only a tagId: the id -> name table has to be fetched to read it
            {"tgDec": 1201, "tgAlpha": "EC FIRE 1", "tags": [{"tagId": 42}]},
            {"tgDec": 1202, "tgAlpha": "EC LAW 1", "tags": [{"tagId": 41, "tagDescr": "Law Dispatch"}]},
            {"tgDec": 1203, "tgAlpha": "EC LAW 2", "tags": [{"tagId": 44, "tagDescr": "Law Tac"}]},
            {"tgDec": 1204, "tgAlpha": "EC WX", "tags": [{"tagId": 99, "tagDescr": "Weather Net"}]},
        ],
    )
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}, "rr_sync_hours": 24})
    summaries = sync_due(session, settings=tuned, client=fake, force=True, now=NOW)
    session.commit()

    filed = {
        row.tgid: (row.service_tag, row.category)
        for row in session.scalars(select(Talkgroup).where(Talkgroup.tgid >= 1201))
    }
    assert filed[1201] == ("Fire Dispatch", "fire")  # blank: RR fills the tag and the bucket
    assert filed[1202] == ("Law Dispatch", "interop")  # the operator's category survives
    assert filed[1203] == ("Interop", "interop")  # already tagged: RR does not re-tag
    assert filed[1204] == ("Weather Net", None)  # unknown tag kept, category not guessed
    assert summaries[0].talkgroups_tagged == 3  # the one already tagged is not re-counted

    # The id -> name table is somebody else's server too: one read for the whole run,
    # taken only because a talkgroup arrived with a bare tagId.
    assert fake.calls.count(("tags", 0)) == 1


def test_rr_sync_asks_for_the_tag_table_only_when_it_needs_it(
    client, session, settings, upload_token
) -> None:
    """Every talkgroup carried its own ``tagDescr``, so ``getTag`` is never called."""
    from app.models import Talkgroup
    from app.rr.sync import sync_due

    post(client, upload_token, observation(observed_at=NOW, control=CONTROL))
    session.expire_all()
    system = session.scalars(select(System)).one()
    session.add(Talkgroup(system_id=system.id, tgid=1301))
    session.commit()

    fake = FakeRadioReference(
        rr_sites(),
        talkgroups=[{"tgDec": 1301, "tags": [{"tagId": 42, "tagDescr": "Fire Dispatch"}]}],
    )
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}})
    sync_due(session, settings=tuned, client=fake, force=True, now=NOW)
    session.commit()

    assert ("tags", 0) not in fake.calls
    row = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1301)).one()
    assert (row.service_tag, row.category) == ("Fire Dispatch", "fire")


def test_rr_sync_drops_a_frequency_radioreference_no_longer_lists(
    client, session, settings, upload_token
) -> None:
    from app.rr.sync import sync_due

    post(client, upload_token, observation(observed_at=NOW, control=CONTROL))
    session.expire_all()
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}})

    sync_due(session, settings=tuned, client=FakeRadioReference(rr_sites()), force=True, now=NOW)
    session.commit()
    trimmed = rr_sites()
    trimmed[0]["siteFreqs"] = [f for f in trimmed[0]["siteFreqs"] if f["freq"] != VOICE_GONE / 1e6]
    summaries = sync_due(session, settings=tuned, client=FakeRadioReference(trimmed), force=True, now=NOW)
    session.commit()

    assert summaries[0].frequencies_removed == 1
    site = session.scalars(select(Site).where(Site.name == "Central")).one()
    remaining = session.scalars(
        select(SiteFrequency.frequency_hz).where(
            SiteFrequency.site_id == site.id, SiteFrequency.source == "radioreference"
        )
    ).all()
    assert VOICE_GONE not in remaining


def test_an_ambiguous_site_is_skipped_rather_than_guessed(client, session, settings, upload_token, caplog) -> None:
    from app.rr.sync import sync_due

    post(client, upload_token, observation(observed_at=NOW, control=CONTROL))
    session.expire_all()
    stray = rr_sites()[0] | {"siteId": 9003, "siteNumber": 7, "siteDescr": "Somewhere else", "nac": 999}
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}})

    with caplog.at_level("INFO", logger="app.rr.sync"):
        summaries = sync_due(session, settings=tuned, client=FakeRadioReference([stray]), force=True, now=NOW)
    session.commit()

    assert summaries[0].sites_matched == 0
    assert summaries[0].unmatched == ["Somewhere else"]
    assert "does not map to a Keyup site" in caplog.text


# --------------------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------------------


@pytest.fixture
def audited_site(client, session, settings, upload_token):  # noqa: ANN001, ANN201
    """A site that exercises every category of the report at once."""
    from app.rr.sync import sync_due

    # A month ago: the site was on its documented control channel and carried two voice
    # channels, one of which nothing has heard since.
    post(
        client,
        upload_token,
        observation(observed_at=OLD, control=CONTROL, traffic=[(VOICE_STALE, 4), (VOICE_KNOWN, 9)]),
    )
    # A neighbor Keyup knows, because the recorder watches it too.
    post(client, upload_token, observation(observed_at=NOW, control=851_600_000, site="Westside", site_number=2))
    # Today: failed over to the backup control channel, carrying an undocumented channel,
    # and configured for a frequency the site never mentions.
    post(
        client,
        upload_token,
        observation(
            observed_at=NOW,
            control=BACKUP,
            secondary=[CONTROL],
            traffic=[(VOICE_NEW, 12), (VOICE_KNOWN, 3)],
            configured=[BACKUP, CONFIGURED_ONLY],
            adjacent=[
                {"system_id": 52, "rfss": 10, "site": 2, "lra": 10, "control_frequency_hz": 851_600_000},
                {"system_id": 57, "rfss": 20, "site": 20, "lra": 20, "control_frequency_hz": 851_087_500},
            ],
        ),
    )
    session.expire_all()
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}})
    sync_due(session, settings=tuned, client=FakeRadioReference(rr_sites()), force=True, now=NOW)
    session.commit()
    # The report is computed "now"; pin that to the scenario's NOW, as the alert tests pass
    # ``now=NOW``, or which channels count as missing drifts with the real date.
    from app.api.channel_audit import report_clock
    from app.main import app

    app.dependency_overrides[report_clock] = lambda: NOW
    try:
        yield session.scalars(select(Site).where(Site.name == "Central")).one()
    finally:
        app.dependency_overrides.pop(report_clock, None)


def test_the_audit_reports_every_category(client, audited_site) -> None:
    report = client.get(f"/api/directory/sites/{audited_site.id}/channel-audit").json()

    assert report["site"]["name"] == "Central"
    assert report["site"]["rr_site_id"] == 9001
    assert report["last_observed_at"].startswith("2026-08-31T08:00")

    control = report["control"]
    assert control["observed"] == BACKUP  # the site failed over
    assert control["rr_primary"] == [CONTROL]
    assert control["rr_alternate"] == [BACKUP]
    assert control["observed_alternate"] == [CONTROL]
    assert control["configured"] == [BACKUP, CONFIGURED_ONLY]

    assert [entry["frequency_hz"] for entry in report["undocumented"]] == [VOICE_NEW]
    assert report["undocumented"][0]["role"] == "voice"
    assert report["undocumented"][0]["grant_count"] == 12

    missing = {entry["frequency_hz"]: entry for entry in report["missing"]}
    assert set(missing) == {VOICE_STALE, VOICE_GONE}
    assert missing[VOICE_GONE]["last_seen_at"] is None  # never heard
    assert missing[VOICE_STALE]["last_seen_at"].startswith("2026-08-01")  # heard, a month ago

    drift = {entry["frequency_hz"]: entry for entry in report["control_drift"]}
    assert drift[CONTROL] == {
        "frequency_hz": CONTROL,
        "rr_role": "primary_control",
        "observed_role": "alternate_control",
    }
    assert drift[BACKUP]["observed_role"] == "primary_control"

    config = report["recorder_config"]
    assert config["announced_not_configured"] == [CONTROL]
    assert config["configured_not_announced"] == [CONFIGURED_ONLY]
    assert config["suggested_control_frequencies"] == [BACKUP, CONTROL]  # live first

    neighbors = {entry["rfss"]: entry for entry in report["adjacent_sites"]}
    assert neighbors[10]["known_site_id"] is not None
    assert neighbors[10]["known_in_rr"] is True
    assert neighbors[20]["known_site_id"] is None
    assert neighbors[20]["known_in_rr"] is False


def test_the_system_summary_counts_each_site(client, audited_site) -> None:
    body = client.get(f"/api/directory/systems/{audited_site.system_id}/channel-audit").json()

    assert body["system"]["rr_system_id"] == 7376
    rows = {row["site"]: row for row in body["sites"]}
    assert set(rows) == {"Central", "Westside"}
    assert rows["Central"]["undocumented"] == 1
    assert rows["Central"]["missing"] == 2
    assert rows["Central"]["control_drift"] == 2
    assert rows["Central"]["adjacent_unknown"] == 1
    assert body["totals"]["undocumented"] == 1
    assert body["totals"]["missing"] == 2


def test_the_report_reads_its_clock_not_the_wall_clock(client, audited_site, monkeypatch) -> None:
    """Months after the fixture date the report still says what it said on NOW.

    The wall clock is pushed half a year ahead for both the route and the report module;
    only the injected clock may decide which channels are stale.
    """
    import app.api.channel_audit as route_module
    import app.rr.audit as audit_module

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return datetime(2027, 3, 1, 8, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(route_module, "datetime", Later)
    monkeypatch.setattr(audit_module, "datetime", Later)

    report = client.get(f"/api/directory/sites/{audited_site.id}/channel-audit").json()
    assert {entry["frequency_hz"] for entry in report["missing"]} == {VOICE_STALE, VOICE_GONE}
    body = client.get(f"/api/directory/systems/{audited_site.system_id}/channel-audit").json()
    assert body["totals"]["missing"] == 2


def test_an_unknown_site_is_a_404(client, session) -> None:
    assert client.get("/api/directory/sites/9999/channel-audit").status_code == 404
    assert client.get("/api/directory/systems/9999/channel-audit").status_code == 404


# --------------------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------------------


def _webhook_client(seen: list[dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": request.content.decode()})
        return httpx.Response(200)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_new_undocumented_frequency_alerts_once_and_only_once(session, audited_site, settings) -> None:
    from app.rr.audit import raise_new_findings

    session.add(
        AlertRule(
            name="channel audit",
            kind="channel_audit",
            channels=["webhook"],
            channel_config={"webhook": {"url": "https://example.invalid/hook"}},
        )
    )
    session.commit()

    seen: list[dict] = []
    with _webhook_client(seen) as http:
        first = raise_new_findings(session, audited_site.id, settings=settings, client=http, now=NOW)
        session.commit()
        second = raise_new_findings(session, audited_site.id, settings=settings, client=http, now=NOW)
        session.commit()

    # One undocumented frequency and one unknown neighbor, then nothing ever again.
    assert len(first) == 2
    assert second == []
    assert len(seen) == 2
    subjects = sorted(row.subject for row in session.scalars(select(Notification)))
    assert subjects == [
        "Keyup: undocumented channel at Central",
        "Keyup: unknown neighbor of Central",
    ]
    assert all(row.ok for row in session.scalars(select(Notification)))
    assert "856.7125 MHz" in seen[0]["body"]


def test_nothing_is_stamped_while_no_channel_audit_rule_exists(session, audited_site, settings) -> None:
    """Enabling a rule later must still report what is already on the books."""
    from app.rr.audit import raise_new_findings

    assert raise_new_findings(session, audited_site.id, settings=settings, now=NOW) == []
    session.commit()
    assert all(
        row.alerted_at is None
        for row in session.scalars(
            select(SiteFrequency).where(SiteFrequency.site_id == audited_site.id)
        )
    )

    session.add(AlertRule(name="late", kind="channel_audit", channels=["webhook"],
                          channel_config={"webhook": {"url": "https://example.invalid/hook"}}))
    session.commit()
    seen: list[dict] = []
    with _webhook_client(seen) as http:
        assert len(raise_new_findings(session, audited_site.id, settings=settings, client=http, now=NOW)) == 2


def test_the_worker_job_logs_a_line_per_site(session, audited_site, settings, caplog) -> None:
    from app.rr.audit import run_channel_audit

    with caplog.at_level("INFO", logger="app.rr.audit"):
        reports = run_channel_audit(session, settings=settings, now=NOW)
    session.commit()

    assert {report["site"]["name"] for report in reports} == {"Central", "Westside"}
    assert "Central — control 851.7875 MHz, 1 undocumented" in caplog.text


def test_match_site_prefers_nac_over_shared_site_number():
    """On a network where every tower numbers its site '1', an RR site is bound by
    NAC/RFSS, not by the shared site number (regression: the first RR site-1 used to
    hijack our site and overwrite its channel plan)."""
    from app.models import Site
    from app.rr.sync import _nac_values, match_site

    central = Site(name="Central", site_number=1, nac=52, rfss=10)
    wrong = {"siteId": 111, "siteNumber": 1, "nac": "006", "rfss": 20, "siteDescr": "Elsewhere"}
    right = {"siteId": 222, "siteNumber": 1, "nac": "034", "rfss": 10, "siteDescr": "Central Co Simulcast"}

    assert match_site(wrong, [central])[0] is None
    site, how = match_site(right, [central])
    assert site is central and how == "nac"
    # A NAC-less RR site still binds on the RFSS+site pair, not the bare number.
    pair = {"siteId": 333, "siteNumber": 1, "rfss": 10, "siteDescr": "Central"}
    assert match_site(pair, [central]) == (central, "rfss+site_number")
    assert _nac_values("034") == {52} and _nac_values("052") == {82}


# --------------------------------------------------------------------------------------
# the RR site cache and naming the neighbors a control channel announces
# --------------------------------------------------------------------------------------


def test_rr_sync_caches_every_site_not_just_the_matched_one(
    client, session, settings, upload_token
) -> None:
    """The sync keeps one local site but every RR site lands in ``rr_sites`` — the list the
    audit resolves neighbors against, filled without any extra RR call."""
    from app.models import RrSite
    from app.rr.sync import sync_due

    post(client, upload_token, observation(observed_at=NOW, control=CONTROL))
    session.expire_all()

    # Two documented sites the recorder heard (Central maps, Westside does not exist locally)
    # plus a neighbor tower nobody hears — all three must be retained.
    sites = rr_sites() + [
        {
            "siteId": 9057,
            "siteNumber": 20,
            "siteDescr": "Northfield tower",
            "rfss": 20,
            "nac": "039",  # hex 0x39 == 57, the P25 system id the neighbor announces
            "county": "Northfield",
            "siteFreqs": [{"freq": Decimal("851.0875"), "use": "d", "lcn": 6}],
        }
    ]
    tuned = settings.model_copy(update={"rr_systems": {"Example System": 7376}})
    summaries = sync_due(session, settings=tuned, client=FakeRadioReference(sites), force=True, now=NOW)
    session.commit()

    assert summaries[0].sites_seen == 3
    assert summaries[0].sites_matched == 1  # only Central maps to a Keyup site
    assert summaries[0].rr_sites_cached == 3

    system = session.scalars(select(System)).one()
    cached = {row.rr_site_id: row for row in session.scalars(select(RrSite))}
    assert set(cached) == {9001, 9002, 9057}
    assert all(row.system_id == system.id for row in cached.values())
    northfield = cached[9057]
    assert northfield.name == "Northfield tower"
    assert northfield.county == "Northfield"
    assert (northfield.rfss, northfield.site_number) == (20, 20)
    assert northfield.nac == 57  # parsed from RR's hex
    assert northfield.control_frequency_hz == 851_087_500  # the use=='d' frequency, in hertz
    # Re-syncing upserts rather than duplicating.
    sync_due(session, settings=tuned, client=FakeRadioReference(sites), force=True, now=NOW)
    session.commit()
    assert session.scalar(select(func.count()).select_from(RrSite)) == 3


def test_the_audit_names_neighbors_from_the_rr_site_cache(client, session) -> None:
    """A neighbor resolves to an ``rr_sites`` row by the (rfss, site_number) pair, or by
    the control frequency alone, or to nothing at all."""
    from app.models import AdjacentSite, RrSite, Site, System
    from app.rr.audit import audit_site

    system = System(name="Example System", system_id=52)
    session.add(system)
    session.flush()
    home = Site(name="Central", system_id=system.id, site_number=1, rfss=10, nac=52)
    session.add(home)
    session.flush()

    session.add_all(
        [
            # Resolves by the (rfss, site_number) pair.
            RrSite(
                system_id=system.id, rr_site_id=9057, site_number=20, rfss=20, nac=57,
                name="Northfield tower", county="Northfield", control_frequency_hz=851_087_500,
            ),
            # Same pair is useless here — its (rfss, site_number) differs — so this one can
            # only be found by its control frequency.
            RrSite(
                system_id=system.id, rr_site_id=9082, site_number=150, rfss=150, nac=82,
                name="Pine tower", county="West Co", control_frequency_hz=770_106_250,
            ),
        ]
    )
    session.add_all(
        [
            AdjacentSite(  # matches by (rfss, site_number)
                site_id=home.id, system_id_p25=57, rfss=20, site_number=20,
                control_frequency_hz=851_087_500,
            ),
            AdjacentSite(  # the announced site_number (99) is wrong; only the freq matches
                site_id=home.id, system_id_p25=82, rfss=150, site_number=99,
                control_frequency_hz=770_106_250,
            ),
            AdjacentSite(  # nothing in the cache describes it
                site_id=home.id, system_id_p25=6, rfss=10, site_number=10,
                control_frequency_hz=769_768_750,
            ),
        ]
    )
    session.commit()

    report = audit_site(session, home.id)
    by_system = {entry["system_id_p25"]: entry for entry in report["adjacent_sites"]}

    assert by_system[57]["known_in_rr"] is True
    assert by_system[57]["known_site_name"] == "Northfield tower"
    assert by_system[57]["known_county"] == "Northfield"
    assert by_system[57]["match"] == "rfss+site_number"

    assert by_system[82]["known_in_rr"] is True
    assert by_system[82]["known_site_name"] == "Pine tower"
    assert by_system[82]["match"] == "control_frequency"

    assert by_system[6]["known_in_rr"] is False
    assert by_system[6]["known_site_name"] is None
    assert by_system[6]["known_county"] is None
    assert by_system[6]["match"] is None
