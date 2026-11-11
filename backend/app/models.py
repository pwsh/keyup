# ruff: noqa: E501
"""ORM schema — the contract shared by ingest, STT, API.

Keep in sync with docs/ARCHITECTURE.md (§3) and the Alembic migrations in alembic/versions/.

Conventions
-----------
* every instant is ``timestamptz`` (UTC); ``calls.started_local`` additionally carries the
  naive local time (the ``TZ`` setting) because CAD systems store naive local time
* enumerations are native Postgres enums with an explicit name, so a migration can ALTER
  TYPE ... ADD VALUE rather than rewriting a check constraint
* ``calls.talkgroup_ids`` holds *tgids* (the radio-network numbers), not primary keys, so
  a patch call answers "calls involving talkgroup X" without a join
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------
# enumerations (§3.2)
# --------------------------------------------------------------------------------------

#: how a call reached us — a watched/scanned sdrtrunk file, a recorder push (§1.6), a
#: trunk-recorder capture directory whose ``.json`` sidecar came with it (§1.7), or a
#: trunkd stream (§1.6), which pushes over the same Rdio-Scanner contract and names itself
#: with ``recorder=trunkd`` because its form is otherwise indistinguishable from
#: trunk-recorder's
INGEST_SOURCES = ("file", "upload", "trunk_recorder", "trunkd")
IngestSourceEnum = Enum(*INGEST_SOURCES, name="ingest_source")

#: what an ``ingest_sources`` row *is*.  Three shapes, because the three ways a recording
#: reaches the archive are genuinely different objects and pretending otherwise would give
#: the operator one form with most of its fields greyed out:
#: ``watch_dir`` — a directory this deployment watches and scans (the AUDIO_ROOT mechanism,
#: now one row among possibly several); ``push_token`` — a recorder that POSTs to
#: ``/api/call-upload`` with an upload token, so the row is a *label* for a token rather
#: than something the watcher acts on; ``migration`` — a one-off bulk import, which owns no
#: runtime behavior at all and exists so its progress has somewhere to be reported.
INGEST_SOURCE_KINDS = ("watch_dir", "push_token", "migration")
IngestSourceKindEnum = Enum(*INGEST_SOURCE_KINDS, name="ingest_source_kind")

#: how ``calls.started_at`` was derived.  ``file_write`` is the sdrtrunk file path: the
#: stamp the recorder writes is the instant it *closed* the file, measured at ``call end +
#: 1.7 s`` against trunkd's control-channel grant times (§1.2), so ``started_at`` is that
#: minus the duration and runs ≈ 2 s late.  ``upload`` is the recorder's own call-start
#: epoch; ``sidecar`` is trunk-recorder's ``start_time_ms``, which is exact (§1.7).  That
#: 2 s is what ``RECONCILE_CLOCK_SKEW_SECONDS`` is sized for (docs/RECONCILIATION.md).
TIME_SOURCES = ("file_write", "upload", "event_log", "sidecar")
TimeSourceEnum = Enum(*TIME_SOURCES, name="time_source")

CALL_TYPES = ("group", "patch", "individual")
CallTypeEnum = Enum(*CALL_TYPES, name="call_type")

#: **Who the transmission was addressed to**, which is a different question from
#: ``call_type`` (how the channel was granted).  ``group`` is everything the archive held
#: before this existed: a talkgroup call, patched or not.  ``unit`` is a P25 *private*
#: call — one radio to one radio, with no talkgroup at all, which is why it is a column and
#: not another ``call_type`` value: a unit call has nothing to file under a talkgroup, and
#: every consumer that keys on one (the exporters, the directory, the calls table) has to
#: be able to ask "is there a talkgroup here" without inspecting NULLs and guessing why.
#: Plain text rather than an enum: the vocabulary is two words and adding a third must not
#: need an ``ALTER TYPE`` on a table this size (docs/RECORDERS.md, "Unit-to-unit calls").
CALL_KINDS = ("group", "unit")

TRANSCRIPT_STATUSES = ("pending", "queued", "running", "done", "skipped", "failed", "empty")
TranscriptStatusEnum = Enum(*TRANSCRIPT_STATUSES, name="transcript_status")

#: hallucination scoring (§5.2): ``suspect`` still has text, ``noise`` is thrown away
TRANSCRIPT_QUALITIES = ("good", "suspect", "noise")
TranscriptQualityEnum = Enum(*TRANSCRIPT_QUALITIES, name="transcript_quality")

#: ``manual`` is not an engine: it is the transcript row a person's caption becomes on a call
#: that had none (``PATCH /api/calls/{id}``), so the worker can see it and keep it primary.
STT_ENGINES = ("openai_compatible", "faster_whisper_local", "openvino_genai", "ovms", "manual")
SttEngineEnum = Enum(*STT_ENGINES, name="stt_engine")

JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
JobStatusEnum = Enum(*JOB_STATUSES, name="job_status")

#: where the recording's audio is: still on disk, moved to the archive tier, or deleted
#: (retention, §5.3).  ``archived`` rows keep ``calls.file_path`` pointing at the moved file.
#: ``discarded`` is the *fourth* way audio can be absent and it is not retention's: the
#: talkgroup's ``record`` toggle was off, so the recording was kept only long enough to
#: transcribe and export it and then deleted (§5.6).  Kept apart from ``purged`` because
#: "expired after its window" and "was never meant to be kept" are different facts about
#: the same missing file, and an operator asking "where did the audio go" deserves the
#: right one.  Every reader that tests ``audio_state <> 'present'`` is already correct for
#: it.  ``missing`` is retention finding no file where the row said there was one — not a
#: decision anybody made, so kept apart from ``purged``, and reversible: the next pass that
#: finds the file again puts the row back to ``present``.
AUDIO_STATES = ("present", "archived", "purged", "discarded", "missing")
AudioStateEnum = Enum(*AUDIO_STATES, name="audio_state")

RADIO_KINDS = ("console", "portable", "mobile", "unknown")
RadioKindEnum = Enum(*RADIO_KINDS, name="radio_kind")

#: what a ``units`` row *is* (§3.1).  A unit is the thing a callsign names, and on this
#: system that is nearly always a vehicle (``E1``, ``A21``) or a fixed station
#: (``Station 3``); ``team`` covers a named crew that moves between vehicles, and ``other``
#: is the honest default for a callsign nobody has classified yet — which is most of them
#: right after an import, so it is what the column defaults to rather than a guess.
UNIT_KINDS = ("vehicle", "station", "team", "other")
UnitKindEnum = Enum(*UNIT_KINDS, name="unit_kind")

LINK_CONFIDENCES = ("strong", "probable", "weak")
LinkConfidenceEnum = Enum(*LINK_CONFIDENCES, name="link_confidence")

#: How a link was decided.  ``time`` is nothing but the window; ``time+agency`` adds that
#: the call was on the agency's channel; ``unit`` that the transmitting radio carries the
#: apparatus id; ``text`` that the transcript names words specific to the incident, and
#: ``time+text`` that the transcript is the only corroboration the window has.  ``manual``
#: is a person saying so, and is the only value the API will fill in for you.
LINK_METHODS = ("time", "time+agency", "unit", "text", "time+text", "manual")
LinkMethodEnum = Enum(*LINK_METHODS, name="link_method")

INGEST_FILE_STATUSES = ("ingested", "duplicate", "failed", "ignored")
IngestFileStatusEnum = Enum(*INGEST_FILE_STATUSES, name="ingest_file_status")

#: One call's fate on one outbound export service (``call_exports``, docs/EXPORTERS.md).
#: ``pending`` is waiting for the exporter worker; ``uploaded`` succeeded exactly once;
#: ``skipped`` is a call the service must never be sent (encrypted, no audio, no
#: talkgroup); ``failed`` is parked after ``EXPORT_MAX_ATTEMPTS`` tries and never retried
#: automatically again.
CALL_EXPORT_STATUSES = ("pending", "uploaded", "skipped", "failed")
CallExportStatusEnum = Enum(*CALL_EXPORT_STATUSES, name="call_export_status")

#: The outbound services :mod:`app.exporters` knows how to upload to.
CALL_EXPORT_SERVICES = ("broadcastify", "openmhz")

#: The events an outbound webhook may subscribe to (:mod:`app.webhooks`,
#: docs/WEBHOOKS.md).  Deliberately four and not "everything on the live bus":
#: each one is a fact a consumer can act on rather than a state change it would have to
#: interpret, and a vocabulary a subscriber can enumerate is one they can build against.
#: ``call.stored`` fires when a recording is in the archive and playable — *before* it is
#: transcribed, because a consumer that wants the audio should not wait on an engine;
#: ``call.transcribed`` fires when text exists for it.
WEBHOOK_EVENTS = ("call.stored", "call.transcribed", "link.created", "link.deleted")

#: One outbound delivery's fate.  ``pending`` is due or backing off; ``delivered`` got a
#: 2xx exactly once; ``failed`` is parked after ``WEBHOOK_MAX_ATTEMPTS`` and never retried
#: automatically again — a receiver that has refused five times is refusing for a reason a
#: sixth try will not fix, and a queue that retries for ever is a queue that never drains.
WEBHOOK_DELIVERY_STATUSES = ("pending", "delivered", "failed")
WebhookDeliveryStatusEnum = Enum(*WEBHOOK_DELIVERY_STATUSES, name="webhook_delivery_status")

RETENTION_SCOPES = ("global", "system", "talkgroup")
RetentionScopeEnum = Enum(*RETENTION_SCOPES, name="retention_scope")

#: ``channel_audit`` is not fired by a call event — the site channel audit (§3.5) raises it
#: itself, once per newly-seen frequency or adjacent site.  ``new_kind`` and
#: ``exemplar_changed`` are the same arrangement for the control-channel message census
#: (:mod:`app.rr.message_kinds`): a ``mfid:opcode`` nobody has heard before on a site, and
#: a beacon whose undecoded bytes changed.  ``disk_low`` is the worker's disk check
#: (:mod:`app.disk_watch`): free space crossed the warning or the critical threshold.
#: ``health`` is the worker's once-a-minute health check (:mod:`app.health`): a recorder
#: gone silent, ingest stalled, transcription stalled, a backup overdue — raised and
#: cleared once each.  ``emergency`` fires on ``call.new`` for a call whose P25 emergency
#: flag is set.
ALERT_KINDS = (
    "talkgroup",
    "radio",
    "keyword",
    "regex",
    "channel_audit",
    "new_kind",
    "exemplar_changed",
    "disk_low",
    "health",
    "emergency",
)
AlertKindEnum = Enum(*ALERT_KINDS, name="alert_kind")

#: what a frequency is used for at a site.  ``unknown`` is what a source that names a
#: frequency without saying what it carries gets.
SITE_FREQUENCY_ROLES = ("primary_control", "alternate_control", "voice", "unknown")
SiteFrequencyRoleEnum = Enum(*SITE_FREQUENCY_ROLES, name="site_frequency_role")

#: who says so — the air (the recorder), RadioReference, or the recorder's own config.
#: One row per (site, frequency, source), so "documented but never heard" is a join and
#: not a guess.
SITE_FREQUENCY_SOURCES = ("observed", "radioreference", "configured")
SiteFrequencySourceEnum = Enum(*SITE_FREQUENCY_SOURCES, name="site_frequency_source")

#: what a ``radio_events`` row is (docs/RADIO_EVENTS.md).  The vocabulary is the P25
#: registration/affiliation/data family the recorder can actually see — everything the FNE
#: sends *downlink*; nothing inbound from a subscriber is observable at all.  Anything the
#: recorder decodes that is not one of the first eleven is stored as ``other`` with the
#: opcode and the decoded fields in ``meta``, so a message nobody has modeled yet still
#: lands in the archive instead of being dropped.
RADIO_EVENT_KINDS = (
    "unit_registration",
    "unit_deregistration",
    "unit_registration_command",
    "group_affiliation",
    "group_affiliation_query",
    "location_registration",
    "sndcp_grant",
    "sndcp_page",
    # a unit-to-unit (private) voice grant — one radio calling one radio.  The event is
    # what the control channel said; the recording, when there is one, arrives separately
    # as a call with ``kind='unit'`` (docs/RADIO_EVENTS.md, docs/RECORDERS.md).
    "private_call",
    "ack",
    "deny",
    "queued",
    "other",
)
RadioEventKindEnum = Enum(*RADIO_EVENT_KINDS, name="radio_event_kind")

#: how a ``radio_affiliations`` row came to exist.  ``call`` is the original meaning —
#: "this radio was heard talking on that talkgroup".  ``affiliation`` is the stronger one
#: the control channel gives: the radio *registered* on the talkgroup, whether or not it
#: ever keyed up.  The two are kept apart rather than silently merged
#: (docs/RADIO_EVENTS.md); ``affiliation`` wins when both are true.
RADIO_AFFILIATION_SOURCES = ("call", "affiliation")
RadioAffiliationSourceEnum = Enum(*RADIO_AFFILIATION_SOURCES, name="radio_affiliation_source")

#: What a recorder is *for* when several of them hear the same site (docs/RECORDERS.md,
#: "Standby and the arbiter").  ``hot`` is an ordinary recorder: it records whenever it is
#: healthy.  ``warm`` is a spare kept registered with its tuners disabled — the arbiter
#: enables them when the hot recorders covering a site all go quiet, and disables them
#: again once the hot side has been back for the recovery window.  ``cold`` is a box the
#: arbiter must never touch at all: a bench radio, a laptop, something an operator is
#: experimenting with.  The distinction between ``warm`` and ``cold`` is the whole point of
#: the column — "registered but idle" and "registered and automatable" look identical from
#: the outside, and guessing wrong means the arbiter keys up somebody's workbench.
RECORDER_STANDBY = ("hot", "warm", "cold")
RecorderStandbyEnum = Enum(*RECORDER_STANDBY, name="recorder_standby")

#: agency kinds: the six a public-safety agency directory needs
AGENCY_KINDS = ("dispatch", "police", "sheriff", "university", "fire", "ems")

#: talkgroup categories (sdrtrunk alias "tag" equivalents); free text, these are the ones
#: the UI knows how to color.  Widened from the original eight to cover every kind of
#: channel a RadioReference service tag can name (:data:`SERVICE_TAG_TO_CATEGORY`), so a
#: synced talkgroup lands in a bucket the UI can draw rather than in "other".  Purely
#: additive — every one of the original eight is still here, so no row needed rewriting.
TALKGROUP_CATEGORIES = (
    "law",
    "fire",
    "ems",
    "multi",
    "interop",
    "pw",
    "utility",
    "transit",
    "rail",
    "aviation",
    "schools",
    "corrections",
    "security",
    "federal",
    "military",
    "media",
    "business",
    "ham",
    "data",
    "other",
)

#: RadioReference's own service-tag vocabulary, verbatim and in RR's spelling — the
#: ``tagDescr`` its ``getTag`` table returns.  ``talkgroups.service_tag`` stores one of
#: these unchanged rather than a slug of it: it is somebody else's classification, and
#: keeping RR's own words means a re-sync, an export or a support question can be matched
#: back to the RR database page without a translation table.  The coarse local bucket the
#: UI colors and filters by is ``category``, derived through
#: :data:`SERVICE_TAG_TO_CATEGORY`.
RR_SERVICE_TAGS = (
    "Aircraft",
    "Business",
    "Corrections",
    "Data",
    "Deprecated",
    "Emergency Ops",
    "EMS Dispatch",
    "EMS-Tac",
    "EMS-Talk",
    "Federal",
    "Fire Dispatch",
    "Fire-Tac",
    "Fire-Talk",
    "Ham",
    "Hospital",
    "Interop",
    "Law Dispatch",
    "Law Tac",
    "Law Talk",
    "Media",
    "Military",
    "Multi-Dispatch",
    "Multi-Tac",
    "Multi-Talk",
    "Other",
    "Public Works",
    "Railroad",
    "Schools",
    "Security",
    "Transportation",
    "Utilities",
)

#: RR service tag → the ``category`` a talkgroup that carries it belongs in.
#:
#: RR splits a service by *role* (dispatch / tac / talk), which is a distinction worth
#: keeping in ``service_tag`` — "Fire-Tac" is a different channel from "Fire Dispatch" —
#: but not one worth coloring differently, so all three fire roles collapse to ``fire``.
#: ``Hospital`` joins ``ems`` because it is the same traffic from the receiving end, and
#: ``Emergency Ops`` joins ``multi`` because an EOC channel is by definition several
#: services at once.  ``Deprecated`` is RR's "this used to be something"; there is nothing
#: to say about it beyond ``other``.
SERVICE_TAG_TO_CATEGORY = {
    "Aircraft": "aviation",
    "Business": "business",
    "Corrections": "corrections",
    "Data": "data",
    "Deprecated": "other",
    "Emergency Ops": "multi",
    "EMS Dispatch": "ems",
    "EMS-Tac": "ems",
    "EMS-Talk": "ems",
    "Federal": "federal",
    "Fire Dispatch": "fire",
    "Fire-Tac": "fire",
    "Fire-Talk": "fire",
    "Ham": "ham",
    "Hospital": "ems",
    "Interop": "interop",
    "Law Dispatch": "law",
    "Law Tac": "law",
    "Law Talk": "law",
    "Media": "media",
    "Military": "military",
    "Multi-Dispatch": "multi",
    "Multi-Tac": "multi",
    "Multi-Talk": "multi",
    "Other": "other",
    "Public Works": "pw",
    "Railroad": "rail",
    "Schools": "schools",
    "Security": "security",
    "Transportation": "transit",
    "Utilities": "utility",
}

#: lower-cased RR tag → its canonical spelling, so an operator or a CSV that writes
#: "fire-tac" or "EMS DISPATCH" still lands on the one value the rest of the app compares.
RR_SERVICE_TAGS_BY_LOWER = {tag.lower(): tag for tag in RR_SERVICE_TAGS}


def canonical_service_tag(value: str | None) -> str | None:
    """RR's canonical spelling of ``value``, or ``None`` when it names no known tag.

    Blank is ``None`` too: "" and NULL both mean "nobody has classified this channel",
    and storing the empty string would make every "is it set?" test read twice.
    """
    # ``raw``, not ``text``: this module imports SQLAlchemy's ``text`` at the top.
    raw = (value or "").strip()
    if not raw:
        return None
    return RR_SERVICE_TAGS_BY_LOWER.get(raw.lower())


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


TZ = DateTime(timezone=True)


# --------------------------------------------------------------------------------------
# §3.1 directory — who is who
# --------------------------------------------------------------------------------------


#: Where an ``agencies`` row came from.  ``seed`` is the agency list loaded by ``keyup
#: seed-agencies`` (a CSV the deployment supplies, usually the dispatch center's own codes);
#: ``local`` is an agency added in this app because that list has no code for it — a
#: neighbouring county, a railroad, a school district.  The distinction is what lets
#: :func:`app.seed.seed_agencies` refresh its own rows without ever touching somebody's.
#: The agency the shared dispatch channels are mapped to is the runtime setting
#: ``shared_agency_code`` (``keyup mark-shared``), not a constant.
AGENCY_SOURCES = ("seed", "local")

#: The shape a locally added agency code has to have.  Upper case, digits, ``_`` and ``-``,
#: 2-16 characters: wide enough for the ORI-style codes a CAD system issues (``XX0000000``)
#: so a local row can later be promoted to a real one by renaming nothing, and narrow enough
#: that the code stays safe in a ``?agency=`` query string and a CSV column.
AGENCY_CODE_PATTERN = r"^[A-Z0-9_-]{2,16}$"


class Agency(Base):
    """Reference table of agencies, seeded from the deployment's agency list.

    ``talkgroups.agency_code`` and ``radios.agency_code`` point here, which is what lets
    ``?agency=`` answer "which channels would a Central City PD call at 14:32 be on?".
    Only the four columns an agency list needs are kept (code, name, short name, kind),
    so a CAD system's own list can be loaded as it is.

    Three more columns are Keyup's own.  ``source`` says
    whether the row came from the shared seed or was added here; ``active`` takes a
    disbanded or merged agency out of the pickers without deleting the rows that point at
    it; ``edited_at`` records that somebody here renamed a seeded row, which is what stops
    the next ``keyup seed-agencies`` from putting the old name back.
    """

    __tablename__ = "agencies"

    code: Mapped[str] = mapped_column(Text, primary_key=True)  # EXAMPLEPD, EXAMPLEFD, ECC
    name: Mapped[str] = mapped_column(Text, nullable=False)  # CENTRAL CITY POLICE DEPARTMENT
    short_name: Mapped[str] = mapped_column(Text, nullable=False)  # CCPD
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # see AGENCY_KINDS
    source: Mapped[str] = mapped_column(Text, nullable=False, default="local")  # AGENCY_SOURCES
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: when an operator last edited this row through the admin API; NULL means never
    edited_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        Index("ix_agencies_kind", "kind"),
        Index("ix_agencies_source", "source"),
    )


#: Protocol names that describe a **trunked** system, matched case-insensitively as
#: substrings of ``systems.protocol``.  That column is free text written by whatever
#: decoded the system (sdrtrunk's ``P25 Phase 1``, a RadioReference ``Motorola SmartZone``,
#: an operator's ``DMR Tier III``), so this is a *guess* — which is exactly why
#: ``systems.trunked`` exists to overrule it.
TRUNKED_PROTOCOL_HINTS = (
    "p25",
    "apco",
    "phase 1",
    "phase 2",
    "dmr",
    "mototrbo",
    "motorola",
    "smartnet",
    "smartzone",
    "edacs",
    "ltr",
    "nxdn",
    "tetra",
    "trunk",
)

#: …and the words that say the opposite loudly enough to win, because several of the hints
#: above name a *family* with a conventional half: ``DMR conventional`` and ``Motorola
#: analog`` are not trunked systems however much of "dmr"/"motorola" is in them.
CONVENTIONAL_PROTOCOL_HINTS = ("conventional", "analog", "analogue", "narrowband fm")

#: The two lists as POSIX regexes, for the SQL half of the same question
#: (:func:`app.exporters.gate.trunked_system_clause`).
TRUNKED_PROTOCOL_REGEX = "(" + "|".join(TRUNKED_PROTOCOL_HINTS) + ")"
CONVENTIONAL_PROTOCOL_REGEX = "(" + "|".join(CONVENTIONAL_PROTOCOL_HINTS) + ")"


def infer_trunked(protocol: str | None) -> bool:
    """Does this protocol name describe a trunked system?  Blank → ``False``.

    Fails **closed**: a system nothing has said anything about is not published to
    Broadcastify Calls, which takes trunked systems only.  An operator who disagrees sets
    ``systems.trunked`` and is never overruled by this.
    """
    name = (protocol or "").strip().lower()
    if not name:
        return False
    if any(hint in name for hint in CONVENTIONAL_PROTOCOL_HINTS):
        return False
    return any(hint in name for hint in TRUNKED_PROTOCOL_HINTS)


class System(Base):
    """A trunked radio system as sdrtrunk names it (``Example System``).

    ``wacn``/``system_id`` are filled from the fully-qualified radio form
    (``101010.57.9831120`` → WACN 101010) the first time one is seen.
    """

    __tablename__ = "systems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    wacn: Mapped[int | None] = mapped_column(BigInteger)  # 101010
    system_id: Mapped[int | None] = mapped_column(Integer)  # the P25 system id
    nac: Mapped[int | None] = mapped_column(Integer)  # network access code its sites key
    protocol: Mapped[str | None] = mapped_column(Text)  # 'P25 Phase 1' (COMM Decoder)
    #: Is this a **trunked** system?  ``NULL`` means "nobody has said", and is read as
    #: :func:`infer_trunked` of ``protocol`` — so a system keyup discovered on its own is
    #: judged by what the decoder called it, and an operator who knows better sets the
    #: column.  It exists because Broadcastify **Calls** accepts trunked systems only
    #: (docs/EXPORTERS.md): :mod:`app.exporters.gate` refuses a call whose system is not
    #: trunked, whatever else is selected.  The Feed and OpenMHz are not gated on it.
    trunked: Mapped[bool | None] = mapped_column(Boolean)
    description: Mapped[str | None] = mapped_column(Text)
    #: RadioReference's ``sid`` for this system (§3.5); NULL until an operator maps it,
    #: either in ``RR_SYSTEMS`` or through ``PUT /api/admin/systems/{id}/radioreference``.
    rr_system_id: Mapped[int | None] = mapped_column(Integer)
    rr_synced_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    sites: Mapped[list[Site]] = relationship(back_populates="system")

    @property
    def is_trunked(self) -> bool:
        """The answer everything should ask: the column when set, the protocol otherwise.

        One helper rather than the same coalesce in the gate, the API and the UI, because
        "is this trunked" has to mean exactly one thing — a system Broadcastify Calls
        would accept — in all three.
        """
        if self.trunked is not None:
            return bool(self.trunked)
        return infer_trunked(self.protocol)


class Site(Base):
    """A site (tower group) of a system — sdrtrunk's ``Site`` in the filename and COMM."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 'Central'
    site_number: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    #: what the control channel announces about itself (RFSS/Network Status), filled from
    #: the first recorder observation that carries it and never overwritten (§3.5)
    nac: Mapped[int | None] = mapped_column(Integer)
    rfss: Mapped[int | None] = mapped_column(Integer)
    lra: Mapped[int | None] = mapped_column(Integer)
    #: RadioReference's ``siteId``; when set it wins over every matching heuristic
    rr_site_id: Mapped[int | None] = mapped_column(Integer)
    rr_synced_at: Mapped[datetime | None] = mapped_column(TZ)
    #: the operator's own note about this site, kept apart from ``description`` because the
    #: RadioReference sync writes that one and this one is nobody's but ours
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    system: Mapped[System] = relationship(back_populates="sites")

    __table_args__ = (
        UniqueConstraint("system_id", "name", name="uq_sites_system_name"),
        Index("ix_sites_system_id", "system_id"),
    )


class AudioProfile(Base):
    """A named ffmpeg filter chain a talkgroup can be processed through (§5.5).

    The audio-processing chain is global by design — the trim, the tone envelope and the
    loudness tail are the same promise for every recording — but the *noise* is not: a
    channel carrying medevac transports has rotor and turbine energy in a narrow band that
    no global denoiser setting removes without eating the speech on four hundred other
    channels.  A profile is that middle section, held as data: an ordered list of steps
    (``[{"kind": "equalizer", "params": {"f": 1950, …}}, …]``) validated against the
    whitelist in :mod:`app.audioproc.profiles` and spliced into the chain in place of the
    global denoiser and gate for the talkgroups pointed at it.

    ``chain`` is JSONB rather than a rendered ffmpeg string on purpose: the string is
    generated from the steps every time, so nothing an operator typed is ever stored in the
    form it would be executed in, and a step kind that gains a parameter does not have to
    re-parse a stored command line to find out what an old row meant.

    ``builtin`` marks the three shipped profiles.  They are editable — an operator who
    wants a different noise floor on "Default" should have it — and **not** deletable, so
    the names the documentation and the seeds refer to keep existing.
    """

    __tablename__ = "audio_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    chain: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Talkgroup(Base):
    """One talkgroup on a system, auto-created from ``TIT2`` and user-editable.

    The alias recorded on the call (``calls.talkgroup_alias_at_record``) is kept as well,
    so renaming a talkgroup here never rewrites history.  ``transcribe`` /
    ``transcribe_min_seconds`` / ``retention_days`` are the per-talkgroup policy the
    ingest and retention workers read; NULL means "use the global setting".
    """

    __tablename__ = "talkgroups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), nullable=False)
    tgid: Mapped[int] = mapped_column(Integer, nullable=False)  # 1101
    alias: Mapped[str | None] = mapped_column(Text)  # 'EC LAW 1'
    group_name: Mapped[str | None] = mapped_column(Text)  # sdrtrunk alias Group
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)  # see TALKGROUP_CATEGORIES
    #: RadioReference's own service tag for this channel, stored verbatim (see
    #: RR_SERVICE_TAGS).  ``category`` is the local bucket derived from it; both are
    #: operator-editable and the RR sync only ever fills a blank one.
    service_tag: Mapped[str | None] = mapped_column(Text)
    agency_code: Mapped[str | None] = mapped_column(ForeignKey("agencies.code", ondelete="SET NULL"))
    #: A county-wide dispatch or interop channel that no single department owns (EC LAW 1,
    #: FIRE DISPATCH, the EVENT channels).  ``agency_code`` answers "whose channel is this";
    #: this answers "is it everybody's", which an ``?agency=`` filter cannot infer from a
    #: blank owner.  Consumers read it to discover the set instead of hardcoding tgids.
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    color: Mapped[str | None] = mapped_column(Text)  # '#c0392b' or an sdrtrunk color int
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    #: Keep this channel's **audio**.  The other half of the pair ``transcribe`` used to be
    #: alone in: ``transcribe`` decides whether the words are worth keeping, this decides
    #: whether the recording is.  Default **true**, so it changes nothing on its own.
    #:
    #: False does *not* mean "do not listen".  The call is still ingested, still
    #: transcribed when ``transcribe`` is on, and still exported when an export service
    #: selects it — and only then is the file deleted (``calls.audio_policy``, §5.6).  It is
    #: when ``record`` and ``transcribe`` are *both* off and no exporter wants the channel
    #: that there is nothing left to do with the audio at all, and that combination is what
    #: the recorder is told to stop uploading (``skip_talkgroups`` in the desired state).
    record: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    transcribe: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    transcribe_min_seconds: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    #: Per-talkgroup export checkboxes (docs/EXPORTERS.md, "Which talkgroups").  Default
    #: **true** so the columns change nothing on their own: they are only consulted when
    #: ``export_<service>_talkgroup_mode`` is ``allow`` or ``deny``, and the shipped mode
    #: is ``all``.
    export_broadcastify: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: The **Calls** uploader's own checkbox, split from ``export_broadcastify`` above
    #: because Broadcastify sells two products off one set of credentials and an operator
    #: does not necessarily want the same channels on both: ``export_broadcastify`` is now
    #: read by the live **Feed** alone, this one by the per-call **Calls** upload alone.
    #: Default true, like its neighbours, and consulted only in ``allow``/``deny`` mode.
    #: Held false on a talkgroup whose system is not trunked — Calls publishes trunked
    #: systems only (docs/EXPORTERS.md).
    export_bcfy_calls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    export_openmhz: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    retention_days: Mapped[int | None] = mapped_column(Integer)  # NULL → global policy
    #: Which filter chain this channel's audio is processed through (:class:`AudioProfile`).
    #: NULL — the shipped state for every talkgroup — means the global ``audio_*`` settings,
    #: which is what the "Default" profile is a snapshot of.  ``SET NULL`` on delete rather
    #: than a restrict: deleting a profile that is in use is refused by the API with the
    #: counts, so reaching this is only ever a direct SQL delete, and falling back to the
    #: global chain is the right answer for one.
    audio_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_profiles.id", ondelete="SET NULL")
    )
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    first_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    system: Mapped[System] = relationship()

    __table_args__ = (
        UniqueConstraint("system_id", "tgid", name="uq_talkgroups_system_tgid"),
        Index("ix_talkgroups_tgid", "tgid"),
        Index("ix_talkgroups_agency_code", "agency_code"),
        Index("ix_talkgroups_category", "category"),
        Index("ix_talkgroups_audio_profile_id", "audio_profile_id"),
        # Near-spelling suggestions and the substring ``ILIKE`` of the search box.
        Index(
            "ix_talkgroups_alias_trgm",
            "alias",
            postgresql_using="gin",
            postgresql_ops={"alias": "gin_trgm_ops"},
        ),
    )


class Radio(Base):
    """A radio (subscriber unit).  ``radio_id`` is the number sdrtrunk put in ``TPE1``.

    A unit addressed by its home network appears fully qualified as ``WACN.SYS.ID``
    (``101010.57.9831120``); ``wacn`` and ``home_system_id`` carry those two extra parts so
    the same physical radio roaming onto our system is one row, and a plain ``9250057``
    from the local system is another (we cannot prove they are the same radio).

    ``unit_id`` is the apparatus name (``E1``, ``A21``) used to match the units an outside
    CAD system names; it is user-entered or imported, never guessed by ingest.

    ``unit_id`` is *managed by the current unit assignment* when one exists
    (:class:`RadioAssignment`): assigning a radio to a unit writes that unit's callsign
    here, and ending the assignment clears it again when it still holds that callsign.  A
    direct ``PATCH /radios/{id}`` still works — an operator who knows better than the
    roster is not overruled — but the next assignment change wins.  The column stays
    denormalized rather than becoming a join because it is the label the whole archive
    reads: ``coalesce(radios.unit_id, radios.alias, radios.radio_id)`` is computed on every
    row of every call list, and a join there would be paid a thousand times a page.
    """

    __tablename__ = "radios"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), nullable=False)
    radio_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 0 when the radio was recorded in the plain (local) form — part of the unique key,
    #: which is why it is NOT NULL with a 0 sentinel rather than nullable
    wacn: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    home_system_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    alias: Mapped[str | None] = mapped_column(Text)
    unit_id: Mapped[str | None] = mapped_column(Text)  # 'E1', 'A21' — the apparatus a CAD system names
    agency_code: Mapped[str | None] = mapped_column(ForeignKey("agencies.code", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(RadioKindEnum, nullable=False, server_default=text("'unknown'"))
    first_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    #: presence, from the control channel rather than from calls (docs/RADIO_EVENTS.md):
    #: when this radio last registered on a site, and when it last de-registered.  Both are
    #: NULL for a radio only ever seen talking.
    last_registered_at: Mapped[datetime | None] = mapped_column(TZ)
    last_deregistered_at: Mapped[datetime | None] = mapped_column(TZ)
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "system_id", "radio_id", "wacn", "home_system_id", name="uq_radios_system_radio_wacn_home"
        ),
        Index("ix_radios_radio_id", "radio_id"),
        Index("ix_radios_unit_id", "unit_id"),
        Index("ix_radios_agency_code", "agency_code"),
        # Near-spelling suggestions and the substring ``ILIKE`` of the search box.
        Index(
            "ix_radios_alias_trgm",
            "alias",
            postgresql_using="gin",
            postgresql_ops={"alias": "gin_trgm_ops"},
        ),
        Index(
            "ix_radios_unit_id_trgm",
            "unit_id",
            postgresql_using="gin",
            postgresql_ops={"unit_id": "gin_trgm_ops"},
        ),
    )


class Unit(Base):
    """A unit: the thing a callsign names — an engine, an ambulance, a station, a crew.

    This is the half of "who was that?" the radio itself cannot answer.  ``radios.unit_id``
    has always carried a callsign, but as free text on the radio row: it could not say what
    ``E1`` *is*, could not hold two agencies' identical callsigns apart, and could not
    survive the radio being handed to another apparatus.  A unit row can, and
    :class:`RadioAssignment` is what ties one to a radio for a stretch of time.

    Identity is ``(agency_code, callsign)``: ``E1`` means a different engine to Riverton
    than to Lakeside, and both must be able to exist.  Callsigns with *no* agency share one
    namespace of their own, enforced by a partial unique index rather than by the
    constraint — in SQL two NULL agency codes are not equal, so the plain unique constraint
    would happily admit ``E1`` twice.
    """

    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callsign: Mapped[str] = mapped_column(Text, nullable=False)  # 'E1', 'A21', 'Station 3'
    name: Mapped[str | None] = mapped_column(Text)  # 'Engine 1'
    agency_code: Mapped[str | None] = mapped_column(ForeignKey("agencies.code", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(UnitKindEnum, nullable=False, server_default=text("'other'"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("agency_code", "callsign", name="uq_units_agency_callsign"),
        # The NULL-agency half of that key; see the class docstring.
        Index(
            "uq_units_callsign_no_agency",
            "callsign",
            unique=True,
            postgresql_where=text("agency_code IS NULL"),
        ),
        Index("ix_units_callsign", "callsign"),
        Index("ix_units_agency_code", "agency_code"),
        Index(
            "ix_units_callsign_trgm",
            "callsign",
            postgresql_using="gin",
            postgresql_ops={"callsign": "gin_trgm_ops"},
        ),
        Index(
            "ix_units_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )


class Person(Base):
    """A person who carries a radio.  ``personnel`` on the wire and in SQL.

    Deliberately thin: a name, what to call them on the air (``rank``), the number the
    agency knows them by (``badge``), and whether they are still on the roster.  This
    archive is not a personnel system and must not grow into one — everything else about a
    person stays in whatever system actually owns it.

    ``active`` is a flag rather than a deletion because the archive is historical: a
    retired firefighter is still the right answer to "who was on that radio in 2024", so the
    row has to stay while dropping out of the pickers and the transcription vocabulary.
    """

    __tablename__ = "personnel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 'J. Smith'
    rank: Mapped[str | None] = mapped_column(Text)  # 'Captain'
    badge: Mapped[str | None] = mapped_column(Text)
    agency_code: Mapped[str | None] = mapped_column(ForeignKey("agencies.code", ondelete="SET NULL"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_personnel_name", "name"),
        Index("ix_personnel_agency_code", "agency_code"),
        Index(
            "ix_personnel_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )


class RadioAssignment(Base):
    """Who — or what — a radio belonged to, over a stretch of time.

    One row is one assignment of one radio to *either* a unit or a person, never both: the
    two are independent facts ("this radio lives on Engine 1" and "J. Smith is carrying it
    today"), so a radio may hold one current assignment of each kind at once and they end
    independently.  The ``CHECK`` is what keeps a row from being ambiguous about which
    statement it is making.

    History is closed-ended rather than overwritten — ``effective_to IS NULL`` means
    *current* — because the archive's whole purpose is answering questions about the past:
    a call from March must resolve against March's assignment, not today's.

    The two partial unique indexes are the real constraint: at most one *current* unit
    assignment and one *current* person assignment per radio.  Expressed as indexes and not
    as application logic because two operators clicking "assign" at the same moment is
    exactly the race that leaves a radio with two current units, and only the database can
    lose that race for them.
    """

    __tablename__ = "radio_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    radio_pk: Mapped[int] = mapped_column(
        ForeignKey("radios.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("personnel.id", ondelete="CASCADE"))
    effective_from: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    #: NULL means current; a closed row is history and is never edited again
    effective_to: Mapped[datetime | None] = mapped_column(TZ)
    note: Mapped[str | None] = mapped_column(Text)

    unit: Mapped[Unit | None] = relationship()
    person: Mapped[Person | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(unit_id IS NOT NULL) <> (person_id IS NOT NULL)",
            name="ck_radio_assignments_unit_xor_person",
        ),
        Index(
            "uq_radio_assignments_current_unit",
            "radio_pk",
            unique=True,
            postgresql_where=text("effective_to IS NULL AND unit_id IS NOT NULL"),
        ),
        Index(
            "uq_radio_assignments_current_person",
            "radio_pk",
            unique=True,
            postgresql_where=text("effective_to IS NULL AND person_id IS NOT NULL"),
        ),
        Index("ix_radio_assignments_radio", "radio_pk", "effective_from"),
        Index("ix_radio_assignments_unit", "unit_id"),
        Index("ix_radio_assignments_person", "person_id"),
    )


class Patch(Base):
    """A temporary supergroup (``P:9230 [19282, 50051, …]``).

    Dispatch regenerates the patch id every time the patch is re-created, so the *member
    set* is the identity; ``patch_id`` is kept because it is what the recorder printed.
    """

    __tablename__ = "patches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), nullable=False)
    patch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    member_tgids: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("system_id", "patch_id", "member_tgids", name="uq_patches_system_id_members"),
        Index("ix_patches_member_tgids", "member_tgids", postgresql_using="gin"),
    )


class RadioAffiliation(Base):
    """Rolling "this radio was last heard on that talkgroup" view.

    Maintained by ingest; powers the live "who's active" panel and a unit's channel
    history without scanning ``calls``.
    """

    __tablename__ = "radio_affiliations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    radio_id_fk: Mapped[int] = mapped_column(ForeignKey("radios.id", ondelete="CASCADE"), nullable=False)
    talkgroup_id_fk: Mapped[int] = mapped_column(
        ForeignKey("talkgroups.id", ondelete="CASCADE"), nullable=False
    )
    first_heard_at: Mapped[datetime | None] = mapped_column(TZ)
    last_heard_at: Mapped[datetime | None] = mapped_column(TZ)
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: what backs this row — ``call`` (heard talking) or ``affiliation`` (the control
    #: channel said so).  Once an affiliation event confirms the pair the row keeps
    #: ``affiliation`` for good: it is the stronger fact, and a later call does not weaken
    #: it.  Existing rows were all ``call`` when the column was added.
    source: Mapped[str] = mapped_column(
        RadioAffiliationSourceEnum, nullable=False, server_default=text("'call'")
    )
    #: when a ``group_affiliation`` event last accepted this radio onto this talkgroup;
    #: NULL when the row is only backed by calls
    last_affiliated_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        UniqueConstraint("radio_id_fk", "talkgroup_id_fk", name="uq_radio_affiliations_radio_talkgroup"),
        Index("ix_radio_affiliations_talkgroup", "talkgroup_id_fk", "last_heard_at"),
    )


class RadioEvent(Base):
    """One control-channel message *about a radio* — registration, affiliation, data.

    ``POST /api/recorder/events`` (docs/RADIO_EVENTS.md).  The archive knows what was
    *said*; this is who was *there*: a radio that turns on and affiliates to a talkgroup
    announces itself on the control channel long before it keys up, and until this table
    existed that radio was invisible.

    One table with a ``kind`` and a JSONB ``meta`` rather than a table per opcode family:
    the whole family is ~600 events an hour on a busy site, and the rare commands (status,
    call alert, radio-unit monitor) are individually the most interesting messages on a P25
    system and collectively too rare to model.  ``kind='other'`` catches them with the
    opcode intact.

    ``radio_pk``/``talkgroup_pk`` are the directory rows the event was folded into;
    ``radio_id``/``talkgroup_tgid`` are the numbers the air actually carried, kept even
    when the fold could not resolve them (an affiliation to a talkgroup no call has ever
    named still creates the talkgroup, but a de-registration names no talkgroup at all).

    Retention: this is presence data about identifiable people.  ``RADIO_EVENTS_KEEP_DAYS``
    (90 by default) purges it on the nightly pass, independently of the audio.
    """

    __tablename__ = "radio_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    system_id: Mapped[int | None] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(RadioEventKindEnum, nullable=False)
    #: the TSBK/LC opcode and manufacturer id exactly as decoded — 0 rather than NULL so
    #: they are usable in the idempotency key
    opcode: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    mfid: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    radio_pk: Mapped[int | None] = mapped_column(ForeignKey("radios.id", ondelete="SET NULL"))
    radio_id: Mapped[int | None] = mapped_column(BigInteger)
    #: the fully qualified form when the message carried one; 0 = local, as in ``radios``
    radio_wacn: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    radio_system: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: unit-to-unit / query target, when the message names a second radio
    target_radio_id: Mapped[int | None] = mapped_column(BigInteger)
    talkgroup_tgid: Mapped[int | None] = mapped_column(Integer)
    talkgroup_pk: Mapped[int | None] = mapped_column(ForeignKey("talkgroups.id", ondelete="SET NULL"))
    #: accept | deny | refused | fail | … — free text, verbatim from the decoder
    result: Mapped[str | None] = mapped_column(Text)
    frequency_hz: Mapped[int | None] = mapped_column(BigInteger)
    #: Which *recorder instance* decoded the message — trunkd's ``instance_id``, not the
    #: software name ``recorder`` carries elsewhere.  NULL on every row written before the
    #: field existed and on every batch from an older binary, which is what "we do not know
    #: which box heard this" honestly looks like.  Deliberately **not** a foreign key onto
    #: ``recorders``: forgetting a retired recorder must not erase what it heard, exactly
    #: as a call keeps the recorder's *name* rather than a reference to its row.
    instance: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        # The recorder spools and retries whole batches, so the same event arrives more
        # than once; this is what makes a retry free.  COALESCE'd because three of the six
        # parts are nullable and NULLs do not collide in a unique index.
        Index(
            "uq_radio_events_identity",
            text("COALESCE(site_id, 0)"),
            "at",
            "kind",
            "opcode",
            text("COALESCE(radio_id, -1)"),
            text("COALESCE(talkgroup_tgid, -1)"),
            unique=True,
        ),
        Index("ix_radio_events_at", text("at DESC")),
        Index("ix_radio_events_radio_at", "radio_pk", text("at DESC")),
        Index("ix_radio_events_talkgroup_at", "talkgroup_pk", text("at DESC")),
        Index("ix_radio_events_site_kind_at", "site_id", "kind", text("at DESC")),
        # There is deliberately no index leading on ``instance``.  The recorder page's
        # "everything this box heard" filter is always *also* scoped to a radio or a site,
        # there are three instances across the whole table, and the one that existed made
        # the planner lead on it and demote the selective column to a heap filter
        # (migration b8d4e60a17c2).
    )


# --------------------------------------------------------------------------------------
# §3.2 calls
# --------------------------------------------------------------------------------------


class Conversation(Base):
    """Consecutive calls on one talkgroup with gaps below ``CONVERSATION_GAP_SECONDS``.

    Derived and rebuilt incrementally on ingest.  It is what lets the UI show a thread and
    lets an outside consumer pull "the whole exchange" instead of one three-second key-up.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    talkgroup_id: Mapped[int | None] = mapped_column(ForeignKey("talkgroups.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    first_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("calls.id", ondelete="SET NULL", use_alter=True, name="fk_conversations_first_call")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_conversations_talkgroup_started", "talkgroup_id", "started_at"),
        Index("ix_conversations_ended_at", "ended_at"),
        # The grouper's own lookup (:func:`app.ingest.service.assign_conversation`):
        # talkgroup, then the newest thread still open at the call's time.  It is the only
        # index that bounds *both* halves of that question — the two above each answer half
        # and discard the talkgroup's whole history to answer the other, which is what made
        # the backlog regroup slower the further through the archive it got.
        Index("ix_conversations_talkgroup_ended", "talkgroup_id", "ended_at"),
        # The referencing side of the FK: deleting a call checks it (retention).
        Index("ix_conversations_first_call_id", "first_call_id"),
    )


class Call(Base):
    """One recorded transmission — the center of the schema.

    Time (§1.2): ``recorded_at`` is the raw fact (the ID3 ``COMM`` Date, which is the
    file-write instant, ≈ call end); ``started_at`` is ``recorded_at − duration_s`` for the
    file path and the recorder's own ``dateTime`` for the push path, and ``time_source``
    says which.  ``started_local`` is the same instant as naive local time (``TZ``), because
    CAD systems store naive local time.

    ``talkgroup_ids`` holds every tgid the transmission was heard on — one for a group
    call, all members for a patch — so "calls involving talkgroup X" is one GIN lookup and
    catches patched traffic.
    """

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), nullable=False)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    #: the primary talkgroup; for a patch, its first member
    talkgroup_id: Mapped[int | None] = mapped_column(ForeignKey("talkgroups.id", ondelete="SET NULL"))
    #: every tgid involved (patch members included) — GIN indexed
    talkgroup_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{}'::integer[]")
    )
    patch_id: Mapped[int | None] = mapped_column(ForeignKey("patches.id", ondelete="SET NULL"))
    radio_id: Mapped[int | None] = mapped_column(ForeignKey("radios.id", ondelete="SET NULL"))
    #: exactly as the recorder wrote it: '9250057' or '101010.57.9831120'
    radio_raw: Mapped[str | None] = mapped_column(Text)
    #: ``group`` or ``unit`` (:data:`CALL_KINDS`).  Every row written before unit calls
    #: existed is ``group``, which is what they were.
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'group'"))
    #: the radio a ``unit`` call was addressed **to** — the other end of a private call.
    #: NULL on every group call, and the reason ``radio_id`` alone is not enough to answer
    #: "what did this radio take part in": on a unit call it names only the caller.
    target_radio_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("radios.id", ondelete="SET NULL")
    )
    #: P25 talker alias — only the push path (§1.6) carries one
    talker_alias: Mapped[str | None] = mapped_column(Text)
    ingest_source: Mapped[str] = mapped_column(
        IngestSourceEnum, nullable=False, server_default=text("'file'")
    )
    #: false when the recorder never resolved a FROM (0.6 % of the corpus)
    radio_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: the admission class the push path was given — ``urgent``/``live``/``backlog``/``bulk``
    #: or a bare integer, NULL for everything that did not arrive over ``POST /call-upload``
    #: with an explicit ``priority``.  It is what makes "this row came from the migration"
    #: answerable in SQL long after the transcription job it set the priority of is gone
    #: (app/ingest/flow.py).
    ingest_priority: Mapped[str | None] = mapped_column(Text)
    #: Which recorder's copy of the transmission these bytes are, and what that recorder
    #: said about how well it heard it (``{"voice_frames", "bad_frames", "rssi_dbfs"}`` and
    #: whatever else it chooses to send).  Both are NULL for every call that did not arrive
    #: over ``POST /call-upload`` with a ``sourceQuality`` field.  They are what makes
    #: best-source negotiation possible at all: when a second receiver pushes the same
    #: transmission, the two quality reports are the only basis for deciding whose audio to
    #: keep, and keeping them on the row means "why is this rec-02's copy?" stays answerable
    #: long after the upload (docs/RECORDERS.md, "Best source").
    source_instance: Mapped[str | None] = mapped_column(Text)
    source_quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    recorded_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    started_local: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    time_source: Mapped[str] = mapped_column(
        TimeSourceEnum, nullable=False, server_default=text("'file_write'")
    )
    duration_s: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)

    frequency_hz: Mapped[int | None] = mapped_column(BigInteger)
    channel: Mapped[str | None] = mapped_column(Text)  # sdrtrunk channel id, '1-90'
    decoder: Mapped[str | None] = mapped_column(Text)  # 'P25 Phase 1'

    call_type: Mapped[str] = mapped_column(CallTypeEnum, nullable=False, server_default=text("'group'"))
    #: sdrtrunk files carry neither flag; a trunk-recorder sidecar and the push path do (§1.7)
    emergency: Mapped[bool | None] = mapped_column(Boolean)
    encrypted: Mapped[bool | None] = mapped_column(Boolean)
    #: trunk-recorder's ``audio_type``: 'analog' | 'digital' | 'digital tdma'
    audio_type: Mapped[str | None] = mapped_column(Text)
    #: sidecar radio measurements, dB as the recorder reported them
    signal_db: Mapped[int | None] = mapped_column(Integer)
    noise_db: Mapped[int | None] = mapped_column(Integer)
    freq_error: Mapped[int | None] = mapped_column(Integer)
    #: trunk-recorder's ``call_num`` — its own per-run sequence, useful when reading its logs
    recorder_call_num: Mapped[int | None] = mapped_column(Integer)
    #: the alias as recorded (TIT2, folder name) — survives a later talkgroup rename
    talkgroup_alias_at_record: Mapped[str | None] = mapped_column(Text)

    #: the recorder's own metadata, verbatim: the whole trunk-recorder sidecar, or the ID3
    #: frame map for an sdrtrunk file.  The detail page renders it and nothing parses it
    #: back — it is the audit trail for everything the columns above were derived from.
    raw_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # relative to AUDIO_ROOT
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(Text)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(SmallInteger)
    recorder_version: Mapped[str | None] = mapped_column(Text)  # TCOM
    file_mtime: Mapped[datetime | None] = mapped_column(TZ)
    #: retention tier of the MP3 (§5.3); ``audio_archived_at`` is when it left ``present``
    audio_state: Mapped[str] = mapped_column(
        AudioStateEnum, nullable=False, server_default=text("'present'")
    )
    audio_archived_at: Mapped[datetime | None] = mapped_column(TZ)
    #: What the ingest decided about keeping this recording, **at the moment it landed** —
    #: ``keep`` (every call on a channel with ``talkgroups.record`` on) or ``discard``.
    #: Written once and never re-derived, exactly like ``transcript_status``: turning Record
    #: back on must not make yesterday's calls keepers, and turning it off must not delete
    #: the archive of a channel that has been recording for a year.  The discard pass (§5.6)
    #: is the only reader.
    audio_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'keep'")
    )

    #: The processed copy of the recording — ``<stem>.proc.mp3`` beside the original, same
    #: layout and the same relative-to-``AUDIO_ROOT`` convention as ``file_path`` (§5.5).
    #: NULL until the ``audio_process`` job has run (or when it decided there was nothing
    #: worth writing).  The original is **never** modified: ``file_path`` and ``sha256``
    #: keep pointing at the bytes the recorder wrote, so the dedupe, the reconciliation and
    #: every "is this the same transmission" answer are unaffected by processing.
    processed_path: Mapped[str | None] = mapped_column(Text)
    #: What the processing pass measured and did — mean/peak dBFS before and after, the
    #: trim it took off each end, the gain it applied, the tone segments it found and the
    #: pipeline ``version``.  Kept so "why does this call sound like that" is answerable
    #: without re-running ffmpeg, and so a version bump can find the rows to redo.
    audio_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: A sustained pure tone was found in the audio (a paging tone, an alert beep).
    tone_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: This call is a *page*: a two-tone or alert pattern in its first seconds.  Deliberately
    #: **not** the P25 ``emergency`` flag — that one is what the radio asserted and is set by
    #: the recorder; this one is what the audio sounds like, and neither may overwrite the
    #: other.
    alert: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    #: set by reconciliation (§5.1 step 4); the row is kept and hidden from listings by
    #: default.  It points at the copy that won, which is not always the one that arrived
    #: first: a better copy takes the primary row and every pointer moves to it.
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    #: when reconciliation last ruled on this call — set on both sides of a pair
    reconciled_at: Mapped[datetime | None] = mapped_column(TZ)
    #: why it ruled that way: ``{"rule", "winner", "scores": {"<id>": {...}}, …}``.
    #: Kept so "why is my call hidden" is answerable without re-deriving anything
    #: (docs/RECONCILIATION.md).
    reconcile_reason: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )

    transcript_status: Mapped[str] = mapped_column(
        TranscriptStatusEnum, nullable=False, server_default=text("'pending'")
    )
    #: the primary transcript's text, denormalized onto the call for FTS and list views
    transcript_text: Mapped[str | None] = mapped_column(Text)
    transcript_quality: Mapped[str | None] = mapped_column(TranscriptQualityEnum)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(transcript_text, '') || ' ' || coalesce(notes, '') "
            "|| ' ' || coalesce(talkgroup_alias_at_record, ''))",
            persisted=True,
        ),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    ingested_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    talkgroup: Mapped[Talkgroup | None] = relationship()
    radio: Mapped[Radio | None] = relationship(foreign_keys=[radio_id])
    target_radio: Mapped[Radio | None] = relationship(foreign_keys=[target_radio_id])
    patch: Mapped[Patch | None] = relationship()
    site: Mapped[Site | None] = relationship()
    system: Mapped[System] = relationship()
    transcripts: Mapped[list[Transcript]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )
    sources: Mapped[list[CallSource]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="CallSource.seq"
    )

    __table_args__ = (
        Index("ix_calls_started_at", text("started_at DESC")),
        Index("ix_calls_talkgroup_started", "talkgroup_id", text("started_at DESC")),
        Index("ix_calls_radio_started", "radio_id", text("started_at DESC")),
        # "What did this radio take part in" reads both ends of a unit call, so the target
        # needs the same time-ordered index the source has.
        Index("ix_calls_target_radio_started", "target_radio_id", text("started_at DESC")),
        # Private calls are rare next to group traffic, so ``kind='unit'`` is worth a
        # partial index rather than one over every row in the archive.
        Index("ix_calls_kind_unit", text("started_at DESC"), postgresql_where=text("kind = 'unit'")),
        CheckConstraint("kind IN ('group', 'unit')", name="ck_calls_kind"),
        Index("ix_calls_talkgroup_ids", "talkgroup_ids", postgresql_using="gin"),
        Index("ix_calls_tags", "tags", postgresql_using="gin"),
        Index("ix_calls_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_calls_transcript_status", "transcript_status"),
        Index("ix_calls_starred", "starred", postgresql_where=text("starred")),
        Index("ix_calls_system_started", "system_id", text("started_at DESC")),
        Index("ix_calls_conversation", "conversation_id"),
        Index("ix_calls_duplicate_of", "duplicate_of"),
        # the reconciliation batch pass walks "everything ingested since the last pass"
        Index("ix_calls_ingested_at", text("ingested_at DESC")),
        # The discard pass's working set and nothing else (§5.6): a pass that finds nothing
        # — which is every pass on a deployment that has never turned Record off — is one
        # probe of an empty partial index rather than a scan of the archive.
        Index(
            "ix_calls_audio_policy_discard",
            "ingested_at",
            postgresql_where=text("audio_policy = 'discard' AND audio_state = 'present'"),
        ),
        Index("ix_calls_sha256", "sha256"),
        # The Sites page asks "how many calls at each tower", and the delete button on
        # every row asks the same question again.  Without an index that is a sequential
        # scan of the whole archive per request; with 1.1 M rows and a page that polls,
        # it was the single most expensive thing the API did (60-78 s per ``GET /sites``).
        # ``recorded_at`` rides along so "the newest call at this site" is the same index's
        # first entry rather than a second scan, and ``site_id`` alone is the leading
        # column, so the grouped count is an index-only scan.  Ascending, and named this
        # way, because production built it by hand during the incident and the migration
        # adopts what is already there rather than rebuilding it (``c1f70a4b82d5``).
        # Even so the count is ~1 s on 1.1 M rows, so the Sites listing caches it.
        Index("ix_calls_site_id_recorded_at", "site_id", "recorded_at"),
        # ``max(recorded_at)`` over the whole archive — the freshness stamp on the admin
        # dashboard and in the stats service.  ``ix_calls_started_at`` cannot answer it:
        # ``started_at`` is derived and the two orders differ by each call's duration.
        Index("ix_calls_recorded_at", text("recorded_at DESC")),
        Index(
            "ix_calls_audio_state",
            "audio_state",
            postgresql_where=text("audio_state <> 'present'"),
        ),
        # Pages and tone calls are a small minority of the archive, so both filters are
        # partial: the index covers only the rows anybody ever asks for by these names.
        Index("ix_calls_tone_detected", "tone_detected", postgresql_where=text("tone_detected")),
        Index("ix_calls_alert", "alert", postgresql_where=text("alert")),
    )


class CallSource(Base):
    """One keying radio inside one call — trunk-recorder's ``srcList`` (§1.7, §3.2).

    A trunked call is a conversation: the recorder holds the channel while several radios
    key up in turn, and the sidecar says which radio was talking at which offset into the
    audio.  ``calls.radio_id`` denormalizes the first of them so every existing query
    keeps working; these rows are what let the detail page seek to "the second unit".

    sdrtrunk cannot see past the first source, so its ingest writes exactly one row at
    ``pos_s = 0`` when the radio is known — the two dialects then read the same way.
    """

    __tablename__ = "call_sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    #: resolved directory row, when the radio id could be parsed
    radio_id: Mapped[int | None] = mapped_column(ForeignKey("radios.id", ondelete="SET NULL"))
    #: the radio id exactly as the recorder reported it
    src: Mapped[int | None] = mapped_column(BigInteger)
    #: seconds into the recording where this radio started talking
    pos_s: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    #: how long it talked — the next source's ``pos`` minus this one, or the call's end
    len_s: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: the recorder's alias for the radio ("E1", "MEDIC 3"), when it had one
    tag: Mapped[str | None] = mapped_column(Text)
    #: position in the sidecar's list, so the original order survives an equal ``pos``
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    call: Mapped[Call] = relationship(back_populates="sources")
    radio: Mapped[Radio | None] = relationship()

    __table_args__ = (
        Index("ix_call_sources_call", "call_id"),
        Index("ix_call_sources_radio", "radio_id"),
    )


class Transcript(Base):
    """One transcription attempt of one call.

    A call may have several (different engines, models or re-runs); exactly one carries
    ``is_primary`` and is the one denormalized into ``calls.transcript_text``.  A human
    correction sets ``edited_by_user`` and ``edited_text`` and never overwrites what the
    engine actually said.
    """

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    engine: Mapped[str] = mapped_column(SttEngineEnum, nullable=False)
    backend_name: Mapped[str | None] = mapped_column(Text)  # the STT_BACKENDS entry's name
    model: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    text_: Mapped[str | None] = mapped_column("text", Text)
    #: [{start, end, text, avg_logprob, no_speech_prob, compression_ratio}]
    segments: Mapped[Any | None] = mapped_column(JSONB)
    words: Mapped[Any | None] = mapped_column(JSONB)
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)
    compression_ratio: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[str | None] = mapped_column(TranscriptQualityEnum)
    quality_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    prompt_used: Mapped[str | None] = mapped_column(Text)
    vad_used: Mapped[bool | None] = mapped_column(Boolean)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    edited_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    edited_text: Mapped[str | None] = mapped_column(Text)

    call: Mapped[Call] = relationship(back_populates="transcripts")

    __table_args__ = (
        Index("ix_transcripts_call_id", "call_id"),
        Index("ix_transcripts_backend", "backend_name"),
    )


class SearchVocab(Base):
    """Every distinct lexeme of ``calls.search_tsv``, for fuzzy search.

    ``ts_stat`` output kept as a table with a trigram index on ``word``, so a misspelt query
    word can be widened to the spellings the archive actually holds without a trigram index
    over five million transcripts.  Maintained by the worker (app/services/fuzzy.py): new
    transcripts every five minutes, a full rebuild daily.  ``ndoc`` is how many primary calls
    contain the word — exact after a rebuild, an overcount by re-transcriptions in between.
    """

    __tablename__ = "search_vocab"

    word: Mapped[str] = mapped_column(Text, primary_key=True)
    ndoc: Mapped[int] = mapped_column(Integer, nullable=False)
    nentry: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "ix_search_vocab_word_trgm",
            "word",
            postgresql_using="gin",
            postgresql_ops={"word": "gin_trgm_ops"},
        ),
    )


class TranscriptionJob(Base):
    """The transcription queue.  Workers claim with ``SELECT … FOR UPDATE SKIP LOCKED``.

    ``priority`` is copied from the talkgroup at enqueue time (lower runs first);
    ``locked_by``/``locked_at`` let a crashed worker's claim be reaped.
    """

    __tablename__ = "transcription_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(JobStatusEnum, nullable=False, server_default=text("'queued'"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    #: pin the job to one backend; NULL lets the scheduler route it
    backend_name: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: why a ``failed`` row was parked, when the reason is not this recording.
    #: ``'backend_unavailable'`` means every STT backend was failing at the time, which is an
    #: outage rather than a verdict on this call, and the unpark pass (§5.2) requeues it once
    #: a backend is healthy again.  NULL is the ordinary case: the content failed.
    parked_reason: Mapped[str | None] = mapped_column(Text)
    #: how many times the unpark pass has requeued this job after an "outage".  That pass
    #: resets ``attempts``, so this is the only thing that stops a recording misread as an
    #: outage from going round for ever; capped at ``app.stt.worker.OUTAGE_REQUEUE_CAP``,
    #: reset to zero by an operator's requeue.
    outage_requeues: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    scheduled_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    call: Mapped[Call] = relationship()

    __table_args__ = (
        # The claim reads only the queued slice, so only the queued slice is indexed: a
        # couple of megabytes that stay in cache, not every job ever run (554fe0551d12).
        Index(
            "ix_transcription_jobs_queued",
            "priority",
            "scheduled_at",
            postgresql_where=text("status = 'queued'"),
        ),
        # The running/failed counts, the stale-job reaper and the parked-failure retry.
        Index(
            "ix_transcription_jobs_running_failed",
            "status",
            postgresql_where=text("status IN ('running', 'failed')"),
        ),
        # "Finished in the last hour / ten minutes": the admin STT queue block and the live
        # activity panel only ever read the right edge of this (1054b8161324).
        Index(
            "ix_transcription_jobs_finished",
            "finished_at",
            postgresql_where=text("status IN ('done', 'failed')"),
        ),
        Index("ix_transcription_jobs_call_id", "call_id"),
        Index(
            "ix_transcription_jobs_open_call",
            "call_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        # The unpark pass runs every two minutes and finds nothing almost every time; a
        # partial index keeps that a lookup rather than a scan of every job ever failed.
        Index(
            "ix_transcription_jobs_parked",
            "parked_reason",
            postgresql_where=text("parked_reason IS NOT NULL"),
        ),
    )


class AudioJob(Base):
    """The audio-processing queue — one row per call awaiting an ``audio_process`` pass.

    A table of its own rather than a ``kind`` column on ``transcription_jobs``: everything
    that reads that queue — the admin jobs page, the flow gauge that decides whether an
    upload is admitted, the drain estimate, the stale-job reaper's minute counts — means
    *transcription* by it, and widening the rows under those readers would have made every
    one of them wrong until it was audited.  The claim statement, the backoff and the
    lifecycle are the same shape, deliberately, so the two queues read alike.

    ``priority`` is the talkgroup's, exactly as the transcription job's is, so an archive
    migration processes behind live traffic.  Only one open job per call, enforced by a
    partial unique index, so a re-enqueue while a pass is running is a no-op rather than a
    second worker writing the same file.
    """

    __tablename__ = "audio_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(JobStatusEnum, nullable=False, server_default=text("'queued'"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    call: Mapped[Call] = relationship()

    __table_args__ = (
        # Partial, as on ``transcription_jobs`` and for the same reasons (554fe0551d12).
        Index(
            "ix_audio_jobs_queued",
            "priority",
            "scheduled_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_audio_jobs_running_failed",
            "status",
            postgresql_where=text("status IN ('running', 'failed')"),
        ),
        Index(
            "ix_audio_jobs_finished",
            "finished_at",
            postgresql_where=text("status IN ('done', 'failed')"),
        ),
        Index("ix_audio_jobs_call_id", "call_id"),
        Index(
            "ix_audio_jobs_open_call",
            "call_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


# --------------------------------------------------------------------------------------
# §3.3 integration
# --------------------------------------------------------------------------------------


class ExternalLink(Base):
    """A call (or conversation) tied to a record in another system.

    A CAD integration stores its incident number here.  The confidence/method vocabulary
    (``strong`` / ``probable`` / ``weak``) is the one CAD-to-radio matching conventionally
    uses, so both sides read alike.
    """

    __tablename__ = "external_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"))
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'cad', 'manual', …
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # the outside record's id
    link_confidence: Mapped[str | None] = mapped_column(LinkConfidenceEnum)
    link_method: Mapped[str | None] = mapped_column(LinkMethodEnum)
    link_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", "call_id", "conversation_id", name="uq_external_links_target"
        ),
        Index("ix_external_links_source_external", "source", "external_id"),
        Index("ix_external_links_call_id", "call_id"),
        Index("ix_external_links_conversation_id", "conversation_id"),
    )


# --------------------------------------------------------------------------------------
# §3.4 operations
# --------------------------------------------------------------------------------------


class IngestRun(Base):
    """One scan or watch batch, for the Admin → Ingest panel."""

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'scan'"))  # scan|watch|upload
    root: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    files_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    files_new: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    files_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    files_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    files_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    errors: Mapped[Any | None] = mapped_column(JSONB)  # [{path, error}], capped

    __table_args__ = (Index("ix_ingest_runs_started_at", text("started_at DESC")),)


class IngestFile(Base):
    """Per-file ledger keyed by path, so a failure is visible and retryable.

    A path stays here even when its call row is deleted, which is what makes a rescan
    cheap: the scanner skips anything already ``ingested``.
    """

    __tablename__ = "ingest_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # relative to AUDIO_ROOT
    status: Mapped[str] = mapped_column(IngestFileStatusEnum, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_runs.id", ondelete="SET NULL"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    first_seen_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    last_attempt_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        Index("ix_ingest_files_status", "status"),
        Index("ix_ingest_files_call_id", "call_id"),
    )


class RecorderGap(Base):
    """A stretch with no calls on a system that normally has traffic — recorder down."""

    __tablename__ = "recorder_gaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TZ, nullable=False)  # last call before the gap
    ended_at: Mapped[datetime] = mapped_column(TZ, nullable=False)  # first call after it
    duration_s: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    #: the call that ended the quiet stretch, so the UI can jump to it
    resumed_call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("system_id", "started_at", name="uq_recorder_gaps_system_started"),
        Index("ix_recorder_gaps_system_started", "system_id", text("started_at DESC")),
        Index("ix_recorder_gaps_resumed_call_id", "resumed_call_id"),
    )


# --------------------------------------------------------------------------------------
# §3.5 site channel audit — what the air says, what RadioReference documents, what the
# recorder is configured to hunt.  Three sources of the same fact, kept apart on purpose:
# the difference between them *is* the report (docs/CHANNEL_AUDIT.md).
# --------------------------------------------------------------------------------------


class RecorderObservation(Base):
    """One ``POST /api/recorder/observations`` document, stored verbatim.

    The normalized rows below are derived from it, so a mapping bug is repairable by
    replaying the payloads rather than by asking the recorder to see the site again.
    """

    __tablename__ = "recorder_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_id: Mapped[int | None] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    recorder: Mapped[str] = mapped_column(Text, nullable=False)  # 'trunkd'
    recorder_version: Mapped[str | None] = mapped_column(Text)
    #: Which recorder *instance* posted it — trunkd's ``instance_id``.  ``recorder`` above
    #: is the software ("trunkd"); this is the machine, and two boxes watching one site are
    #: only told apart by it.  NULL from a binary that does not send one.
    instance: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    #: how long the recorder watched to produce this document, seconds
    window_s: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_recorder_observations_site_observed", "site_id", text("observed_at DESC")),
        Index("ix_recorder_observations_observed_at", text("observed_at DESC")),
    )


class SiteFrequency(Base):
    """One frequency at one site, as **one** source describes it.

    Unique on ``(site_id, frequency_hz, source)`` rather than on the frequency alone: a
    channel RadioReference calls an alternate control channel and the recorder hears
    carrying voice is two rows, and the audit is the comparison between them.  Counts
    accumulate across observation windows; ``first_seen_at`` is never moved backwards by
    a later window arriving out of order.
    """

    __tablename__ = "site_frequencies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    frequency_hz: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(SiteFrequencyRoleEnum, nullable=False)
    source: Mapped[str] = mapped_column(SiteFrequencySourceEnum, nullable=False)
    #: the recorder's channel label for it ('1-638'), or RadioReference's LCN
    channel_ref: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    #: observation windows this frequency appeared in, and grants counted across them
    observation_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    grant_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: when the ``channel_audit`` alert for this row was raised; NULL = never (once only)
    alerted_at: Mapped[datetime | None] = mapped_column(TZ)
    #: The conventional-channel twin of ``talkgroups.export_*``: a call with no talkgroup is
    #: judged by the frequency it was heard on.  Default true, same as talkgroups.
    export_broadcastify: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    export_openmhz: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    meta: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("site_id", "frequency_hz", "source", name="uq_site_frequencies_site_freq_source"),
        Index("ix_site_frequencies_site_source", "site_id", "source"),
    )


class SiteMessageKind(Base):
    """One ``mfid:opcode`` the control channel of one site has ever transmitted.

    The message census itself (``GET /api/sites/{id}/message-mix``) is aggregated out of
    the stored observation payloads and is deliberately not a table: it is a histogram, and
    a histogram is cheap to recompute.  This *is* a table because it answers a different
    question — "have we ever seen this before?" — which cannot be answered by a rolling
    window at all.  One row per (site, mfid, opcode), written at observation ingest, is
    what makes a message kind appearing for the first time an event rather than a number
    that quietly changes in a report nobody reloads (docs/UNKNOWN-DATA-PLAN.md phase 5).

    ``exemplar`` is the hex body the recorder could not decode, kept for the kinds it
    reports one for; when the bytes of a kind change, the old ones move to
    ``exemplar_previous`` and ``exemplar_changed_at`` is stamped, because a beacon whose
    payload changed is the interesting half of "unknown data".  ``known``/``label`` are the
    operator's: a kind that has been named stops being news.

    ``alerted_at`` is the rate limiter — one alert per (site, kind) per 24 hours — and is
    the same device ``site_frequencies.alerted_at`` is for the channel audit.
    """

    __tablename__ = "site_message_kinds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    #: manufacturer id (0 = standard) and the opcode within its table
    mfid: Mapped[int] = mapped_column(Integer, nullable=False)
    opcode: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    #: messages of this kind counted across every window, ever
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: the newest hex body, for a kind the recorder could not decode
    exemplar: Mapped[str | None] = mapped_column(Text)
    exemplar_changed_at: Mapped[datetime | None] = mapped_column(TZ)
    exemplar_previous: Mapped[str | None] = mapped_column(Text)
    #: true once the operator (or the identifier registry) has named it
    known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    label: Mapped[str | None] = mapped_column(Text)
    #: when this kind last raised an alert; NULL = never
    alerted_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("site_id", "mfid", "opcode", name="uq_site_message_kinds_identity"),
        Index("ix_site_message_kinds_site_first_seen", "site_id", text("first_seen_at DESC")),
    )


class AdjacentSite(Base):
    """A neighbor one site's control channel announces (Adjacent Status Broadcast).

    Keyed by the identity the *air* gives it — system id, RFSS and site number — because
    that is all the announcement carries; ``known_site_id`` is resolved at read time by
    matching those against ``sites``.
    """

    __tablename__ = "adjacent_sites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    #: the neighbor's P25 system id, RFSS and site number as announced
    system_id_p25: Mapped[int] = mapped_column(Integer, nullable=False)
    rfss: Mapped[int] = mapped_column(Integer, nullable=False)
    site_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lra: Mapped[int | None] = mapped_column(Integer)
    control_frequency_hz: Mapped[int | None] = mapped_column(BigInteger)
    channel_ref: Mapped[str | None] = mapped_column(Text)
    #: conventional / failure / valid / rfss_connected, verbatim from the broadcast
    flags: Mapped[Any | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    observation_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    alerted_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "site_id", "system_id_p25", "rfss", "site_number", name="uq_adjacent_sites_identity"
        ),
        Index("ix_adjacent_sites_site", "site_id"),
    )


class RrSite(Base):
    """Every site RadioReference lists for a system — a reference cache, not a heard site.

    The sync fetches all of a system's sites in one ``getTrsSites`` call and only *one* of
    them maps to a Keyup ``Site`` (the tower the recorder actually hears).  The other sites
    are the neighbors those towers announce, so they are kept here to give the channel
    audit something to resolve an ``AdjacentSite`` against — a name, a county — without a
    second RR call.

    Two partial unique indexes hold the identity: ``(system_id, rr_site_id)`` when RR
    carries a site id, ``(system_id, rfss, site_number)`` when it does not.
    """

    __tablename__ = "rr_sites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_id: Mapped[int] = mapped_column(
        ForeignKey("systems.id", ondelete="CASCADE"), nullable=False
    )
    #: RR's own ``siteId`` for the tower; the P25 ``systemId``/``sysid`` if RR carries one
    rr_site_id: Mapped[int | None] = mapped_column(Integer)
    rr_system_id: Mapped[int | None] = mapped_column(Integer)
    site_number: Mapped[int | None] = mapped_column(Integer)
    rfss: Mapped[int | None] = mapped_column(Integer)
    #: the P25 NAC, parsed from RR's hex the way :func:`app.rr.sync.match_site` reads it
    nac: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(Text)  # siteDescr
    county: Mapped[str | None] = mapped_column(Text)
    control_frequency_hz: Mapped[int | None] = mapped_column(BigInteger)
    #: the whole ``siteFreqs`` list, verbatim (MHz, use, lcn, …)
    frequencies: Mapped[Any | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "uq_rr_sites_system_rr_site",
            "system_id",
            "rr_site_id",
            unique=True,
            postgresql_where=text("rr_site_id IS NOT NULL"),
        ),
        Index(
            "uq_rr_sites_system_rfss_number",
            "system_id",
            "rfss",
            "site_number",
            unique=True,
            postgresql_where=text("rr_site_id IS NULL"),
        ),
        Index("ix_rr_sites_system_id", "system_id"),
    )


class RetentionPolicy(Base):
    """How long audio, metadata and transcripts are kept, globally or per system/talkgroup.

    The most specific matching row wins (talkgroup → system → global).  ``keep_days`` 0
    means "keep for ever"; ``archive_path`` moves the MP3 instead of deleting it.
    """

    __tablename__ = "retention_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(RetentionScopeEnum, nullable=False)
    scope_id: Mapped[int | None] = mapped_column(Integer)  # systems.id / talkgroups.id; NULL for global
    keep_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    keep_audio_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    keep_transcripts: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    archive_path: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("scope", "scope_id", name="uq_retention_policies_scope"),)


class Setting(Base):
    """Runtime settings editable in the Admin UI; the environment provides the defaults."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)


class User(Base):
    """A person who can log in.  Reads are open unless ``AUTH_REQUIRE_READ`` is set."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: 'read' | 'write' | 'admin' — the same vocabulary as api_tokens.scopes
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{read}'::text[]")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(TZ)


class UserSession(Base):
    """A logged-in browser session; the cookie carries only the opaque ``token``."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_user_sessions_user_id", "user_id"),)


class ApiToken(Base):
    """A bearer token for machine access.

    ``scopes`` is any of ``read``, ``write``, ``admin``, ``stream``, ``upload``; the
    recorder push endpoint (§1.6) presents its token in the ``key`` form field and needs
    ``upload``.  Only the hash is stored.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: first characters of the token, so the UI can tell two tokens apart
    token_prefix: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{read}'::text[]")
    )
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


class Recorder(Base):
    """A trunkd/recorder instance keyup manages the tuners of (docs/ARCHITECTURE.md §4.8).

    A recorder appears here the first time it polls for its desired state or posts a tuner
    status, exactly as a site appears from the first observation for it: keyup never has to
    be told a recorder exists.  ``version`` is the desired-state generation the recorder
    polls — it is bumped whenever any of the recorder's tuners has its ``desired`` written,
    so a poller can cheaply tell "nothing has changed since last time".  ``recorder_version``
    is the software version string the instance reports about itself.

    ``station`` and ``counters`` are the recorder's *self-description*: what the operator
    said the site is, and the instance-level totals it has run up.  Both are written only
    when the status push actually carries them — an omitted key means "the recorder did not
    say", never "clear it", so an older binary that knows nothing of either cannot wipe what
    a newer one reported.

    *Recorder-level* desired state — what keyup wants of the instance rather than of one of
    its tuners — is three settings on one generation: ``desired_spool_only`` (spool the calls
    instead of pushing them), ``desired_event_log_archive`` (ship the daily raw event log
    here) and ``desired_link`` (the whole recorder-to-recorder link block).  All three are
    three-valued, NULL meaning "keyup has no opinion, keep the recorder's own TOML value".
    ``desired_recorder_version`` is their shared generation, bumped by a write to any of
    them, and the recorder echoes it back as ``applied_recorder_version`` alongside the
    fuller ``recorder_applied`` block, whatever it ``recorder_rejected`` and the
    ``recorder_capabilities`` that say which of them cost it a restart.  ``uplink``,
    ``link`` and ``event_log`` are what it reports about the same three in return.
    """

    __tablename__ = "recorders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: the recorder's own stable identity — trunkd's ``instance_id`` (§1.6)
    instance_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    #: the API token the recorder authenticated with when we first saw it, for the record
    token_id: Mapped[int | None] = mapped_column(ForeignKey("api_tokens.id", ondelete="SET NULL"))
    #: the desired-state generation the recorder polls; bumped on every ``desired`` write
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    #: the recorder software version the instance reports ('trunkd 0.9.1')
    recorder_version: Mapped[str | None] = mapped_column(Text)
    #: the operator's description of the *site* — name, location, lat/lon, elevation,
    #: contact, notes.  Facts only the person who built the station knows, so keyup stores
    #: them verbatim and never overrides them.  JSONB because the field set is the
    #: recorder's to grow, exactly as ``tuners.reported`` is.
    station: Mapped[Any | None] = mapped_column(JSONB)
    #: instance-level lifetime counters (calls started/ended, uploaded/spooled/failed,
    #: spool bytes, free disk) — a handful of totals, not a metrics dump
    counters: Mapped[Any | None] = mapped_column(JSONB)
    #: when ``station``/``counters`` were last *reported*.  Distinct from ``last_seen_at``,
    #: which every contact bumps: an older recorder binary that sends neither leaves this
    #: at the last time one did, so the operator can tell stale self-description from a
    #: recorder that has simply gone quiet.
    reported_at: Mapped[datetime | None] = mapped_column(TZ)
    #: Which recorder to prefer when several hear the same site — **lower is preferred**,
    #: the way a routing metric reads.  It is the arbiter's tie-break and nothing else
    #: consumes it: two warm spares that could both cover a fallen site are ordered by this
    #: rather than by whichever row happens to be found first, so promotion is predictable
    #: and an operator can say "use the tower before the garage" by typing a number.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    #: ``hot`` | ``warm`` | ``cold`` — see :data:`RECORDER_STANDBY`.
    standby: Mapped[str] = mapped_column(
        RecorderStandbyEnum, nullable=False, server_default=text("'hot'")
    )
    #: When the **arbiter** enabled this recorder's tuners, and NULL whenever it did not.
    #: It is what separates "keyup turned this spare on" from "an operator turned it on",
    #: and it lives in the database rather than in the arbiter's memory for one reason: an
    #: API restart must not orphan a promotion.  A warm recorder an operator enabled by hand
    #: has NULL here for ever and is never auto-demoted.
    promoted_at: Mapped[datetime | None] = mapped_column(TZ)
    #: Whether keyup wants this recorder to *hold* its uploads in its local spool rather
    #: than push them.  Three-valued on purpose: ``True`` hold, ``False`` upload, and NULL
    #: — the default — "keyup has no opinion", which leaves the recorder on whatever its
    #: own config or panel says.  A recorder keyup has never been told anything about must
    #: not have its own setting overridden by the mere absence of one here.
    desired_spool_only: Mapped[bool | None] = mapped_column(Boolean)
    #: The generation of the *recorder-level* desired state, bumped on every operator write
    #: that actually changes :attr:`desired_spool_only`, :attr:`desired_event_log_archive` or
    #: :attr:`desired_link`.  One counter for all three: the recorder applies the block as a
    #: whole and echoes one number back.  Deliberately separate from :attr:`version`, which any
    #: tuner write moves: the recorder echoes this one back as
    #: :attr:`applied_recorder_version`, and a counter that also moved on unrelated tuner
    #: edits could never settle.
    desired_recorder_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: The ``desired_recorder_version`` the recorder says it has applied — NULL until one
    #: reports it, which is every recorder running a binary that predates the hold.
    applied_recorder_version: Mapped[int | None] = mapped_column(Integer)
    #: Whether keyup wants this recorder to ship its daily raw event log here.  Three-valued
    #: exactly as :attr:`desired_spool_only` is, and for the same reason: NULL is "keyup has
    #: no opinion", which leaves the recorder on its own TOML setting.
    desired_event_log_archive: Mapped[bool | None] = mapped_column(Boolean)
    #: The **whole** recorder-to-recorder link block keyup wants, or NULL for "not managed
    #: here".  One JSONB object rather than a column per setting because the block is applied
    #: and refused as a unit — a peer list that disagreed with the key written beside it
    #: would be the worst kind of half-applied state — and because its field set is trunkd's
    #: to grow.  Shape: ``{enabled, bind, peers[], key, hello_interval_s,
    #: accept_remote_grants, publish_grants, defer_to_claims}``.
    #:
    #: The ``key`` is stored **in clear**.  It is a shared HMAC secret that keyup must be
    #: able to hand back to the recorder in its desired state, not a password to verify, so
    #: there is nothing a hash would buy; what keeps it from leaking is the API, which
    #: renders ``key_set``/``key_fingerprint`` everywhere except one admin-only read.
    desired_link: Mapped[Any | None] = mapped_column(JSONB)
    #: The recorder's own report of its upload path: whether it is holding, where the hold
    #: came from (``config``/``keyup``/``panel``), how much is waiting in the spool and the
    #: URL it uploads to.  Stored verbatim as JSONB for the same reason ``tuners.reported``
    #: is: the field set is the recorder's to grow.
    uplink: Mapped[Any | None] = mapped_column(JSONB)
    #: The whole ``recorder_applied`` block the recorder echoed back — what it applied, not
    #: merely which version.  The instance-level counterpart of a tuner's ``reported``, kept
    #: as its own column because a recorder has no single reported blob to hide it in.  Its
    #: ``link`` half carries ``key_set``/``key_fingerprint`` rather than the key itself.
    recorder_applied: Mapped[Any | None] = mapped_column(JSONB)
    #: Recorder-level ``{field: reason}`` — the instance-level twin of a tuner's ``rejected``.
    #: A refusal does not hold the recorder pending (it acknowledges the version either way),
    #: so this map is the only trace one leaves.
    recorder_rejected: Mapped[Any | None] = mapped_column(JSONB)
    #: ``capabilities.recorder`` as reported — chiefly ``restart_on``, the recorder-level
    #: fields it cannot change without restarting itself.
    recorder_capabilities: Mapped[Any | None] = mapped_column(JSONB)
    #: What the recorder says about the **peers it is linked to** — one entry per peer with
    #: its instance, whether it is alive, when it was last heard and what it currently
    #: claims.  Opaque JSONB stored verbatim: keyup displays it and decides nothing from it,
    #: so the shape is trunkd's to grow without a migration here.
    link: Mapped[Any | None] = mapped_column(JSONB)
    #: What the recorder says about its own **event-log archive**: how many days are still
    #: waiting to be uploaded and which day it last shipped.  Opaque for the same reason
    #: ``link`` is; the authoritative record of what actually arrived is
    #: :class:`EventLogArchive`, not this.
    event_log: Mapped[Any | None] = mapped_column(JSONB)
    #: What the recorder says about its own **live audio monitor** — the little panel it
    #: serves itself, where an operator can listen to what it is hearing right now.  Shape:
    #: ``{enabled, url, stream_url, token_required, streams, max_streams}`` — and only those,
    #: unlike ``link`` and ``event_log``: this is the one report whose stored content becomes
    #: an ``href`` on somebody's status page, so the validator drops what it does not know
    #: rather than keeping it.  NULL for a recorder that serves no panel — which is every
    #: recorder on a binary that predates one — and NULL again as soon as one stops reporting
    #: it, because a link must not outlive the panel it points at.
    monitor: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    tuners: Mapped[list[Tuner]] = relationship(
        back_populates="recorder", cascade="all, delete-orphan", passive_deletes=True
    )
    # No explicit index on instance_id: the unique constraint already creates one.


class Tuner(Base):
    """One tuner (SDR channel) of a recorder — what it reports and what keyup wants of it.

    ``reported`` is the last status the recorder pushed (serial, backend, center/span, role,
    lock state, control-channel health, pool usage…); ``desired`` is what an operator wants
    it to do (role, focus site, center frequency, enabled, spectrum).  Both are JSONB so the
    field set can evolve with the recorder without a migration.  A tuner drifts when the
    ``applied_version`` it reports lags the ``desired_version`` keyup has recorded.
    """

    __tablename__ = "tuners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recorder_id: Mapped[int] = mapped_column(
        ForeignKey("recorders.id", ondelete="CASCADE"), nullable=False
    )
    #: the recorder's own key for the tuner ('rtl-0', 'airspy-1', a device serial)
    tuner_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: last status the recorder pushed for this tuner, stored verbatim
    reported: Mapped[Any | None] = mapped_column(JSONB)
    reported_at: Mapped[datetime | None] = mapped_column(TZ)
    #: what an operator wants the tuner to do, stored verbatim
    desired: Mapped[Any | None] = mapped_column(JSONB)
    #: bumped every time ``desired`` is written; the recorder echoes it back as applied
    desired_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    recorder: Mapped[Recorder] = relationship(back_populates="tuners")

    __table_args__ = (
        UniqueConstraint("recorder_id", "tuner_key", name="uq_tuners_recorder_key"),
        Index("ix_tuners_recorder_id", "recorder_id"),
    )


class EventLogArchive(Base):
    """One day of one recorder's raw event log, as it was received (docs/RECORDERS.md).

    trunkd writes every decoded control-channel message to a local ``<date>.jsonl.gz`` — the
    firehose ``radio_events`` deliberately never stores (docs/NON-VOICE-DATA.md §5, "store
    the histogram, never the firehose") — and rolls one file per UTC day.  This table is the
    *receipt* for a file that has been shipped here: which instance, which day, how many
    bytes, the sha256 the recorder claimed and keyup verified, and where the file landed
    under ``EVENT_LOG_ROOT``.  The bytes live on disk; only the receipt is a row, because a
    day of a busy site is tens of megabytes and Postgres is not a file server.

    Unique on ``(instance, date)``: a day is archived once.  A re-upload of the *same*
    digest is answered 409 — the recorder already succeeded and is retrying a lost response
    — while a re-upload of a *different* digest replaces both file and row, which is what a
    recorder that rebuilt a partial day is asking for.

    Nothing here is a foreign key onto ``recorders``.  Forgetting a retired recorder must
    not delete its archive, and an instance that has never polled may still have logs
    shipped for it.
    """

    __tablename__ = "event_log_archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: the recorder's ``instance_id``, as it named itself in the upload
    instance: Mapped[str] = mapped_column(Text, nullable=False)
    #: the UTC day the log covers
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: hex sha256 of the stored file, verified on arrival rather than taken on trust
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    #: where the file is, relative to nothing — an absolute path under ``EVENT_LOG_ROOT``
    path: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("instance", "date", name="uq_event_log_archives_instance_date"),
        Index("ix_event_log_archives_instance_date", "instance", text("date DESC")),
    )


class SavedSearch(Base):
    """A named ``GET /calls`` filter set, per user and optionally shared."""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[Any] = mapped_column(JSONB, nullable=False)  # the filter object verbatim
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_saved_searches_user_name"),)


class Feed(Base):
    """A syndication feed: one saved call filter, published as RSS and Atom.

    The **token is the credential**.  A feed URL is handed to a reader that has no session,
    no cookie and no way to sign in, so the secret has to live in the URL — which is why it
    is 43 characters of :mod:`secrets` rather than the row id, why the row can be disabled
    without being deleted, and why rotating it is one button.  ``query`` is a
    ``GET /calls`` filter object, the same shape ``saved_searches.query`` holds and the
    same one :func:`app.api.filters.filters_from_mapping` accepts.
    """

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The owner.  A feed made by a signed-in user carries ``user_id``; one made by an API
    #: token carries ``token_id`` — a token principal has no user, and before this column
    #: every token-made feed shared the owner NULL, so any token could list, edit or rotate
    #: any other token's feeds.  A feed with neither (its token was deleted, or it predates
    #: the column) is an admin's to manage.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: the secret in the URL — unique and indexed, because every fetch looks a feed up by it
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    query: Mapped[Any] = mapped_column(JSONB, nullable=False)  # the filter object verbatim
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: attach the recording as an enclosure, so a podcast app can play the feed
    include_audio: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    max_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(TZ)
    fetch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_feeds_user_name"),)


class Favorite(Base):
    """A user's pinned talkgroup, with a mute flag for the live feed."""

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    talkgroup_id: Mapped[int] = mapped_column(ForeignKey("talkgroups.id", ondelete="CASCADE"), nullable=False)
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "talkgroup_id", name="uq_favorites_user_talkgroup"),)


class AlertRule(Base):
    """Fire a webhook/discord/ntfy/email when a matching call arrives.

    ``filters`` is a ``GET /calls`` filter object; ``pattern`` is the keyword or regex for
    the two text kinds.  ``cooldown_seconds`` stops a busy talkgroup from spamming.
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(AlertKindEnum, nullable=False)
    pattern: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[Any | None] = mapped_column(JSONB)
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    #: per-channel configuration ({"discord": {"webhook_url": …}, "ntfy": {"topic": …}})
    channel_config: Mapped[Any | None] = mapped_column(JSONB)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("300"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_fired_at: Mapped[datetime | None] = mapped_column(TZ)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Notification(Base):
    """Delivery log for alert rules — what was sent where, and whether it worked."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="SET NULL"))
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_notifications_created_at", text("created_at DESC")),
        Index("ix_notifications_call_id", "call_id"),
    )


class ExportJob(Base):
    """An asynchronous audio+metadata bundle (``POST /api/exports``, §4.1).

    The zip is built by a background thread and written under ``EXPORT_DIR``; this row is
    the only thing the API needs in order to answer ``GET /exports/{id}``.  ``id`` is a
    random hex string rather than a serial so a download link cannot be walked.
    """

    __tablename__ = "export_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    filters: Mapped[Any | None] = mapped_column(JSONB)  # the filter set, verbatim
    status: Mapped[str] = mapped_column(JobStatusEnum, nullable=False, server_default=text("'queued'"))
    file_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (Index("ix_export_jobs_created_at", text("created_at DESC")),)


class ActivityEvent(Base):
    """The operational activity log of the ingest/transcription pipeline (Admin → Activity).

    One row per notable pipeline moment — a call accepted, a duplicate rejected, a
    transcription queued/done/failed, an error — written in the same transaction as the
    work it records and announced on the live bus (``activity`` event, id only) so the
    Admin page and ``keyup activity --follow`` can tail it.  The table is bounded: rows
    older than ``ACTIVITY_RETENTION_DAYS`` are pruned by the nightly retention pass.

    ``source`` is the pipeline stage (ingest, upload, transcribe, export, rr, import,
    system, arbiter);
    ``level`` is info | warn | error; ``kind`` is a dotted event name (``call.accepted``,
    ``transcribe.done``); ``message`` is a human one-liner; ``meta`` carries whatever
    structured detail is worth keeping (engine, priority, duration, error string).
    """

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    source: Mapped[str] = mapped_column(Text, nullable=False)  # see app.activity.SOURCES
    level: Mapped[str] = mapped_column(Text, nullable=False)  # info|warn|error
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # dotted, e.g. call.accepted
    message: Mapped[str] = mapped_column(Text, nullable=False)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("calls.id", ondelete="SET NULL"))
    meta: Mapped[Any | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_activity_events_at", text("at DESC")),
        # Not a plain index on ``level``: 99.1% of this table is ``info``, so for the
        # only value most rows have it selects the whole table, and it is the two rare
        # levels an operator ever filters to.  Carrying ``id DESC`` means it also serves
        # the ``ORDER BY id DESC LIMIT n`` every activity query ends with, so the filter
        # and the sort come out of one scan (migration b8d4e60a17c2).
        Index(
            "ix_activity_events_level_at",
            "level",
            text("id DESC"),
            postgresql_where=text("level <> 'info'"),
        ),
        # The referencing side of ``call_id``'s FK.  Without it every deleted call is a
        # sequential scan of this table — millions of rows — to SET NULL its log lines,
        # which made one 500-call retention batch hours of work (migration c4e9a1d07b35).
        Index("ix_activity_events_call_id", "call_id", postgresql_where=text("call_id IS NOT NULL")),
    )


class DiskSample(Base):
    """One free-space reading of one filesystem, every five minutes (:mod:`app.disk_watch`).

    Kept for two days: the last 24 hours are the growth rate behind "days to full", and the
    newest row's ``level`` is the alert's memory — a threshold fires when the new level is
    worse than the last one recorded and re-arms when a reading is back to ``ok``.
    """

    __tablename__ = "disk_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: the path that was measured (the first watched path on that filesystem)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: ok | warn | critical
    level: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ok'"))

    __table_args__ = (Index("ix_disk_samples_path_at", "path", "at"),)


class HealthCondition(Base):
    """One thing the worker's health check watches, and whether it is wrong right now.

    :mod:`app.health` keeps one row per subject — ``ingest_lag``, ``stt_backlog``,
    ``backup_stale``, and ``recorder_silent:<instance_id>`` per hot recorder — and this row
    is the alert's memory: an alert is raised when ``active`` goes false → true and cleared
    when it goes back, so a worker restart neither repeats nor forgets an alert.
    ``good_since`` is when the subject was first seen healthy again while still active; it
    clears only after :data:`app.health.RECOVERY` of that.
    """

    __tablename__ = "health_conditions"

    #: the subject: the check's name, ``:<instance_id>`` for a per-recorder check
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    #: recorder_silent | ingest_lag | stt_backlog | backup_stale
    check: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    raised_at: Mapped[datetime | None] = mapped_column(TZ)
    cleared_at: Mapped[datetime | None] = mapped_column(TZ)
    good_since: Mapped[datetime | None] = mapped_column(TZ)
    checked_at: Mapped[datetime | None] = mapped_column(TZ)
    #: the last measurement, in the check's own unit (minutes or hours), and what it said
    value: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(Text)


class CallExport(Base):
    """One call's upload ledger row for one outbound service (docs/EXPORTERS.md).

    The exporter worker is a queue over this table, and the ``UNIQUE (call_id, service)``
    is the whole point of it: a restart, a second worker or a re-discovery pass can all
    try to claim the same call, and the unique index means the second one loses the insert
    rather than sending the recording to Broadcastify twice.  A row is written **before**
    anything is uploaded and only moves ``pending → uploaded`` once the service has
    acknowledged it.

    ``attempts``/``last_error`` are the retry bookkeeping (exponential backoff on
    ``next_attempt_at``, parked as ``failed`` at ``EXPORT_MAX_ATTEMPTS``); ``remote_id`` is
    whatever identifier the service handed back, kept so an upload can be traced from this
    side to theirs.  Rows are never written for a call outside the live band or older than
    the service's enable watermark — see :mod:`app.exporters.gate`.
    """

    __tablename__ = "call_exports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    #: one of :data:`CALL_EXPORT_SERVICES`; Text rather than an enum so adding a third
    #: service is a code change and not a migration
    service: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        CallExportStatusEnum, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: why a ``skipped`` row was skipped — 'encrypted', 'no audio', 'no talkgroup'
    skip_reason: Mapped[str | None] = mapped_column(Text)
    #: the service's own identifier for the upload, when it returns one
    remote_id: Mapped[str | None] = mapped_column(Text)
    #: the integer that leads Broadcastify Calls' one-line stage-1 answer — ``0`` success,
    #: ``1`` skipped or rejected, ``100`` no API key.  ``None`` when the attempt never got
    #: an answer to parse (a timeout, a 5xx, a transcode failure) and for services whose
    #: protocol has no such code.
    response_code: Mapped[int | None] = mapped_column(Integer)
    #: what the service actually said, as one line, redacted of any API key and capped at
    #: 500 characters.  A Broadcastify success is stored as ``0 <last path segment>``: the
    #: rest of the upload URL is a presigning signature that expires in minutes and is not
    #: ledger material.  Kept so "how many of these were duplicates" is a query rather than
    #: a guess at ``skip_reason`` prose (docs/EXPORTERS.md).
    response: Mapped[str | None] = mapped_column(Text)
    #: **Who sent it**: ``keyup`` when this host's exporter worker uploaded the call, or
    #: the recorder's ``instance_id`` when a trunkd recorder registered and PUT it itself
    #: and reported the outcome on the call push (``broadcastify_upload_at=recorder``).
    #: Nullable: every row written before recorder-direct upload existed has no answer,
    #: and inventing ``keyup`` for them would make history look like it was observed.
    uploaded_by: Mapped[str | None] = mapped_column(Text)
    #: backoff: the worker will not claim this row before this instant
    next_attempt_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    uploaded_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("call_id", "service", name="uq_call_exports_call_service"),
        #: the claim query: pending rows for one service that are due, oldest first
        Index("ix_call_exports_claim", "service", "status", "next_attempt_at"),
        Index("ix_call_exports_status", "status"),
        #: the Exports tab's 24-hour tallies and its recent-rows table (554fe0551d12)
        Index("ix_call_exports_updated_at", "updated_at"),
    )


class BcfyClaim(Base):
    """One "I am uploading this call to Broadcastify Calls" claim (docs/EXPORTERS.md).

    Exists because more than one uploader can hear the same transmission.  Two trunkd
    recorders covering one site both record every call, and keyup itself uploads whatever
    no recorder answered for; Broadcastify keys a duplicate on ``(system, tg, ts)`` with a
    **whole-second** ``ts``, so two uploaders that rounded the start of the same call a
    second apart do not look like a duplicate to them — they look like two calls.  The
    registry is the tie-break taken *before* anything is sent: the first claimant for a
    ``(tgid, ts)`` inside a ±2 s tolerance wins and uploads, everybody else skips.

    ``instance`` is the recorder's ``instance_id``, or the literal ``keyup`` for this
    host's own exporter worker.  ``outcome``/``response`` are filled in afterwards with
    what Broadcastify said, and ``call_id`` is stamped once the call it was about has a
    row here — a claim is made at the *edge*, before the audio has been pushed, so the two
    are only linked later.  Rows are pruned after two days by the export vacuum: the
    tolerance window is seconds wide and nothing reads a claim older than the call it
    belonged to.
    """

    __tablename__ = "bcfy_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: the talkgroup the call was on, as the claimant reported it
    tgid: Mapped[int] = mapped_column(Integer, nullable=False)
    #: the call's start as **whole seconds** since the epoch — Broadcastify's own ``ts``
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_s: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    #: the recorder's ``instance_id``, or ``keyup`` for this host's exporter worker
    instance: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    #: what became of the upload the claim reserved — ``uploaded``, ``duplicate``,
    #: ``failed``, ``late``, ``skipped`` — or NULL while it is still in flight
    outcome: Mapped[str | None] = mapped_column(Text)
    #: the service's own line, capped and redacted like ``call_exports.response``
    response: Mapped[str | None] = mapped_column(Text)
    #: the call this claim turned out to be about, once one is stored here
    call_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        #: the lookup: every claim for one talkgroup near one second
        Index("ix_bcfy_claims_tgid_ts", "tgid", "ts"),
    )


class WebhookDelivery(Base):
    """One outbound webhook waiting to be, or having been, delivered (:mod:`app.webhooks`).

    A queue, written in the same transaction as the work that caused it — an ingest that
    rolls back leaves no delivery behind — and drained by a worker job.  The alternative,
    POSTing from the ingest path itself, would put somebody else's HTTP server in the
    critical path of storing a recording, which is the one thing this archive must not
    make conditional on anything.

    ``payload`` is the JSON body **as it will be sent**, built at enqueue time from rows
    the caller already had in hand.  That is deliberate: it costs the hot path no query,
    and a retry three hours later re-sends what was true when the event happened rather
    than what is true now — which is what an event log means by an event.

    ``attempts``/``next_at`` are the same exponential backoff the export ledger and the
    transcription worker use, so the curve an operator already knows is the curve they get
    here.  There is no unique constraint: unlike ``call_exports``, two identical events
    genuinely can happen (a call re-transcribed after a correction), and the receiver
    dedupes on the delivery id in the payload if it cares.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: one of :data:`WEBHOOK_EVENTS`; Text rather than an enum so adding a fifth event is
    #: a code change and not a migration
    event: Mapped[str] = mapped_column(Text, nullable=False)
    #: the call or link the event is about, for the admin list and for a targeted retry.
    #: No foreign key: a delivery outlives the row it describes on purpose — "this call
    #: was stored" stays true after retention deletes the call.
    subject_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        WebhookDeliveryStatusEnum, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: the HTTP status the receiver last answered with, when it answered at all
    response_status: Mapped[int | None] = mapped_column(Integer)
    #: with every URL scrubbed (:func:`app.alerts.scrub_secrets`): a delivery URL can carry
    #: a token in its path, and this column is read back by anybody who can see the archive
    last_error: Mapped[str | None] = mapped_column(Text)
    #: backoff: the worker will not claim this row before this instant
    next_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        #: the claim query: due pending rows, oldest first
        Index("ix_webhook_deliveries_claim", "status", "next_at"),
        Index("ix_webhook_deliveries_created", text("created_at DESC")),
    )


class SttBackendStat(Base):
    """What each speech-to-text backend is doing, published by the worker (§5.2).

    The counters the admin "backends" page shows live in memory inside the ``keyup worker``
    process — one :class:`~app.stt.router.BackendStats` per backend, mutated under the
    router's lock.  The API process has no router and no counters, so it cannot answer
    "what is the GPU server doing right now" from anything it holds itself; this table is how
    the answer crosses the process boundary.  The worker upserts every row on a short
    cadence and immediately when a backend's health flips, and the API reads it back.  A
    row whose ``updated_at`` has gone quiet means the *worker* stopped reporting, which is
    itself the thing an operator most wants to see, so rows are never deleted for age.

    ``admin_enabled`` is the other direction: the operator's runtime override of the
    configured ``enabled`` flag, written by the API and read back by the worker's router
    at selection time.  NULL means "follow the configuration"; the effective state is what
    ``enabled`` reports.  The worker's upsert must never assign it — see the upsert lock
    note in :mod:`app.ingest.service` for why an upsert here sets only non-key columns.
    """

    __tablename__ = "stt_backend_stats"

    #: the backend name from ``STT_BACKENDS`` — the identity both processes agree on
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    #: the effective state the worker routed on: configuration AND the operator's override
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: the configured half alone, so the UI can say "disabled by config" or "by operator"
    config_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: the operator's override: NULL follows the configuration, true/false wins over it
    admin_enabled: Mapped[bool | None] = mapped_column(Boolean)
    admin_changed_at: Mapped[datetime | None] = mapped_column(TZ)
    admin_changed_by: Mapped[str | None] = mapped_column(Text)
    #: NULL when the backend has never been probed in this worker's lifetime
    healthy: Mapped[bool | None] = mapped_column(Boolean)
    health_detail: Mapped[str | None] = mapped_column(Text)
    #: why the router took the backend out of rotation for good (missing model)
    disqualified: Mapped[str | None] = mapped_column(Text)
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    successes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(TZ)
    last_success_at: Mapped[datetime | None] = mapped_column(TZ)
    audio_s: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    processing_s: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    mean_rtf: Mapped[float | None] = mapped_column(Float)
    mean_processing_s: Mapped[float | None] = mapped_column(Float)
    #: rolling 60-second windows, counted in the worker's memory and republished each pass
    recent_completed_1m: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    recent_failed_1m: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: which worker process published this row, and when that process started
    worker_instance: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    #: the ``stt_config_version`` the publishing worker's router was built from.  This is
    #: how the admin page can say "the worker has picked your edit up" rather than leaving
    #: the operator to guess from a counter that has not moved: the page compares this
    #: against the version the API reads from the ``settings`` table.
    config_version: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class SttBackendConfig(Base):
    """A speech-to-text backend as the *operator* defined it (§5.2, §4.7).

    Until this table existed, ``STT_BACKENDS`` — a JSON blob in the environment — was the
    only way to say a backend exists, which meant adding a transcription server was an edit
    to a ``.env`` on the host plus a restart of two processes.  The columns here are exactly
    :class:`app.config.SttBackend`'s fields, so the table is that JSON with a primary key
    and an audit trail rather than a second, competing notion of a backend.

    **Precedence** (docs/ARCHITECTURE.md §5.2): the database wins.  ``STT_BACKENDS`` seeds
    this table once, in the migration that creates it, and only when the table is empty; it
    is never consulted again while a row exists.  A deployment that has never written a row
    still runs off the environment, so an install that never opens the admin page behaves
    exactly as it did before.

    ``api_key`` is **write-only**: the API accepts it, stores it, and never echoes it —
    responses carry a ``has_api_key`` boolean instead.  Nothing about a key an operator
    already pasted in is improved by being readable from a browser.

    ``deleted_at`` is why a delete is not always a delete.  ``transcripts.backend_name``
    names the backend that produced every transcript ever stored, and the archive's whole
    point is that its history stays answerable, so removing a backend that appears there
    marks the row deleted (and disabled) instead of dropping it.  The
    :class:`SttBackendStat` row is never touched either way — the counters are the record
    of work that really happened.
    """

    __tablename__ = "stt_backends"

    #: the name the worker, the stats row and ``transcripts.backend_name`` all agree on
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'openai_compatible'")
    )
    base_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: write-only; see the class docstring.  Blank means "no key", not "unchanged".
    api_key: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    device: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    compute_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'int8'"))
    cpu_threads: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    cache_dir: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    use_prompt: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    use_hotwords: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    timeout: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("300"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: ``env`` — seeded from ``STT_BACKENDS``; ``db`` — created or edited from the admin page.
    #: Purely informational: it tells the operator where a row they did not write came from.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'db'"))
    notes: Mapped[str | None] = mapped_column(Text)
    #: set instead of dropping the row when transcripts still name this backend
    deleted_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "kind in ('openai_compatible', 'ovms', 'faster_whisper_local', 'openvino_genai')",
            name="ck_stt_backends_kind",
        ),
        #: the router's read: everything not deleted, cheapest first
        Index("ix_stt_backends_live", "deleted_at", "priority"),
    )


class IngestSource(Base):
    """Where recordings come from, as rows rather than as environment variables (§5.1, §4.7).

    One watched directory (``AUDIO_ROOT``) used to be the whole of ingest's configuration,
    read at process start and deliberately kept out of the runtime settings because
    changing it meant restarting the watcher.  This table removes that constraint rather
    than working around it: :class:`app.ingest.watcher.IngestWatcher` runs one watchdog
    watch per enabled ``watch_dir`` row and re-reads the table on a short cadence, so adding
    a second recorder's spool, or switching one off, takes effect within half a minute and
    without a restart.  ``AUDIO_ROOT`` seeds the first row in the migration and stays the
    archive's root — every ``calls.file_path`` is still relative to it — so a directory
    outside it is watched and ingested, just with an absolute path in the ledger.

    The other two kinds own no runtime behavior and are here because *identity* is the
    thing the operator lacks.  A ``push_token`` row names a recorder that POSTs to
    ``/api/call-upload``: the upload path is unchanged and still authenticates on the token
    alone, but the token now has a label, notes and a place to show what it last sent.  A
    ``migration`` row is a bulk import's status card, fed by the CLI tool through
    ``POST /admin/ingest/migration-progress``.

    ``last_error`` is written by the watcher, not by the save: a path that is readable when
    the operator saves it and unmounted an hour later is the case worth seeing.
    """

    __tablename__ = "ingest_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(IngestSourceKindEnum, nullable=False)
    #: what the operator calls it.  Unique because it is how a migration run names itself.
    label: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: ``watch_dir``: the directory to watch and scan.  NULL for the other kinds.
    path: Mapped[str | None] = mapped_column(Text)
    #: ``watch_dir``: descend into subdirectories (sdrtrunk writes day/talkgroup trees)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: ``watch_dir``: per-source override of ``TR_PREFER_AUDIO`` — trunk-recorder writes one
    #: call as both a WAV and an M4A and two spools may not have been configured alike
    tr_prefer_audio: Mapped[str | None] = mapped_column(Text)
    #: ``watch_dir``: per-source override of ``TR_SIDECAR_WAIT_SECONDS``
    tr_sidecar_wait_seconds: Mapped[float | None] = mapped_column(Float)
    #: ``push_token``: the upload token this recorder presents in the ``key`` form field
    token_id: Mapped[int | None] = mapped_column(ForeignKey("api_tokens.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    #: ``migration``: whatever the import tool last posted (files done/total, its own state)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_activity_at: Mapped[datetime | None] = mapped_column(TZ)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "kind <> 'watch_dir' or path is not null",
            name="ck_ingest_sources_watch_dir_path",
        ),
        CheckConstraint(
            "tr_prefer_audio is null or tr_prefer_audio in ('m4a', 'wav')",
            name="ck_ingest_sources_tr_prefer_audio",
        ),
        #: the watcher's read on every reload tick
        Index("ix_ingest_sources_kind_enabled", "kind", "enabled"),
        UniqueConstraint("kind", "path", name="uq_ingest_sources_kind_path"),
    )


# --------------------------------------------------------------------------------------
# Broadcastify feed-owner statistics (docs/BCFY_STATS.md)
# --------------------------------------------------------------------------------------


class BcfyFeedSample(Base):
    """One answer from the feed-owner API's ``a=feed`` method, kept verbatim.

    ``raw`` is the whole response as it arrived — the entire point of this table.  The
    owner API's field names are not published anywhere, so the columns beside it
    (``listeners``, ``online``) are what a small, easily-extended mapping table
    (:data:`app.bcfy_stats.client.FIELD_ALIASES`) could *find* in that response, and
    ``extracted`` is everything the mapping recognized.  When a field turns out to be
    called something else, the mapping is widened and the history can be re-derived from
    ``raw`` rather than re-collected.

    Samples are kept :data:`app.bcfy_stats.store.SAMPLE_KEEP_DAYS` days; the hourly rollup
    beside them is what months-long charts read, so the retention of this table is a
    storage decision and not a loss of history.
    """

    __tablename__ = "bcfy_feed_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: Broadcastify's feed id, as text — it is their identifier, not ours, and nothing
    #: here does arithmetic on it
    feed_id: Mapped[str] = mapped_column(Text, nullable=False)
    polled_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    listeners: Mapped[int | None] = mapped_column(Integer)
    online: Mapped[bool | None] = mapped_column(Boolean)
    raw: Mapped[Any | None] = mapped_column(JSONB)
    extracted: Mapped[Any | None] = mapped_column(JSONB)

    __table_args__ = (
        #: every read — the chart window, the rollup pass, the retention delete
        Index("ix_bcfy_feed_samples_feed_polled", "feed_id", "polled_at"),
    )


class BcfyFeedHourly(Base):
    """One feed-hour of listener statistics, maintained by the poller as it writes samples.

    A month of minute samples is 43 000 rows to average on every chart draw; the same
    month is 720 rows here.  The row is upserted on every sample rather than rebuilt by a
    nightly job, so the current hour is always current and there is no window in which the
    chart and the samples disagree.
    """

    __tablename__ = "bcfy_feed_hourly"

    feed_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: the hour, truncated in UTC
    hour: Mapped[datetime] = mapped_column(TZ, primary_key=True)
    listeners_avg: Mapped[float | None] = mapped_column(Float)
    listeners_max: Mapped[int | None] = mapped_column(Integer)
    #: how many samples with a listener count went into the average
    samples: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: where the row came from.  ``NULL`` — the default and the overwhelming majority — is
    #: "this deployment's own polls", the truth.  ``'import'`` is one row of the feed
    #: dashboard's hourly CSV export, which is Broadcastify's own authoritative figure for
    #: an hour nobody here polled.  ``'page'`` is one bar of the public feed page's 24-hour
    #: sparkline (docs/BCFY_STATS.md), which is a number read off a chart.  Precedence runs
    #: polled > import > page, and ``samples`` is 0 on both backfilled kinds, which is what
    #: the merge rule tests and what keeps a backfilled value out of every average.
    source: Mapped[str | None] = mapped_column(Text)
    #: how many times Broadcastify checked the feed in this hour, and how many of those
    #: checks found it up — the hourly export's uptime pair, and ``NULL`` on every row this
    #: deployment polled for itself, which has no such figure to record.
    online_checks: Mapped[int | None] = mapped_column(Integer)
    online_reports: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_bcfy_feed_hourly_hour", "hour"),)


class BcfyFeedDaily(Base):
    """One feed-day of uptime and peak listeners, from the dashboard's daily export.

    **Nothing but the import writes this table**, and it is deliberately not derived from
    :class:`BcfyFeedHourly`.  The daily export reaches about 180 days back where the hourly
    one reaches seven, so for the week they overlap there would be two numbers for the same
    day that do not have to agree — Broadcastify's own daily figure, and a roll-up of their
    hourly peaks.  Keeping the two sources in two tables means neither is ever silently
    replaced by the other, and the summary shows them side by side at read time.

    ``uptime_pct`` is the only place in this schema that carries uptime at all: a poll knows
    whether the feed was up *at that instant* and the rollup does not keep even that, so a
    day's percentage cannot be reconstructed from anything else here.
    """

    __tablename__ = "bcfy_feed_daily"

    feed_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: the UTC day, as the export dates it (docs/BCFY_STATS.md on why UTC)
    day: Mapped[date_type] = mapped_column(Date, primary_key=True)
    uptime_pct: Mapped[float | None] = mapped_column(Float)
    listeners_max: Mapped[int | None] = mapped_column(Integer)
    #: ``'import'``; the column exists so a future second source is a value rather than a
    #: schema change, and so a reader never has to assume where a row came from.
    source: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_bcfy_feed_daily_day", "day"),)


class BcfyArchive(Base):
    """One archived block of feed audio, as the ``a=archives&day=`` listing describes it.

    ``UNIQUE (feed_id, start_at)`` is what makes the sync idempotent: a block re-listed on
    the next pass updates its row (the download URL is time-limited and does change)
    rather than being stored twice.
    """

    __tablename__ = "bcfy_archives"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: the local archive day the block was listed under, kept as Broadcastify reported it
    day: Mapped[date_type] = mapped_column(Date, nullable=False)
    start_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(TZ)
    url: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[Any | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("feed_id", "start_at", name="uq_bcfy_archives_feed_start"),
        Index("ix_bcfy_archives_feed_day", "feed_id", "day"),
    )
