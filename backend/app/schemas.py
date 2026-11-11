"""Response and request models for the HTTP API (docs/ARCHITECTURE.md §4).

Conventions this file keeps to, because third parties are meant to consume the generated
OpenAPI document at ``/api/openapi.json``:

* **Every instant is returned twice.**  ``*_at`` fields are ISO-8601 UTC; a call also
  carries ``started_local``, the naive local (``TZ``) wall-clock time, because CAD systems
  and everything else local reasons in local time (§3).
* **Lists are enveloped** in :class:`Page`, and the same total is repeated in the
  ``X-Total-Count`` response header so a client that only wants the count can ``HEAD``.
* Durations are seconds (``float``); sizes are bytes; ``*_ms`` is milliseconds.
* Anything the schema calls an enum is a ``Literal`` here, so the generated client gets
  the vocabulary rather than a bare string.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

# The one import from the model layer.  Every other vocabulary here is a hand-kept
# ``Literal`` mirror, because a Literal is what puts the values into the OpenAPI document;
# RadioReference's service tags cannot be one — they are matched case-insensitively and
# normalized, which a Literal cannot do — so mirroring the 31 names would buy nothing but a
# second list to drift.
from app.models import (
    AGENCY_CODE_PATTERN,
    AGENCY_KINDS,
    RR_SERVICE_TAGS,
    canonical_service_tag,
)

# --------------------------------------------------------------------------------------
# vocabularies (mirrors app.models / docs/ARCHITECTURE.md §3)
# --------------------------------------------------------------------------------------

CallTypeName = Literal["group", "patch", "individual"]
#: ``calls.kind`` — who the transmission was addressed to, which is a different question
#: from ``call_type`` (how the channel was granted).  See :class:`CallOut`.
CallKindName = Literal["group", "unit"]
TranscriptStatusName = Literal["pending", "queued", "running", "done", "skipped", "failed", "empty"]
QualityName = Literal["good", "suspect", "noise"]
IngestSourceName = Literal["file", "upload", "trunk_recorder", "trunkd"]
TimeSourceName = Literal["file_write", "upload", "event_log", "sidecar"]
RadioKindName = Literal["console", "portable", "mobile", "unknown"]
UnitKindName = Literal["vehicle", "station", "team", "other"]
AssignmentKindName = Literal["unit", "person"]
JobStatusName = Literal["queued", "running", "done", "failed", "cancelled"]
IngestFileStatusName = Literal["ingested", "duplicate", "failed", "ignored"]
SttEngineName = Literal["openai_compatible", "faster_whisper_local", "openvino_genai", "ovms", "manual"]
LinkConfidenceName = Literal["strong", "probable", "weak"]
LinkMethodName = Literal["time", "time+agency", "unit", "text", "time+text", "manual"]
AlertKindName = Literal[
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
]
SiteFrequencyRoleName = Literal["primary_control", "alternate_control", "voice", "unknown"]
SiteFrequencySourceName = Literal["observed", "radioreference", "configured"]
RadioEventKindName = Literal[
    "unit_registration",
    "unit_deregistration",
    "unit_registration_command",
    "group_affiliation",
    "group_affiliation_query",
    "location_registration",
    "sndcp_grant",
    "sndcp_page",
    "private_call",
    "ack",
    "deny",
    "queued",
    "other",
]
RadioAffiliationSourceName = Literal["call", "affiliation"]
ScopeName = Literal["read", "write", "admin", "stream", "upload"]
RetentionScopeName = Literal["global", "system", "talkgroup"]


class ORMModel(BaseModel):
    """Base for anything read straight out of a SQLAlchemy row or ``Row._mapping``."""

    model_config = ConfigDict(from_attributes=True)


class Page[T](ORMModel):
    """A page of results plus the total number of rows matching the current filter.

    ``total`` is also sent as the ``X-Total-Count`` header — when there is one.  It is
    **null** while cursor paging: an infinite scroll shows no total, and counting for it
    meant a full scan of the filtered set on every page.  It is *capped* otherwise, at
    50 000 rows, with ``total_capped`` saying so — past that the honest answer is "at
    least this many".

    ``next_cursor`` is present only for keyset ("infinite scroll") paging: pass it back as
    ``cursor=`` to continue from exactly where the page ended, which — unlike ``page=``+1 —
    cannot skip or repeat a row while new calls are arriving.
    """

    items: list[T]
    total: int | None = Field(
        default=None,
        description="Rows matching the filter, ignoring pagination; null while cursor paging",
    )
    total_capped: bool = Field(
        default=False,
        description="True when `total` stopped at the counting cap and the real number is larger",
    )
    page: int = Field(description="1-based page number")
    page_size: int = Field(description="Rows per page (max 500)")
    next_cursor: str | None = Field(
        default=None, description="Opaque keyset cursor for the next page, or null at the end"
    )


class ErrorOut(BaseModel):
    detail: str = Field(description="Human readable error message")


class OkOut(BaseModel):
    ok: bool = True
    detail: str | None = None


# --------------------------------------------------------------------------------------
# directory (§3.1)
# --------------------------------------------------------------------------------------


class SystemOut(ORMModel):
    """A trunked radio system, e.g. ``Example System``."""

    id: int
    name: str
    wacn: int | None = Field(default=None, description="P25 Wide Area Communications Network id")
    system_id: int | None = Field(default=None, description="P25 system id inside the WACN")
    protocol: str | None = Field(default=None, examples=["P25 Phase 1"])
    trunked: bool | None = Field(
        default=None,
        description=(
            "Whether this is a **trunked** system, as an operator set it. `null` means "
            "nobody has said, and `is_trunked` below answers from `protocol` instead. "
            "Editable through `PATCH /systems/{id}`."
        ),
    )
    is_trunked: bool = Field(
        default=False,
        description=(
            "The answer everything acts on: `trunked` when it is set, otherwise a guess "
            "from `protocol` (P25/APCO, DMR, Motorola, SmartNet/SmartZone, EDACS, LTR, "
            "NXDN, TETRA — unless the protocol also says conventional or analog). "
            "**Broadcastify Calls publishes trunked systems only**: a call from a system "
            "this is false for is refused with the ledger reason `not_trunked`, whatever "
            "is selected. The Feed and OpenMHz are not gated on it."
        ),
    )
    description: str | None = None
    rr_system_id: int | None = Field(
        default=None, description="RadioReference system id (`sid`), when mapped"
    )
    rr_synced_at: datetime | None = Field(
        default=None, description="Last successful RadioReference sync of this system"
    )
    call_count: int = 0
    site_count: int = 0
    deletable: bool = Field(
        default=False,
        description=(
            "True when `DELETE /systems/{id}` would succeed — nothing hangs off this system. "
            "Every foreign key onto `systems` cascades, so this is the only thing standing "
            "between a delete and a million recordings."
        ),
    )
    blocking: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "What still belongs to this system, as `{table: rows}`; empty exactly when "
            "`deletable` is true. It is what a 409 from the delete would say."
        ),
    )


class SystemPatch(BaseModel):
    """Body of ``PATCH /systems/{id}``; only the keys present are written.

    As narrow as :class:`SitePatch` and for the same reason: ``wacn``, ``system_id``,
    ``nac`` and ``protocol`` are what the air announced about itself and were filled from
    what the recorder heard.  The name and the description are the two an operator owns.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    description: str | None = None
    trunked: bool | None = Field(
        default=None,
        description=(
            "Whether this is a trunked system. The third field an operator owns, and the "
            "exception to the rule above, because it is a *judgement* about what the "
            "protocol string means rather than something the control channel announced: "
            "Broadcastify Calls takes trunked systems only, and a decoder's free-text "
            "protocol name is not always enough to tell. Send `null` to go back to the "
            "guess derived from `protocol`."
        ),
    )


class SystemMergeResult(BaseModel):
    """What ``POST /systems/{id}/merge-into/{target_id}`` moved before deleting the source."""

    source: dict[str, Any] = Field(description="The id and name of the system that was deleted")
    target: SystemOut
    reparented: dict[str, int] = Field(
        default_factory=dict,
        description="Children with no twin under the target, moved across as they were",
    )
    merged: dict[str, int] = Field(
        default_factory=dict,
        description="Children folded into a twin the target already had, as `{table: rows}`",
    )
    rows_repointed: dict[str, int] = Field(
        default_factory=dict,
        description="References rewritten onto the surviving rows, as `{table.column: rows}`",
    )
    duplicates_dropped: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Rows a unique key would not let across, dropped because their survivor says the "
            "same thing. Never a call: `calls` has no unique key over its foreign keys."
        ),
    )
    deleted: dict[str, int] = Field(
        default_factory=dict, description="Rows the fold removed, as `{table: rows}`"
    )


class SiteOut(ORMModel):
    """A tower site of a system, e.g. ``Central``.

    ``name`` is unique only *within* a system: two systems can both have an ``Eastside``, and
    real archives have exactly that.  ``system_name`` is carried on every row so a client never
    has to render a site name on its own — "Example System / Eastside" is the only label that
    identifies a site, and the SPA shows the system beside or under the name everywhere.
    """

    id: int
    system_id: int
    system_name: str | None = Field(
        default=None,
        description="Name of the owning system. A site name alone is ambiguous across systems.",
    )
    name: str
    site_number: int | None = None
    description: str | None = None
    notes: str | None = Field(default=None, description="The operator's own note about this site")
    call_count: int = 0
    nac: int | None = Field(default=None, description="Network access code the site keys (§3.5)")
    rfss: int | None = Field(default=None, description="P25 RF subsystem the site belongs to")
    lra: int | None = Field(default=None, description="Location registration area")
    rr_site_id: int | None = Field(default=None, description="RadioReference site id, when mapped")
    rr_synced_at: datetime | None = Field(
        default=None, description="Last successful RadioReference sync of this site"
    )
    services_available: list[str] | None = Field(
        default=None,
        description=(
            "The services this site's SYS_SVC_BCAST says it is offering, from the newest "
            "observation window that carried the bitmap. **Null** means nobody has decoded "
            "the message here — which is not the same as a site offering nothing. "
            "`GET /sites/{id}/profile` has the rest of the bitmap."
        ),
    )
    deletable: bool = Field(
        default=False,
        description=(
            "True when `DELETE /sites/{id}` would succeed — nothing references this site. "
            "The UI enables its delete button from this rather than guessing."
        ),
    )
    blocking: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "What still references this site, as `{table: rows}`; empty exactly when "
            "`deletable` is true. It is what a 409 from the delete would say."
        ),
    )


class SitePatch(BaseModel):
    """Body of ``PATCH /sites/{id}``; only the keys present are written.

    Deliberately narrow.  ``site_number``/``nac``/``rfss``/``lra`` are what the control
    channel announces about itself and the recorder fills them in; an operator editing them
    by hand would be editing the archive's record of what the air said.  ``name``, ``notes``
    and the RadioReference mapping are the three that are genuinely ours to choose.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    notes: str | None = None
    rr_site_id: int | None = Field(
        default=None, ge=1, description="RadioReference site id, or null to unmap"
    )


class SiteMergeResult(BaseModel):
    """What ``POST /sites/{id}/merge-into/{target_id}`` moved before deleting the source."""

    source: dict[str, Any] = Field(description="The id and name of the site that was deleted")
    target: SiteOut
    moved: dict[str, int] = Field(
        default_factory=dict, description="Rows repointed onto the target, as `{table: rows}`"
    )
    batches: int = Field(default=0, description="Committed batches the call repointing took")


class SiteProfileObserver(BaseModel):
    """One recorder instance's most recent window on this site.

    Two boxes watching one tower are told apart by nothing but ``instance`` (§3.5), and the
    profile below is merged across them — so this list is how a reader sees *who* saw what,
    and how stale the newest sighting from each box is.
    """

    instance: str | None = Field(
        default=None, description="The recorder's instance id; null from an older binary"
    )
    observed_at: datetime = Field(description="End of that instance's newest stored window")
    tsbk_rate: float | None = Field(default=None, description="Control blocks per second it measured")
    control_frequency_hz: int | None = None


class SiteProfileIdentity(BaseModel):
    """Who this site says it is: the numbers the control channel keys, and our mapping of them."""

    system_id: int = Field(description="Keyup's own system row id")
    system_name: str | None = Field(default=None, description="The system as Keyup files it")
    system_label: str | None = Field(default=None, description="The system as the recorder names it")
    wacn: int | None = None
    p25_system_id: int | None = Field(
        default=None, description="The P25 system id, from the fully-qualified radio form"
    )
    nac: int | None = None
    nac_hex: str | None = Field(
        default=None,
        description="The same NAC as the hex everyone quotes it in",
        examples=["0x034"],
    )
    rfss: int | None = None
    site_number: int | None = None
    lra: int | None = Field(default=None, description="Location registration area")
    rr_site_id: int | None = None
    rr_synced_at: datetime | None = None


class SiteProfileChannel(BaseModel):
    """A control channel this site uses or announces as a backup."""

    frequency_hz: int | None = Field(
        default=None,
        description=(
            "Null when the site announced a channel number whose band plan has not been "
            "heard yet — the channel is real, the frequency is not computable."
        ),
    )
    channel: str | None = None
    tsbk_rate: float | None = None
    service_class: list[str] = Field(default_factory=list)


class SiteProfileControl(BaseModel):
    """The control channel the recorder is on, the backups the site names, and what we hunt."""

    frequency_hz: int | None = None
    channel: str | None = None
    tsbk_rate: float | None = None
    secondary_control: list[SiteProfileChannel] = Field(default_factory=list)
    configured_control: list[int] = Field(
        default_factory=list, description="Control frequencies the recorder is configured to hunt"
    )


class SiteProfileServices(BaseModel):
    """The SYS_SVC_BCAST bitmap: what this site offers, and what it merely supports.

    ``available``/``supported`` are **null**, not empty, when no stored window carries the
    message: "the site offers nothing" and "nobody has decoded it" are different answers and
    the UI says so.
    """

    available: list[str] | None = Field(default=None, description="Services the site says it is offering now")
    supported: list[str] | None = Field(default=None, description="Services the site is capable of")
    available_raw: str | None = Field(default=None, description="The 24-bit available bitmap as hex")
    supported_raw: str | None = None
    request_priority: int | None = None
    supported_not_available: list[str] = Field(
        default_factory=list,
        description="Supported but not currently offered — the interesting half of the bitmap",
    )


class SiteProfileBand(BaseModel):
    """One IDEN band-plan entry, which is what turns a channel number into a frequency."""

    id: int | None = None
    base_hz: int | None = None
    spacing_hz: int | None = None
    offset_hz: int | None = None
    bandwidth_hz: int | None = None
    tdma: bool | None = None


class SiteProfileTraffic(BaseModel):
    """A traffic channel the site granted in the newest window, busiest first."""

    frequency_hz: int | None = None
    channel: str | None = None
    grants: int = 0
    calls: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class SiteProfileAdjacent(BaseModel):
    """A neighbor this site announces, resolved to a site Keyup knows when it can be."""

    system_id: int | None = Field(default=None, description="The neighbor's P25 system id, as announced")
    rfss: int | None = None
    site_number: int | None = None
    lra: int | None = None
    control_frequency_hz: int | None = None
    channel: str | None = None
    flags: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(
        default=None,
        description=(
            "What this neighbour is called: the Keyup site with the same (system id, RFSS, "
            "site number), or — when Keyup has never heard it — the RadioReference site the "
            "channel audit resolves it to.  Null when neither source names it."
        ),
    )
    source: Literal["keyup", "radioreference"] | None = Field(
        default=None, description="Where `name` came from; null when nothing named it"
    )
    known: bool = Field(
        default=False,
        description=(
            "True exactly when a *Keyup* site matched — a tower this archive knows, not "
            "merely one RadioReference documents"
        ),
    )


class SiteProfileDataChannel(BaseModel):
    """SNDCP packet-data activity in the newest window."""

    grants: int = 0
    pages: int = 0
    pdus: int = 0
    channels: list[int] = Field(default_factory=list)


class SiteProfileMessageKinds(BaseModel):
    """How much of what this control channel says has ever been named.

    ``noise`` is the part of ``unknown`` that is not worth a reader's attention: a kind
    counted once or twice is a CRC false pass, not a message — the neighbour sites carry
    hundreds of them — so the interesting number is ``unknown - noise``, the kinds that
    recur and that nobody has named.
    """

    known: int = 0
    unknown: int = 0
    noise: int = Field(
        default=0,
        description="Unnamed kinds seen at most twice, ever — decode artefacts, counted inside `unknown`",
    )
    total_seen: int = Field(
        default=0, description="Distinct `mfid:opcode` kinds the site has ever transmitted"
    )


class SiteProfileOut(BaseModel):
    """Everything Keyup knows about one tower site, in one document.

    The site and system rows carry the identity; everything else is folded out of the
    *latest stored observation per recorder instance*, newest instance winning field by
    field — so a box that has been down for a day still contributes the band plan it saw,
    and the live box's control channel is the one shown.

    ``unknowns`` is the point of the document as much as the rest of it: a site is a pile of
    numbers some of which were never decoded, and a panel that only renders what is known
    quietly hides how much is not (docs/UNKNOWN-DATA-PLAN.md).
    """

    site_id: int
    site_name: str
    description: str | None = None
    notes: str | None = None
    call_count: int = 0
    observed_by: list[SiteProfileObserver] = Field(
        default_factory=list, description="Newest window per recorder instance, newest first"
    )
    identity: SiteProfileIdentity
    control: SiteProfileControl = Field(default_factory=SiteProfileControl)
    services: SiteProfileServices = Field(default_factory=SiteProfileServices)
    bands: list[SiteProfileBand] = Field(default_factory=list)
    traffic: list[SiteProfileTraffic] = Field(default_factory=list)
    adjacent_sites: list[SiteProfileAdjacent] = Field(default_factory=list)
    data_channel: SiteProfileDataChannel | None = None
    lsd_nonzero: int | None = Field(
        default=None, description="Low-speed-data octet pairs that were not all zero, newest window"
    )
    message_kinds: SiteProfileMessageKinds = Field(default_factory=SiteProfileMessageKinds)
    unknowns: list[str] = Field(
        default_factory=list, description="Short sentences naming what about this site is *not* known"
    )



def _service_tag_validator(value: str | None) -> str | None:
    """Accept only a RadioReference service tag, in RR's own spelling.

    ``category`` stays free text — it is this archive's own bucket and an operator may
    invent one — but ``service_tag`` claims to be *RR's* classification, so a value RR
    would not recognize is a 422 rather than a quiet lie in the column that the sync and
    the category mapping both read.  Matching is case-insensitive and the answer is the
    canonical spelling, so "fire dispatch" from a hand-written client is stored as
    ``Fire Dispatch`` and compares equal to what a sync would have written.
    """
    tag = canonical_service_tag(value)
    if value is not None and str(value).strip() and tag is None:
        raise ValueError(
            f"unknown RadioReference service tag {value!r}; expected one of "
            + ", ".join(RR_SERVICE_TAGS)
        )
    return tag


#: The per-talkgroup export checkbox, described once for both services.  The wording is the
#: selection rule itself (docs/EXPORTERS.md): the flag is inert until a mode consults it.
_EXPORT_FLAG_DOC = (
    "Whether this talkgroup's calls may be published to {service}. Consulted **only** when "
    "`{key}` is `allow` (publish only the ticked talkgroups) or `deny` (publish all but the "
    "cleared ones); in the shipped `all` mode it is ignored entirely. Default true, so it "
    "changes nothing until a mode is chosen."
)


class TalkgroupOut(ORMModel):
    """A channel.  ``tgid`` is the number on the air; ``id`` is this archive's key."""

    id: int
    system_id: int
    tgid: int = Field(description="Talkgroup id as transmitted", examples=[1101])
    alias: str | None = Field(default=None, examples=["EC EVENT 6"])
    group_name: str | None = Field(default=None, description="sdrtrunk alias group")
    description: str | None = None
    category: str | None = Field(default=None, examples=["law", "fire", "ems"])
    service_tag: str | None = Field(
        default=None,
        description=(
            "RadioReference's own service tag for this channel, stored verbatim "
            "(`Fire Dispatch`, `Law Tac`, `Public Works`, …). Finer than `category`, "
            "which is derived from it."
        ),
        examples=["Fire Dispatch"],
    )
    agency_code: str | None = Field(default=None, description="Agency code", examples=["EXAMPLEPD"])
    shared: bool = Field(
        default=False,
        description=(
            "A county-wide dispatch or interop channel no single department owns "
            "(EC LAW 1, FIRE DISPATCH, the EVENT channels). `agency_code` says whose "
            "channel this is; `shared` says it is everybody's, which is not something "
            "a blank `agency_code` can be read as. See `include_shared` on the filters."
        ),
    )
    color: str | None = None
    priority: int = 100
    record: bool = Field(
        default=True,
        description=(
            "Keep this channel's **audio**. The companion to `transcribe`: that one decides "
            "whether the words are kept, this one whether the recording is. Default "
            "**true**. False does not mean the channel is ignored — the call is still "
            "ingested, still transcribed when `transcribe` is on and still uploaded when "
            "an export service selects it; the audio file is deleted once that is done "
            "(`calls.audio_policy`). With `record` **and** `transcribe` both off and no "
            "exporter selecting the channel there is nothing left to do with the audio, "
            "and the recorder is told to stop uploading the talkgroup entirely."
        ),
    )
    transcribe: bool = True
    transcribe_min_seconds: float | None = Field(
        default=None, description="Per-talkgroup override of TRANSCRIBE_MIN_SECONDS"
    )
    retention_days: int | None = None
    export_broadcastify: bool = Field(
        default=True,
        description=_EXPORT_FLAG_DOC.format(
            service="the Broadcastify **Feed** (the live stream)",
            key="export_broadcastify_talkgroup_mode",
        ),
    )
    export_bcfy_calls: bool = Field(
        default=True,
        description=(
            "Whether this talkgroup's calls may be published to Broadcastify **Calls** — "
            "the per-call archive, which is a different product from the Feed above and "
            "chosen separately. Consulted only when `export_broadcastify_talkgroup_mode` "
            "is `allow` or `deny`. Forced **false**, and refused whatever it says, on a "
            "talkgroup whose system is not trunked: Calls publishes trunked systems only."
        ),
    )
    export_openmhz: bool = Field(
        default=True,
        description=_EXPORT_FLAG_DOC.format(service="OpenMHz", key="export_openmhz_talkgroup_mode"),
    )
    audio_profile_id: int | None = Field(
        default=None,
        description=(
            "Which audio profile this channel's recordings are filtered through "
            "(docs/AUDIO.md). Null — the shipped state — means the global audio settings, "
            "which is what the `Default` profile is a snapshot of."
        ),
    )
    is_hidden: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    call_count: int = 0
    notes: str | None = None


class TalkgroupDetail(TalkgroupOut):
    system_name: str | None = None
    agency_name: str | None = None
    audio_profile_name: str | None = None
    audio_seconds: float = 0.0
    radio_count: int = Field(default=0, description="Distinct radios heard on this talkgroup")
    last_call_id: int | None = None


#: A talkgroup priority is what a live transcription job inherits, and the backlog
#: re-runs the same number 500 higher (``ingest.flow.BACKLOG_OFFSET``) in a queue that
#: tops out at 999.  So the highest a talkgroup may claim is 499: anything above it
#: would push its own backlog jobs off the end of the scale.  Lower is worked first.
TALKGROUP_PRIORITY_MIN = 1
TALKGROUP_PRIORITY_MAX = 499


class TalkgroupIn(BaseModel):
    """Body of ``POST /talkgroups`` — everything but ``system_id``/``tgid`` is optional."""

    system_id: int | None = None
    tgid: int
    alias: str | None = None
    group_name: str | None = None
    description: str | None = None
    category: str | None = None
    service_tag: str | None = Field(default=None, examples=["Fire Dispatch"])
    agency_code: str | None = None
    shared: bool = False
    color: str | None = None
    priority: int = Field(
        default=100, ge=TALKGROUP_PRIORITY_MIN, le=TALKGROUP_PRIORITY_MAX
    )
    record: bool = True
    transcribe: bool = True
    transcribe_min_seconds: float | None = None
    retention_days: int | None = None
    export_broadcastify: bool = True
    export_bcfy_calls: bool = True
    export_openmhz: bool = True
    audio_profile_id: int | None = None
    is_hidden: bool = False
    notes: str | None = None

    _check_service_tag = field_validator("service_tag")(_service_tag_validator)


class TalkgroupPatch(BaseModel):
    """Body of ``PATCH /talkgroups/{id}``; only the keys present are written."""

    alias: str | None = None
    group_name: str | None = None
    description: str | None = None
    category: str | None = None
    service_tag: str | None = Field(default=None, examples=["Fire Dispatch"])
    agency_code: str | None = None
    shared: bool | None = None
    color: str | None = None
    priority: int | None = Field(
        default=None, ge=TALKGROUP_PRIORITY_MIN, le=TALKGROUP_PRIORITY_MAX
    )
    record: bool | None = None
    transcribe: bool | None = None
    transcribe_min_seconds: float | None = None
    retention_days: int | None = None
    export_broadcastify: bool | None = None
    export_bcfy_calls: bool | None = None
    export_openmhz: bool | None = None
    audio_profile_id: int | None = Field(
        default=None,
        description="Audio profile row id, or null to fall back to the global audio settings",
    )
    is_hidden: bool | None = None
    notes: str | None = None

    _check_service_tag = field_validator("service_tag")(_service_tag_validator)


class HourCount(BaseModel):
    hour: int = Field(ge=0, le=23)
    count: int


class LabelCount(BaseModel):
    key: str
    label: str | None = None
    count: int


class TopRadio(BaseModel):
    radio_id: int
    label: str
    count: int


class TalkgroupStatsOut(BaseModel):
    """``GET /talkgroups/{id}/stats`` — calls per day, busiest hours, top radios (§4.2)."""

    talkgroup_id: int
    call_count: int
    audio_seconds: float
    hourly: list[HourCount] = Field(description="Counts by hour of the local day, 0..23")
    spark: list[int] = Field(description="24 hourly counts ending now, oldest first")
    daily: list[LabelCount] = Field(default_factory=list, description="Calls per local day")
    top_radios: list[TopRadio] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# roster — units and personnel (§3.1, docs/ARCHITECTURE.md)
# --------------------------------------------------------------------------------------


class UnitRef(BaseModel):
    """Just enough of a unit to render "E1 — Engine 1" and link to it."""

    id: int
    callsign: str = Field(examples=["E1"])
    name: str | None = Field(default=None, examples=["Engine 1"])


class PersonRef(BaseModel):
    """Just enough of a person to render a byline and link to them."""

    id: int
    name: str = Field(examples=["J. Smith"])


class UnitOut(ORMModel):
    """A unit: the apparatus, station or crew a callsign names."""

    id: int
    callsign: str = Field(description="The callsign on the air", examples=["E1"])
    name: str | None = Field(default=None, examples=["Engine 1"])
    agency_code: str | None = Field(default=None, examples=["EXAMPLEPD"])
    kind: UnitKindName = "other"
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: radios currently assigned to this unit; filled by the endpoint, not by the row
    radio_count: int = 0


class AssignedRadioOut(BaseModel):
    """A radio a unit or a person currently holds, with the assignment that says so."""

    radio_pk: int = Field(description="Our radios.id")
    radio_id: int = Field(description="The unit number on the air", examples=[5200001])
    alias: str | None = None
    unit_id: str | None = Field(default=None, description="The radio's denormalized label")
    kind: RadioKindName = "unknown"
    call_count: int = 0
    last_seen_at: datetime | None = None
    assignment_id: int
    effective_from: datetime
    note: str | None = None


class UnitDetail(UnitOut):
    agency_name: str | None = None
    radios: list[AssignedRadioOut] = Field(default_factory=list, description="Currently assigned")
    #: traffic from every radio currently assigned — the closest thing to "what has this
    #: unit said", which is only as true as the assignments are
    call_count: int = 0
    last_call_at: datetime | None = None


class UnitIn(BaseModel):
    """Body of ``POST /units``."""

    callsign: str
    name: str | None = None
    agency_code: str | None = None
    kind: UnitKindName = "other"
    notes: str | None = None


class UnitPatch(BaseModel):
    """Body of ``PATCH /units/{id}``; only the keys present are written."""

    callsign: str | None = None
    name: str | None = None
    agency_code: str | None = None
    kind: UnitKindName | None = None
    notes: str | None = None


class PersonOut(ORMModel):
    """One person on the roster."""

    id: int
    name: str = Field(examples=["J. Smith"])
    rank: str | None = Field(default=None, examples=["Captain"])
    badge: str | None = None
    agency_code: str | None = None
    active: bool = True
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    radio_count: int = 0


class PersonDetail(PersonOut):
    agency_name: str | None = None
    radios: list[AssignedRadioOut] = Field(default_factory=list, description="Currently assigned")
    call_count: int = 0
    last_call_at: datetime | None = None


class PersonIn(BaseModel):
    """Body of ``POST /personnel``."""

    name: str
    rank: str | None = None
    badge: str | None = None
    agency_code: str | None = None
    active: bool = True
    notes: str | None = None


class PersonPatch(BaseModel):
    """Body of ``PATCH /personnel/{id}``; only the keys present are written."""

    name: str | None = None
    rank: str | None = None
    badge: str | None = None
    agency_code: str | None = None
    active: bool | None = None
    notes: str | None = None


class RadioAssignmentOut(BaseModel):
    """One row of a radio's assignment history.  ``current`` is ``effective_to IS NULL``."""

    id: int
    radio_pk: int
    kind: AssignmentKindName = Field(description="Which of the two this row assigns")
    unit: UnitRef | None = None
    person: PersonRef | None = None
    effective_from: datetime
    effective_to: datetime | None = None
    current: bool = False
    note: str | None = None


class RadioAssignmentIn(BaseModel):
    """Body of ``POST /radios/{id}/assignments`` — exactly one of the two ids."""

    unit_id: int | None = Field(default=None, description="units.id")
    person_id: int | None = Field(default=None, description="personnel.id")
    note: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self):  # noqa: ANN202
        if (self.unit_id is None) == (self.person_id is None):
            raise ValueError("send exactly one of unit_id or person_id")
        return self


class RadioPresenceAffiliation(BaseModel):
    """One talkgroup a radio is affiliated to, and what backs the claim."""

    talkgroup_id: int | None = Field(default=None, description="Our talkgroups.id")
    tgid: int | None = Field(default=None, description="The number on the air", examples=[1101])
    talkgroup_alias: str | None = None
    source: RadioAffiliationSourceName = Field(
        default="call", description="`call` = heard talking; `affiliation` = the control channel said so"
    )
    last_heard_at: datetime | None = None
    last_affiliated_at: datetime | None = None
    call_count: int = 0


class RadioPresence(BaseModel):
    """Where a radio *is*, as opposed to what it has said (docs/RADIO_EVENTS.md).

    ``last_seen_at`` moves for a call **or** a control-channel event; the other two move
    only for a registration or a de-registration, so a radio that has never been heard on
    a site the recorder watches has all three NULL.
    """

    last_seen_at: datetime | None = None
    last_registered_at: datetime | None = None
    last_deregistered_at: datetime | None = None
    affiliations: list[RadioPresenceAffiliation] = Field(default_factory=list)


class RadioEventOut(ORMModel):
    """One control-channel message about a radio."""

    id: int
    at: datetime
    kind: RadioEventKindName
    opcode: int = 0
    mfid: int = 0
    system_id: int | None = None
    site_id: int | None = None
    site_name: str | None = None
    radio_pk: int | None = Field(default=None, description="Our radios.id, when it resolved")
    radio_id: int | None = Field(default=None, description="The unit number on the air")
    radio_wacn: int = 0
    radio_system: int = 0
    target_radio_id: int | None = None
    talkgroup_pk: int | None = None
    talkgroup_tgid: int | None = None
    talkgroup_alias: str | None = None
    result: str | None = Field(default=None, examples=["accept"])
    frequency_hz: int | None = None
    instance: str | None = Field(
        default=None,
        description=(
            "Which recorder instance decoded it. Null on every row written before recorders "
            "sent one, and on any batch from a binary that still does not."
        ),
    )
    meta: dict[str, Any] | None = None


class RadioOut(ORMModel):
    """A subscriber unit.  ``radio_id`` is the number on the air; ``id`` is our key."""

    id: int
    system_id: int
    radio_id: int
    wacn: int | None = None
    home_system_id: int | None = None
    alias: str | None = None
    unit_id: str | None = Field(default=None, description="Apparatus id, e.g. E1 / A21", examples=["E1"])
    agency_code: str | None = None
    kind: RadioKindName = "unknown"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    call_count: int = 0
    notes: str | None = None
    #: The identity the roster resolves for this radio *right now* (§3.1).  Both are
    #: filled by the endpoint from ``radio_assignments``, not by the ORM row, so a radio
    #: read straight out of the database has them null.  ``unit_id`` above stays the
    #: denormalized label; these two are the structured answer behind it.
    current_unit: UnitRef | None = None
    current_person: PersonRef | None = None


class RadioDetail(RadioOut):
    system_name: str | None = None
    agency_name: str | None = None
    audio_seconds: float = 0.0
    talkgroup_count: int = 0
    last_call_id: int | None = None
    #: presence and the last 50 control-channel events, so the detail page needs one
    #: request for "what is this radio doing" (docs/RADIO_EVENTS.md)
    presence: RadioPresence = Field(default_factory=RadioPresence)
    events: list[RadioEventOut] = Field(default_factory=list, description="The 50 most recent")
    event_count: int = 0


class RadioPatch(BaseModel):
    alias: str | None = None
    unit_id: str | None = None
    agency_code: str | None = None
    kind: RadioKindName | None = None
    notes: str | None = None


class RadioAffiliationOut(BaseModel):
    """Which talkgroups a radio has been heard on — "who's active" and unit history."""

    radio_id: int
    radio_alias: str | None = None
    unit_id: str | None = None
    talkgroup_id: int
    talkgroup_alias: str | None = None
    tgid: int
    first_heard_at: datetime | None = None
    last_heard_at: datetime | None = None
    call_count: int
    source: RadioAffiliationSourceName = "call"
    last_affiliated_at: datetime | None = None


class RadioHistoryOut(BaseModel):
    radio_id: int
    call_count: int
    spark: list[int]
    hourly: list[HourCount]
    talkgroups: list[LabelCount] = Field(default_factory=list)


class PatchOut(ORMModel):
    """A temporary supergroup.  Patches are identified by their member set (§1.4)."""

    id: int
    system_id: int
    patch_id: int
    member_tgids: list[int]
    member_aliases: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    call_count: int = 0


class AgencyOut(ORMModel):
    """An agency from the directory, plus the channels and units mapped to it."""

    code: str
    name: str
    short_name: str | None = None
    kind: str | None = None
    talkgroup_ids: list[int] = Field(
        default_factory=list,
        description=(
            "**tgids** — the numbers on the air — of the talkgroups whose `agency_code` is "
            "this agency. Stable since 2026-09-03."
        ),
        examples=[[1404, 1425]],
    )
    talkgroups: list[int] = Field(
        default_factory=list,
        description=(
            "**Row primary keys** (`talkgroups.id`) of the same channels — the ids "
            "`/talkgroups/{talkgroup_id}` wants, not the on-air numbers. The same set as "
            "`talkgroup_ids`, each list sorted on its own, so do not read the two "
            "positionally as pairs. Stable since 2026-09-03."
        ),
        examples=[[8772, 8777]],
    )
    shared_talkgroup_ids: list[int] = Field(
        default_factory=list,
        description=(
            "tgids of every channel flagged `shared` — the county dispatch and interop "
            "channels no single department owns. The **same list on every agency**: it is "
            "the discoverable form of that set, not something this agency owns. Channels "
            "mapped to this agency *and* shared appear in `talkgroup_ids` as well."
        ),
        examples=[[1011, 1401]],
    )
    radio_count: int = 0
    call_count: int = 0
    source: str = Field(
        default="seed",
        description=(
            "`seed` for a row loaded from the deployment's agency list (`keyup seed-agencies`), "
            "`local` for one added in Keyup because that list has no code for it."
        ),
        examples=["seed"],
    )
    active: bool = Field(
        default=True,
        description=(
            "False takes a disbanded or merged agency out of the pickers without touching "
            "the talkgroups and radios that still point at it, so the history stays "
            "readable. Filters still match it."
        ),
    )


# --------------------------------------------------------------------------------------
# §4.7 admin — the agency table itself
# --------------------------------------------------------------------------------------


class AdminAgencyOut(ORMModel):
    """One row of the agency table as the Admin page edits it.

    Deliberately not :class:`AgencyOut`: that one answers "what is on this agency's air"
    and carries three id lists to do it, which an administrative table neither shows nor
    wants to page through. This one answers "may I delete this row", which is the counts.
    """

    code: str
    name: str
    short_name: str
    kind: str
    source: str = Field(examples=["seed", "local"])
    active: bool = True
    edited_at: datetime | None = Field(
        default=None,
        description=(
            "When this row was last changed here. On a `seed` row a non-null value is also "
            "what stops `keyup seed-agencies` putting the shared name back."
        ),
    )
    talkgroup_count: int = 0
    radio_count: int = 0
    unit_count: int = 0
    person_count: int = 0


class AgencyIn(BaseModel):
    """A new agency. `code` is the key everything else points at, so it cannot be changed."""

    code: str = Field(
        pattern=AGENCY_CODE_PATTERN,
        description=(
            "Upper case, digits, `_` and `-`, 2-16 characters. Pick something that reads "
            "as the agency — `WEST_SO`, `EXAMPLE_RR` — unless the agency has a real "
            "CAD/ORI code, in which case use that."
        ),
        examples=["WEST_SO"],
    )
    name: str = Field(min_length=1, max_length=120, examples=["WEST COUNTY SHERIFF"])
    short_name: str | None = Field(
        default=None,
        max_length=32,
        description="The abbreviation the tables show. Defaults to the code.",
        examples=["LCSO"],
    )
    kind: str = Field(description=f"One of {', '.join(AGENCY_KINDS)}", examples=["sheriff"])
    active: bool = True

    @field_validator("code", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        """Fold the code to upper case *before* the pattern is checked.

        Codes are stored and compared upper case, so `west_so` is not a mistake to reject —
        it is the same code typed the way a keyboard is easiest. Rejecting it would only
        teach the operator to hold shift.
        """
        return value.strip().upper() if isinstance(value, str) else value


class AgencyPatchIn(BaseModel):
    """The fields of an existing agency that may change. `code` is not one of them."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    short_name: str | None = Field(default=None, max_length=32)
    kind: str | None = None
    active: bool | None = None


class VocabularyTerm(BaseModel):
    """One value of a controlled vocabulary and what it means."""

    key: str
    label: str
    description: str


class CategoryTerm(VocabularyTerm):
    """A talkgroup category, plus the RadioReference tags that land a channel in it."""

    service_tags: list[str] = Field(default_factory=list)


class ServiceTagTerm(BaseModel):
    """A RadioReference service tag, in RR's own spelling, and the category it maps to."""

    tag: str = Field(examples=["Fire Dispatch"])
    category: str = Field(examples=["fire"])
    category_label: str = Field(examples=["Fire"])
    description: str


class VocabularyOut(BaseModel):
    """Every controlled vocabulary a talkgroup or an agency uses, with its descriptions.

    Served so the Help page and the docs render the *code's* words rather than a second
    copy of them: `app/vocabulary.py` is the only place any of this is written down.
    """

    categories: list[CategoryTerm]
    category_effect: str = Field(description="What choosing a category changes in the app.")
    service_tags: list[ServiceTagTerm]
    agency_kinds: list[VocabularyTerm]
    agency_sources: list[VocabularyTerm]


class ConversationOut(ORMModel):
    """Consecutive calls on one talkgroup with gaps under CONVERSATION_GAP_SECONDS (§3.2)."""

    id: int
    talkgroup_id: int | None = None
    talkgroup_alias: str | None = None
    tgid: int | None = None
    started_at: datetime
    started_local: datetime | None = None
    ended_at: datetime
    call_count: int
    first_call_id: int | None = None
    duration_s: float = 0.0


# --------------------------------------------------------------------------------------
# calls (§3.2)
# --------------------------------------------------------------------------------------


class CallOut(ORMModel):
    """One recorded transmission, denormalized for the table and the live feed.

    Times: ``started_at`` is UTC and authoritative; ``started_local`` is the same instant
    as naive local time (the ``TZ`` setting).  For file ingest ``started_at`` is ``recorded_at −
    duration_s`` and therefore ±1 s (``time_source='file_write'``); a recorder push
    carries the true call start (``time_source='upload'``).
    """

    id: int
    system_id: int
    system_name: str | None = None
    site_id: int | None = None
    site_name: str | None = None
    talkgroup_id: int | None = Field(default=None, description="Talkgroup primary key")
    talkgroup_alias: str | None = None
    talkgroup_alias_at_record: str | None = Field(
        default=None, description="Alias as recorded, so history survives a rename"
    )
    tgid: int | None = Field(default=None, description="Talkgroup id on the air", examples=[1101])
    talkgroup_ids: list[int] = Field(
        default_factory=list, description="Every tgid involved, patch members included"
    )
    category: str | None = None
    service_tag: str | None = Field(
        default=None, description="The talkgroup's RadioReference service tag", examples=["Law Dispatch"]
    )
    agency_code: str | None = None
    color: str | None = None
    patch_id: int | None = None
    patch_label: str | None = Field(default=None, examples=["PATCH 14257"])
    patch_member_tgids: list[int] = Field(default_factory=list)
    kind: CallKindName = Field(
        default="group",
        description=(
            "Who the transmission was addressed to. `group` is traffic on a talkgroup, "
            "patched or not. `unit` is a P25 **private call** — one radio to one radio, on "
            "no talkgroup at all, so `talkgroup_id`, `tgid` and `talkgroup_ids` are empty "
            "and `target_radio_id` names the far end. Filter with `?kind=group|unit`. "
            "Distinct from `call_type`, which says how the channel was granted."
        ),
    )
    target_radio_id: int | None = Field(
        default=None,
        description="Primary key of the radio a `unit` call was addressed to; null on a group call",
    )
    target_radio_label: str | None = Field(
        default=None, description="Unit id, alias or radio id of the `target_radio_id` radio"
    )
    radio_id: int | None = Field(default=None, description="Radio primary key")
    radio_label: str | None = Field(default=None, description="Unit id, alias, or the raw radio id")
    radio_raw: str | None = None
    unit_id: str | None = None
    talker_alias: str | None = None
    radio_resolved: bool = False
    ingest_source: IngestSourceName = "file"
    ingest_priority: str | None = Field(
        default=None,
        description=(
            "The admission class the recorder pushed this call at — `live`, `urgent`, "
            "`backlog`, `bulk` or a bare integer. `null` means it was picked up off disk "
            "by the file watcher, which says nothing about when it was recorded. Filter "
            "with `?priority=live|backlog|bulk`, where `urgent` reads back as `live` and "
            "an integer is judged against the transcription queue's own band edges. A "
            "`backlog`/`bulk` row is archive that arrived long after the fact, and both "
            "the UI and an outside consumer can mark it as such rather than presenting it as news."
        ),
        examples=["live"],
    )
    recorded_at: datetime = Field(description="File-write time from the ID3 COMM frame")
    started_at: datetime
    started_local: datetime = Field(description="Naive local wall clock (the TZ setting)")
    ended_at: datetime
    time_source: TimeSourceName = "file_write"
    duration_s: float
    frequency_hz: int | None = None
    channel: str | None = None
    decoder: str | None = None
    call_type: CallTypeName = "group"
    emergency: bool | None = None
    encrypted: bool | None = None
    audio_type: str | None = Field(
        default=None,
        description="How it was carried on the air, when the recorder said",
        examples=["digital tdma"],
    )
    audio_state: Literal["present", "archived", "purged", "discarded", "missing"] = Field(
        default="present",
        description=(
            "Where the recording is: `present` on disk, `archived` on the cold tier, "
            "`purged` — the row is metadata only, which is how an encrypted call is kept "
            "(§1.7, §5.3) — `discarded`, the recording the discard pass deleted because "
            "the talkgroup's **Record** toggle is off or nobody said anything on it (§5.6), "
            "or `missing`: retention found no file where there should have been one, and "
            "puts the row back to `present` if the file reappears."
        ),
    )
    tone_detected: bool = Field(
        default=False,
        description=(
            "The audio-processing pass found a sustained pure tone in this call — a paging "
            "tone, an alert beep, a warble (§5.5). Filter with `?tone_detected=true`."
        ),
    )
    alert: bool = Field(
        default=False,
        description=(
            "This call is a **page**: a two-tone or alert pattern in its first seconds. "
            "Deliberately not the same thing as `emergency`, which is what the radio "
            "asserted; neither ever overwrites the other. Filter with `?alert=true`."
        ),
    )
    processed_audio: bool = Field(
        default=False,
        description=(
            "A processed copy of the recording exists and is what `audio_url` serves. "
            "Add `?raw=1` to that URL for the recorder's own bytes."
        ),
    )
    audio_meta: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the processing pass measured and did: levels before and after, the trim "
            "it took off each end, the gain applied, and the tone segments it found."
        ),
    )
    duplicate_of: int | None = None
    conversation_id: int | None = None
    transcript_status: TranscriptStatusName = "pending"
    transcript_text: str | None = None
    transcript_highlight: str | None = Field(
        default=None, description="ts_headline markup; only <mark> is emitted"
    )
    transcript_quality: QualityName | None = None
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    starred: bool = False
    audio_url: str = Field(description="Relative URL of the MP3", examples=["/api/calls/42/audio"])
    ingested_at: datetime | None = None
    updated_at: datetime | None = None


class RawMetadata(BaseModel):
    """Exactly what was on disk — for the "raw" panel of the detail page."""

    file_path: str
    file_name: str
    file_size: int | None = None
    sha256: str | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    recorder_version: str | None = None
    file_mtime: datetime | None = None
    id3: dict[str, str] = Field(
        default_factory=dict,
        description="ID3 frames as read (§1.3); empty for a call that did not come from an sdrtrunk file",
    )
    raw_meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The recorder's own metadata, verbatim and unparsed: a trunk-recorder JSON "
            "sidecar (§1.7), an sdrtrunk file's ID3 frame map, or the fields a push "
            "carried. Nothing reads it back — it is the audit trail for every column "
            "above that was derived from it."
        ),
    )


class CallSourceOut(ORMModel):
    """One radio keying up inside one call — trunk-recorder's ``srcList`` (§1.7, §3.2).

    ``pos_s`` is seconds into the recording, so the player can seek straight to the moment
    this unit started talking; ``len_s`` is how long it held the channel.  An sdrtrunk call
    has exactly one, at offset 0, because that is all its files say.
    """

    id: int
    radio_id: int | None = Field(default=None, description="Radio primary key, when resolved")
    radio_label: str | None = Field(default=None, description="Unit id, alias, or the raw radio id")
    src: int | None = Field(default=None, description="Radio id as the recorder reported it")
    pos_s: float = Field(default=0.0, description="Seconds into the recording")
    len_s: float | None = Field(default=None, description="Seconds this radio held the channel")
    emergency: bool = False
    tag: str | None = Field(default=None, description="The recorder's own name for the unit")
    seq: int = 0


class TranscriptSegmentOut(BaseModel):
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


class TranscriptOut(ORMModel):
    """One transcription attempt.  A call can carry several; exactly one is primary."""

    id: int
    call_id: int
    engine: SttEngineName
    backend_name: str | None = None
    model: str | None = None
    language: str | None = None
    text: str | None = Field(default=None, description="edited_text when a human corrected it")
    original_text: str | None = Field(default=None, description="What the engine produced")
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)
    words: list[dict[str, Any]] | None = None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    quality: QualityName | None = None
    quality_reasons: list[str] = Field(default_factory=list)
    prompt_used: str | None = None
    vad_used: bool | None = None
    processing_ms: int | None = None
    rtf: float | None = Field(default=None, description="Audio seconds per second of wall clock")
    created_at: datetime | None = None
    is_primary: bool = False
    edited_by_user: bool = False
    edited_text: str | None = None


class ExternalLinkOut(ORMModel):
    """A confirmed link to something outside this archive — a CAD incident number (§3.3)."""

    id: int
    call_id: int | None = None
    conversation_id: int | None = None
    source: str = Field(examples=["cad"])
    external_id: str = Field(examples=["20260829123"])
    link_confidence: LinkConfidenceName | None = None
    link_method: LinkMethodName | None = None
    link_note: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None


#: The one ``source`` whose defaults are a human's: somebody sat there and asserted the link.
MANUAL_LINK_SOURCE = "manual"


class ExternalLinkIn(BaseModel):
    """Body of ``POST /links``.

    The defaults describe a person: ``source='manual'``, ``link_confidence='strong'``,
    ``link_method='manual'``.  That is right for the archive's own UI and wrong for
    everything else, because an automated caller that sent only ``{call_id, external_id}``
    used to be recorded as a human's confident assertion — a machine guess wearing a
    librarian's signature.  So the defaults are now *only* available to the manual source:
    name any other ``source`` and you must say how sure you are and how you decided, or the
    request is a 422 naming the fields you left out.
    """

    call_id: int | None = None
    conversation_id: int | None = None
    source: str = Field(
        default=MANUAL_LINK_SOURCE,
        description=(
            "Who is asserting the link (`cad`, `manual`, …). Anything but `manual` "
            "makes `link_confidence` and `link_method` required."
        ),
        examples=["cad"],
    )
    external_id: str
    link_confidence: LinkConfidenceName | None = Field(
        default="strong",
        description="Defaulted only for `source=manual`; required for any other source.",
    )
    link_method: LinkMethodName | None = Field(
        default="manual",
        description="Defaulted only for `source=manual`; required for any other source.",
    )
    link_note: str | None = None

    @model_validator(mode="after")
    def _machine_links_are_explicit(self):  # noqa: ANN202
        # ``model_fields_set`` rather than the value: a caller who spells out
        # ``source: "manual"`` is still asking for the manual defaults, and a caller who
        # omits ``source`` entirely has not claimed to be a machine.
        if "source" not in self.model_fields_set:
            return self
        if self.source.strip().lower() == MANUAL_LINK_SOURCE:
            return self
        missing = [
            name
            for name in ("link_confidence", "link_method")
            if getattr(self, name) is None or name not in self.model_fields_set
        ]
        if not missing:
            return self
        raise ValidationError.from_exception_data(
            self.__class__.__name__,
            [
                InitErrorDetails(
                    type=PydanticCustomError(
                        "missing",
                        "Field required when `source` is not '{manual}' (source='{source}')",
                        {"manual": MANUAL_LINK_SOURCE, "source": self.source},
                    ),
                    loc=(name,),
                    input=None,
                )
                for name in missing
            ],
        )


class CallCopyOut(BaseModel):
    """One other copy of the same transmission — docs/RECONCILIATION.md.

    ``score`` is what reconciliation gave this copy when it was compared with the primary:
    the same number the decision was made on, so "why is that one the primary" is
    answerable from the page.
    """

    id: int
    ingest_source: IngestSourceName = "file"
    duration_s: float
    audio_state: Literal["present", "archived", "purged", "discarded", "missing"] = "present"
    started_at: datetime
    time_source: TimeSourceName = "file_write"
    transcript_status: TranscriptStatusName = "pending"
    score: float


class CallDetail(CallOut):
    """``GET /calls/{id}`` — the row plus everything the detail page needs in one trip."""

    system: SystemOut | None = None
    site: SiteOut | None = None
    talkgroup: TalkgroupOut | None = None
    radio: RadioOut | None = None
    #: the far end of a `unit` call — the radio it was addressed to.  Null on a group call,
    #: which is the whole of what `kind` distinguishes.
    target_radio: RadioOut | None = None
    patch: PatchOut | None = None
    sources: list[CallSourceOut] = Field(
        default_factory=list, description="Every radio that keyed up, in order (§1.7)"
    )
    raw: RawMetadata
    transcript: TranscriptOut | None = Field(default=None, description="The primary transcript")
    transcript_count: int = 0
    prev_call_id: int | None = Field(
        default=None,
        description=(
            "Previous call on the same channel — the same talkgroup for a group call, the "
            "same pair of radios for a `unit` one, which has no talkgroup to step through"
        ),
    )
    next_call_id: int | None = None
    conversation: ConversationOut | None = None
    conversation_call_ids: list[int] = Field(default_factory=list)
    links: list[ExternalLinkOut] = Field(default_factory=list)
    duplicates: list[CallCopyOut] = Field(
        default_factory=list,
        description="Other recorders' copies of this transmission, scored (§5.1 step 4)",
    )
    reconcile_reason: dict[str, Any] | None = Field(
        default=None, description="Why reconciliation ruled the way it did"
    )
    source_instance: str | None = Field(
        default=None,
        description="The recorder instance whose audio this is, when the push named one",
        examples=["rec-01"],
    )
    source_quality: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What that recorder said about how well it heard the transmission — "
            "`voice_frames`, `bad_frames`, `rssi_dbfs` — stored verbatim. It is what a "
            "second receiver's copy is compared against (docs/RECORDERS.md, 'Best source')."
        ),
    )
    reconciled_at: datetime | None = None


class ConversationThread(ORMModel):
    """``GET /conversations/{id}`` — the conversation and every call in it, oldest first."""

    conversation: ConversationOut
    calls: list[CallOut] = Field(default_factory=list)
    total: int = 0


class CallPatch(BaseModel):
    """``PATCH /calls/{id}`` — user-editable fields plus a transcript correction."""

    notes: str | None = None
    tags: list[str] | None = None
    starred: bool | None = None
    transcript_text: str | None = Field(
        default=None,
        description=(
            "Corrected transcript. Stored on the primary transcript as `edited_text` with "
            "`edited_by_user=true` and denormalized onto the call, so the engine's own output "
            "is never overwritten."
        ),
    )


class WaveformOut(BaseModel):
    """Peak envelope for the player, cached on disk after the first request."""

    call_id: int
    duration_s: float
    peaks: list[float] = Field(description="Normalized 0..1 peaks, one per bucket")
    buckets: int = 200


class TranscribeRequest(BaseModel):
    backend: str | None = Field(default=None, description="Name of an STT_BACKENDS entry")
    model: str | None = None
    priority: int | None = None
    force: bool = Field(default=False, description="Re-queue even when a transcript exists")


class TranscribeAccepted(BaseModel):
    call_id: int
    job_id: int | None = None
    status: str
    detail: str | None = None


# --------------------------------------------------------------------------------------
# facets and search (§4.1, §4.3)
# --------------------------------------------------------------------------------------


class FacetBucket(BaseModel):
    key: str
    label: str
    count: int


class FacetsOut(BaseModel):
    """``GET /calls/facets`` — counts per dimension for the *current* filter."""

    talkgroup: list[FacetBucket] = Field(default_factory=list)
    radio: list[FacetBucket] = Field(default_factory=list)
    agency: list[FacetBucket] = Field(default_factory=list)
    category: list[FacetBucket] = Field(default_factory=list)
    hour: list[FacetBucket] = Field(default_factory=list, description="24 buckets, key '00'..'23'")
    dow: list[FacetBucket] = Field(default_factory=list, description="7 buckets, key '0'=Sunday")
    transcript_status: list[FacetBucket] = Field(default_factory=list)
    quality: list[FacetBucket] = Field(default_factory=list)
    tag: list[FacetBucket] = Field(default_factory=list)
    total: int = 0
    incomplete: bool = Field(
        default=False,
        description=(
            "True when at least one dimension was dropped because its query hit the facet "
            "statement timeout. The dimensions that are present are still exact for the "
            "filter; the missing ones are empty. A sidebar should say so rather than read "
            "an empty list as a zero."
        ),
    )


class SearchHit(BaseModel):
    call: CallOut
    highlight: str = Field(description="ts_headline output; only <mark> markup")
    rank: float = Field(description="ts_rank_cd of the match")


class FuzzyTermOut(BaseModel):
    term: str = Field(
        description="A word of `q` as the index stores it — stemmed, so `melrose` is `melros`"
    )
    variants: list[str] = Field(
        description=(
            "The archive's own spellings it was widened to (stemmed the same way), fewest "
            "edits first; empty when none was close enough"
        )
    )


class FuzzyOut(BaseModel):
    """What a fuzzy search ran, so the reader can be told."""

    automatic: bool = Field(
        description=(
            "True when the exact query matched nothing and `fallback=true` retried it with "
            "spelling variants; false when the request itself asked for `fuzzy=true`"
        )
    )
    widened: bool = Field(
        description="At least one word had a variant; false means the results are the exact ones"
    )
    terms: list[FuzzyTermOut]


class SearchResponse(BaseModel):
    items: list[SearchHit]
    total: int = Field(description="Calls matching `q` and the filters, ignoring pagination")
    total_capped: bool = Field(
        default=False,
        description=(
            "True when `total` stopped at the counting cap (50 000) and the real number is "
            "larger, as on `GET /calls`"
        ),
    )
    page: int
    page_size: int
    facets: FacetsOut | None = None
    fuzzy: FuzzyOut | None = Field(
        default=None,
        description=(
            "Present only when spelling variants were looked up: with `fuzzy=true`, or when "
            "`fallback=true` found nothing exact. Absent on every exact search."
        ),
    )


class Suggestion(BaseModel):
    value: str
    kind: Literal["talkgroup", "unit", "term", "radio", "tag", "person"]
    count: int
    label: str | None = None
    fuzzy: bool = Field(
        default=False,
        description=(
            "A near spelling rather than a substring match — only with `fuzzy=true`, and only "
            "after every substring match"
        ),
    )


class SavedSearchOut(ORMModel):
    id: int
    name: str
    query: str = Field(description="Query string that recreates the search")
    shared: bool = False
    created_at: datetime | None = None
    created_by: str | None = None


class SavedSearchIn(BaseModel):
    name: str
    query: str
    shared: bool = False


class FeedIn(BaseModel):
    """Create a feed: a name and a ``GET /calls`` filter object."""

    name: str = Field(min_length=1, max_length=200)
    #: the SPA's filter object, exactly as a saved search stores it
    query: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    include_audio: bool = False
    max_items: int = Field(default=50, ge=1, le=200)


class FeedPatch(BaseModel):
    """Edit a feed.  Every field is optional; only what is sent is changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    query: dict[str, Any] | None = None
    enabled: bool | None = None
    include_audio: bool | None = None
    max_items: int | None = Field(default=None, ge=1, le=200)


class FeedOut(BaseModel):
    """One feed, with the two absolute URLs a reader subscribes to.

    ``rss_url`` and ``atom_url`` contain the token, which **is** the credential: anybody
    holding one of these URLs can read every call the filter matches.
    """

    id: int
    name: str
    #: the filter as it was parsed — what will actually apply, not what was sent
    query: dict[str, Any] = Field(default_factory=dict)
    #: a sentence describing ``query`` for somebody who did not build it
    summary: str = ""
    enabled: bool = True
    include_audio: bool = False
    max_items: int = 50
    rss_url: str
    atom_url: str
    owner: str | None = Field(default=None, description="Username, when an admin is listing")
    created_at: datetime
    updated_at: datetime
    last_fetched_at: datetime | None = None
    fetch_count: int = 0


class FeedPresetTalkgroup(BaseModel):
    """One of the busiest channels a built-in feed covers, so a reader can see what is
    actually in it rather than only how many talkgroups are filed under it."""

    tgid: int
    alias: str | None = None
    #: calls on this talkgroup in the last seven days
    calls_7d: int = 0


class FeedPresetOut(BaseModel):
    """A built-in feed: a fixed filter at a stable URL that holds no secret.

    Unlike :class:`FeedOut` there is no token here and nothing to rotate — the URLs are
    meant to be handed out, and what they publish is governed by the archive's own read
    policy rather than by possession of the link.
    """

    #: ``all``, or a talkgroup category — the last path segment, stable for ever
    slug: str
    name: str
    #: what belongs in it, in the words the Help page uses
    description: str = ""
    #: the ``GET /calls`` filter it publishes
    query: dict[str, Any] = Field(default_factory=dict)
    #: how many talkgroups are filed under it right now
    talkgroups: int = 0
    #: calls matching the feed in the last seven days — what it publishes, in volume
    calls_7d: int = 0
    #: the busiest channels in it over that week, at most five, busiest first
    top_talkgroups: list[FeedPresetTalkgroup] = Field(default_factory=list)
    rss_url: str
    atom_url: str


class RecentTranscriptOut(BaseModel):
    """One row of the embeddable live transcript view (docs/EMBED.md) — just what it shows.

    ``transcript_text`` is the call's denormalized text, which is the human correction
    whenever there is one (``PATCH /calls/{id}`` writes it there), so an edit wins here
    exactly as it does everywhere else.
    """

    id: int
    started_at: datetime
    started_local: datetime = Field(description="Naive local wall clock (the TZ setting)")
    duration_s: float = 0.0
    tgid: int | None = None
    talkgroup_alias: str | None = None
    radio_label: str | None = None
    transcript_text: str | None = None
    transcript_status: str
    transcript_quality: str | None = None


class EmbedPresetOut(BaseModel):
    """A built-in feed the embed's filter bar can start from: its slug and a short name."""

    slug: str
    name: str


class RecentTranscriptsOut(BaseModel):
    """The first screen of ``/embed/transcripts``, plus the filter to tail it live with."""

    #: the window the rows were read over, and the moment it started
    minutes: int
    since: datetime
    #: the built-in feed the filter started from, or null
    preset: str | None = None
    #: the resolved filter as a ``WS /api/live`` first frame — the preset folded in, no
    #: date bounds (the live feed is rolling)
    filter: dict[str, Any] = Field(default_factory=dict)
    #: pass back as ``cursor`` for the rows before the last one here; null at the end
    next_cursor: str | None = None
    items: list[RecentTranscriptOut] = Field(default_factory=list)
    #: the built-in feeds, only when ``presets=true`` was asked for
    presets: list[EmbedPresetOut] | None = None


class FavoriteOut(BaseModel):
    talkgroup_id: int
    alias: str | None = None
    tgid: int | None = None
    muted: bool = False


class FavoriteIn(BaseModel):
    talkgroup_id: int
    muted: bool = False


# --------------------------------------------------------------------------------------
# statistics (§4.5)
# --------------------------------------------------------------------------------------


class StatsOverview(BaseModel):
    """The headline numbers on the dashboard."""

    calls_total: int
    calls_today: int
    calls_24h: int = 0
    unit_calls_24h: int = Field(
        default=0,
        description=(
            "Unit-to-unit (private) calls in the last 24 hours — a subset of `calls_24h`, "
            "not a second total to add to it."
        ),
    )
    audio_hours_total: float
    audio_hours_today: float
    transcript_coverage: float = Field(
        description="0..1 — share of calls eligible for transcription that have one"
    )
    transcripts_total: int = 0
    stt_rtf: float | None = Field(
        default=None, description="Audio seconds per second of processing, from transcripts"
    )
    queue_depth: int
    running_jobs: int = 0
    failed_jobs: int = 0
    ingest_lag_s: float | None = Field(default=None, description="now − max(recorded_at)")
    last_call_at: datetime | None = None
    recorder_gaps_24h: int = 0
    talkgroups_active_24h: int = 0
    radios_active_24h: int = 0
    audio_discarded_24h: int = Field(
        default=0,
        description=(
            "Calls ingested in the last 24 hours whose audio was deleted by the discard "
            "pass because their talkgroup's **Record** toggle is off. The metadata and any "
            "transcript are still there; only the recording is gone."
        ),
    )
    no_speech_24h: int = Field(
        default=0,
        description=(
            "Calls in the last 24 hours the audio pass found nothing in — a mic keyed and "
            "released with nothing said (`audio_meta.content = \"none\"`). A pager tone-out "
            "with no words behind it is `\"page\"` and is not one of these. They are counted "
            "in `calls_24h` like any other transmission; they are simply never transcribed or "
            "exported, and their audio is discarded when `audio_discard_no_speech` is on."
        ),
    )
    static_24h: int = Field(
        default=0,
        description=(
            "Calls in the last 24 hours that were broadband static rather than speech — a "
            "squelch tail or a failed decode, noise across the whole vocoder band "
            '(`audio_meta.content = "static"`). Refused by the same gates as '
            "`no_speech_24h` — never transcribed, never exported, and discarded when "
            "`audio_discard_no_speech` is on — and counted apart from it because a rising "
            "static count is an RF or subscriber-radio fault, not quiet traffic."
        ),
    )
    disk_free_bytes: int | None = None
    disk_total_bytes: int | None = None


class ActivityCell(BaseModel):
    talkgroup: int | None = Field(default=None, description="Talkgroup primary key, null = all")
    tgid: int | None = None
    alias: str | None = None
    bucket: datetime
    calls: int
    seconds: float


class ActivityResponse(BaseModel):
    """``GET /stats/activity`` — heatmap-ready cells plus the matrix the UI renders."""

    bucket: Literal["hour", "day"]
    cells: list[ActivityCell]
    buckets: list[str] = Field(default_factory=list, description="Column labels, in order")
    rows: list[str] = Field(default_factory=list, description="Row labels, in order")
    matrix: list[list[int]] = Field(default_factory=list)
    max: int = 0


class TalkgroupStat(BaseModel):
    talkgroup_id: int
    tgid: int
    alias: str | None = None
    category: str | None = None
    agency_code: str | None = None
    call_count: int
    audio_seconds: float
    last_heard_at: datetime | None = None
    spark: list[int] = Field(default_factory=list, description="24 hourly counts, oldest first")


class RadioStat(BaseModel):
    radio_id: int
    radio_id_raw: int
    alias: str | None = None
    unit_id: str | None = None
    kind: RadioKindName = "unknown"
    agency_code: str | None = None
    call_count: int
    audio_seconds: float
    last_heard_at: datetime | None = None
    top_talkgroup: str | None = None
    spark: list[int] = Field(default_factory=list)


class BackendThroughput(BaseModel):
    backend_name: str
    engine: str | None = None
    calls: int
    audio_seconds: float
    processing_seconds: float
    rtf: float | None = None


class StatusCount(BaseModel):
    status: str
    count: int


class QualityCount(BaseModel):
    quality: str
    count: int


class TranscriptionStats(BaseModel):
    queue_depth: int
    running: int
    failed_24h: int
    status: list[StatusCount]
    quality: list[QualityCount]
    backends: list[BackendThroughput]


class RecorderGapOut(ORMModel):
    id: int
    system_id: int
    system_name: str | None = None
    started_at: datetime
    ended_at: datetime
    duration_s: float
    minutes: float
    note: str | None = None
    detected_at: datetime | None = None


class TimelinePoint(BaseModel):
    minute: datetime
    count: int
    seconds: float = 0.0


# --------------------------------------------------------------------------------------
# operations (§4.7)
# --------------------------------------------------------------------------------------


class IngestStatusOut(BaseModel):
    watching: bool = False
    audio_root: str
    last_run_at: datetime | None = None
    last_run_files_new: int = 0
    files_seen_24h: int = 0
    files_new_24h: int = 0
    files_duplicate_24h: int = 0
    files_failed_24h: int = 0
    lag_s: float | None = None
    pending_files: int = 0
    calls: int = 0
    duplicates: int = 0
    ledger: dict[str, int] = Field(default_factory=dict)
    jobs: dict[str, int] = Field(default_factory=dict)
    #: When the cached half of this block (``calls``, ``duplicates``, ``ledger``, ``jobs``,
    #: ``last_run``) was last measured, and how long ago that was.  Those numbers are full
    #: scans of the archive and are recomputed at most once a minute
    #: (:func:`app.ingest.service.ingest_status`); everything else here is current.  Both
    #: are optional additions — a client that ignores them sees exactly the old shape.
    as_of: datetime | None = None
    cached_s: float | None = None


class IngestRunOut(ORMModel):
    id: int
    kind: str
    root: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    files_seen: int
    files_new: int
    files_duplicate: int
    files_skipped: int
    files_failed: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


class AdminIngestOut(BaseModel):
    status: IngestStatusOut
    runs: list[IngestRunOut]


class IngestFileOut(ORMModel):
    id: int
    path: str
    status: IngestFileStatusName
    error: str | None = None
    call_id: int | None = None
    attempts: int = 0
    first_seen_at: datetime | None = None
    last_attempt_at: datetime | None = None


class TranscriptionJobOut(ORMModel):
    id: int
    call_id: int
    talkgroup_alias: str | None = None
    status: JobStatusName
    priority: int
    backend_name: str | None = None
    model: str | None = None
    attempts: int
    max_attempts: int
    last_error: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    locked_by: str | None = None


class SttBackendStatOut(BaseModel):
    """One backend as the worker last reported it (``stt_backend_stats``).

    ``stale`` is the honest answer to "is anybody publishing this": the row is kept when the
    worker stops so the page can say *since when*, rather than showing an empty table that
    looks like an idle system.
    """

    name: str
    kind: str | None = None
    model: str | None = None
    base_url: str | None = None
    priority: int = 100
    concurrency: int = 1
    #: configuration AND the operator's override — what the router actually routes on
    enabled: bool = True
    config_enabled: bool = True
    #: NULL means "follow the configuration"
    admin_enabled: bool | None = None
    admin_changed_at: datetime | None = None
    admin_changed_by: str | None = None
    healthy: bool | None = None
    health_detail: str | None = None
    disqualified: str | None = None
    in_flight: int = 0
    attempts: int = 0
    successes: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success_at: datetime | None = None
    audio_s: float = 0.0
    processing_s: float = 0.0
    mean_rtf: float | None = None
    mean_processing_s: float | None = None
    recent_completed_1m: int = 0
    recent_failed_1m: int = 0
    worker_instance: str | None = None
    #: when the worker process that published this row started
    started_at: datetime | None = None
    updated_at: datetime | None = None
    #: no worker has published this row recently (or ever)
    stale: bool = True

    # -- the configuration half of the row (``stt_backends``) --------------------------
    # Carried on the same object rather than behind a second request because the page shows
    # them together and the edit dialog opens from the row the operator is already looking
    # at.  ``api_key`` is deliberately absent: the field is write-only, and the only thing
    # the browser is told is whether one is set.
    has_api_key: bool = False
    #: ``env`` (seeded from ``STT_BACKENDS``) or ``db`` (written from the admin page).
    #: ``None`` for a backend that has no row — the environment is still the configuration.
    config_source: str | None = None
    device: str | None = None
    compute_type: str | None = None
    cpu_threads: int | None = None
    cache_dir: str | None = None
    use_prompt: bool | None = None
    use_hotwords: bool | None = None
    timeout: float | None = None
    notes: str | None = None
    #: set when the backend was removed but transcripts still name it — history is kept
    deleted_at: datetime | None = None
    #: false for a backend that exists only in ``STT_BACKENDS``, or only in the stats table
    #: as the leftovers of one that has gone: neither can be edited from here
    editable: bool = False
    #: the ``stt_config_version`` the worker's router was built from when it wrote this row
    config_version: int | None = None


class SttQueueOut(BaseModel):
    """The transcription queue from ``transcription_jobs`` — the cross-process truth.

    The per-backend counters are one process' memory; these numbers are what the database
    says, which is what makes them comparable with what the operator sees on the Calls page.
    """

    queued_live: int = 0
    queued_backlog: int = 0
    running: int = 0
    completed_1h: int = 0
    failed_1h: int = 0
    #: mean ``finished_at - started_at`` over the last hour's completions
    mean_processing_s: float | None = None
    #: jobs finished per minute over the last ten minutes
    throughput_per_min: float = 0.0
    #: audio seconds behind the queued and running jobs
    audio_seconds_queued: float = 0.0
    #: how long the current queue takes at the measured rate; null when nothing is moving
    eta_seconds: float | None = None


class SttBackendsOut(BaseModel):
    """The admin backends page in one response."""

    backends: list[SttBackendStatOut] = []
    queue: SttQueueOut = SttQueueOut()
    #: false when no worker has published anything recently — the page says so explicitly
    reporting: bool = False
    worker_instance: str | None = None
    worker_started_at: datetime | None = None
    #: the freshest ``updated_at`` across the rows, stale or not
    last_report_at: datetime | None = None
    #: the configuration generation the database is at right now
    config_version: int = 0
    #: the generation the worker's router was actually built from.  Behind ``config_version``
    #: means the worker has not applied the last save yet; it picks it up within its publish
    #: interval.  ``None`` means no worker has reported since the column existed.
    applied_config_version: int | None = None
    #: the table has no rows, so ``STT_BACKENDS`` is still what this deployment runs on
    from_environment: bool = False


class SttBackendEnabledIn(BaseModel):
    """The operator's override: on, off, or ``null`` to go back to following the config."""

    enabled: bool | None = None


IngestSourceKindName = Literal["watch_dir", "push_token", "migration"]


class IngestSourceIn(BaseModel):
    """A place recordings come from — the body of POST/PUT ``/admin/ingest/sources``.

    One model for three kinds, with the per-kind fields optional and validated by the
    endpoint, rather than three models: the operator's mental object is "a source", the
    three kinds differ by a handful of fields, and a discriminated union would put that
    difference in the schema at the cost of three names to keep in step.
    """

    kind: IngestSourceKindName
    label: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    #: ``watch_dir``: the directory to watch.  Required for that kind, rejected for others.
    path: str | None = None
    recursive: bool = True
    #: ``watch_dir``: which file to keep when trunk-recorder wrote both a WAV and an M4A.
    #: ``null`` follows ``TR_PREFER_AUDIO``.
    tr_prefer_audio: Literal["m4a", "wav"] | None = None
    #: ``watch_dir``: how long to wait for trunk-recorder's ``.json`` sidecar.  ``null``
    #: follows ``TR_SIDECAR_WAIT_SECONDS``.
    tr_sidecar_wait_seconds: float | None = Field(default=None, ge=0, le=600)
    #: ``push_token``: the upload token this recorder presents in the ``key`` form field
    token_id: int | None = None
    notes: str | None = None


class IngestSourceOut(BaseModel):
    """One source, with whatever is known about whether it is actually working.

    ``path_state`` is checked on read rather than stored, because the interesting failure —
    an NFS mount that went away at 03:00 — happens long after the save that validated it.
    """

    id: int
    kind: IngestSourceKindName
    label: str
    enabled: bool = True
    path: str | None = None
    recursive: bool = True
    tr_prefer_audio: str | None = None
    tr_sidecar_wait_seconds: float | None = None
    token_id: int | None = None
    #: the token's label, so the row reads as a recorder rather than as an id
    token_label: str | None = None
    #: the token has been revoked or has expired — the recorder's pushes are being refused
    token_active: bool | None = None
    notes: str | None = None
    meta: dict[str, Any] | None = None
    last_activity_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    #: ``watch_dir`` only: ``ok`` | ``missing`` | ``not_a_directory`` | ``unreadable`` | ``n/a``
    path_state: str = "n/a"
    path_detail: str | None = None


class IngestSourcesOut(BaseModel):
    """Every source, plus the generation the watcher is polling for."""

    sources: list[IngestSourceOut] = []
    config_version: int = 0


class IngestSourceStatusOut(BaseModel):
    """What one source has actually done lately — the answer to "is this thing on"."""

    id: int
    kind: IngestSourceKindName
    label: str
    enabled: bool = True
    path_state: str = "n/a"
    path_detail: str | None = None
    #: ``watch_dir``: ledger rows under this path in the last 24 hours
    files_new_24h: int = 0
    files_failed_24h: int = 0
    #: ``push_token``: calls that arrived over the push path in the last 24 hours — every
    #: recorder's, not this token's.  A call records *how* it arrived, never which token
    #: authenticated it, so the per-token fact is ``last_activity_at`` (the token's own
    #: ``last_used_at``) rather than a count.
    calls_24h: int = 0
    last_activity_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    meta: dict[str, Any] | None = None


class IngestMigrationProgressIn(BaseModel):
    """What a bulk-import run posts about itself.

    The import tool is a CLI on somebody's laptop, not part of this process, so its progress
    can only be *reported*.  ``label`` names (and, the first time, creates) the ``migration``
    source row it reports against — which is what turns "a long import is running somewhere"
    into a card on the Ingest tab.
    """

    label: str = Field(min_length=1, max_length=120)
    state: str | None = Field(default=None, max_length=40)
    files_total: int | None = Field(default=None, ge=0)
    files_done: int | None = Field(default=None, ge=0)
    files_failed: int | None = Field(default=None, ge=0)
    message: str | None = Field(default=None, max_length=2000)
    finished: bool = False


class SttBackendConfigIn(BaseModel):
    """A backend as the operator defines it — the body of POST and PUT ``/admin/stt/backends``.

    Mirrors :class:`app.config.SttBackend` field for field, because the table *is* that
    model with a primary key; anything else would be a second vocabulary for one concept.

    ``api_key`` is write-only, and its two falsy values mean different things on a PUT:
    ``null`` (or an omitted field) leaves the stored key alone, and ``""`` clears it.  There
    is no way to read one back, so an operator editing a backend's URL must not have to
    re-paste its key, and a browser must not be able to ask for it.
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    kind: Literal["openai_compatible", "ovms", "faster_whisper_local", "openvino_genai"] = (
        "openai_compatible"
    )
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    device: str = ""
    compute_type: str = "int8"
    cpu_threads: int = Field(default=4, ge=1, le=256)
    cache_dir: str = ""
    use_prompt: bool = True
    use_hotwords: bool = False
    concurrency: int = Field(default=2, ge=1, le=64)
    priority: int = Field(default=100, ge=0, le=10_000)
    timeout: float = Field(default=300.0, gt=0, le=3600)
    enabled: bool = True
    notes: str | None = None


class SttBackendPut(SttBackendConfigIn):
    """PUT's body: the same fields, with the name coming from the path instead."""

    name: str | None = None  # ignored; the path segment is the identity


class SttBackendDeleteOut(BaseModel):
    """What a delete did — which is not always a delete."""

    name: str
    #: the row was dropped outright (nothing in the archive refers to it)
    removed: bool = False
    #: the row was kept, marked deleted and disabled, because transcripts still name it
    retained: bool = False
    #: how many transcripts name this backend — why it was retained
    transcripts: int = 0
    config_version: int = 0


class SttBackendProbeOut(BaseModel):
    """The result of asking one backend, right now, whether it works.

    Two questions in one answer: the health check (does the server respond, does it have the
    model) and — when a short clip could be found in the archive — one real transcription,
    because "answers its health endpoint" and "can transcribe" are not the same claim and it
    is the second one the operator is about to depend on.
    """

    name: str
    ok: bool = False
    #: what the health check said, or why the probe could not run at all
    detail: str | None = None
    kind: str | None = None
    model: str | None = None
    #: seconds the health check took
    health_s: float | None = None
    #: the benchmark clip, when one was transcribed
    clip_call_id: int | None = None
    audio_s: float | None = None
    #: wall seconds the transcription took
    latency_s: float | None = None
    #: audio seconds per wall second — the number worth comparing between backends
    rtf: float | None = None
    text: str | None = None
    #: why no clip was transcribed (no suitable recording, or the health check failed first)
    benchmark_skipped: str | None = None
    checked_at: datetime | None = None


class SttBackendOut(BaseModel):
    name: str
    engine: str
    model: str | None = None
    base_url: str | None = None
    enabled: bool = True
    priority: int = 100
    concurrency: int = 1
    healthy: bool | None = None
    detail: str | None = None
    last_error: str | None = None
    rtf: float | None = None
    queue_depth: int = 0
    calls_24h: int = 0


class AudioStatusOut(BaseModel):
    """What the Audio admin tab shows: the queue, the archive's coverage, today's tones."""

    enabled: bool = Field(description="`audio_process_enabled`, as it is in force right now")
    version: int = Field(description="The pipeline version new work is written at")
    reprocess_version: int = Field(
        description="The version the backfill treats as current; bump it to reprocess lazily"
    )
    jobs: dict[str, int] = Field(
        default_factory=dict, description="Audio jobs by status: queued/running/done/failed"
    )
    processed: int = Field(default=0, description="Calls with a processed copy")
    pending: int = Field(default=0, description="Calls with audio and no current processed copy")
    failed: int = Field(default=0, description="Audio jobs parked as failed")
    tone_today: int = Field(default=0, description="Calls with a tone found, since local midnight")
    alert_today: int = Field(default=0, description="Pages since local midnight")
    average_gain_db: float | None = Field(
        default=None, description="Mean gain applied over the last 1000 processed calls"
    )


class AudioReprocessIn(BaseModel):
    """The same selection ``keyup process-audio`` takes."""

    since: str | None = Field(default=None, description="ISO date/timestamp; calls on or after")
    talkgroup: int | None = Field(default=None, description="Only calls involving this tgid")
    ids: list[int] | None = Field(default=None, description="Explicit call ids")
    only_tone_flagged: bool = Field(
        default=False,
        description=(
            "Only calls the previous pass heard a tone in (`tone_detected`). What a "
            "detector change is re-run over: every row it may have called wrong."
        ),
    )
    force: bool = Field(default=False, description="Requeue even calls whose copy is current")
    priority: int = Field(default=600, ge=1, le=10_000, description="Queue priority; 600 is backlog")
    limit: int = Field(default=100_000, ge=1, le=1_000_000)


class AudioReprocessOut(BaseModel):
    matched: int
    queued: int
    already_open: int = Field(description="Matched calls that already had an open job")
    priority: int
    force: bool = False


class AudioRepairPrimariesIn(BaseModel):
    """The window to sweep for primaries reconciliation left without processed audio."""

    since: str | None = Field(
        default=None, description="ISO date/timestamp; primaries started on or after"
    )
    limit: int = Field(default=100_000, ge=1, le=1_000_000)


class AudioRepairPrimariesOut(BaseModel):
    """What the sweep found and what it did about it."""

    matched: int = Field(description="Primaries with audio, with copies, and no processed file")
    queued: int
    already_open: int = Field(description="Matched calls that already had an open job")
    priority: int
    since: str | None = None


class AudioPreviewIn(BaseModel):
    """Run the chain on one call without storing anything."""

    call_id: int = Field(description="The call to run the chain over")


class AudioPreviewOut(BaseModel):
    """Before/after for one call, plus where to hear the result."""

    call_id: int
    meta: dict[str, Any] = Field(description="Everything the pass measured and did")
    filter: str = Field(description="The ffmpeg -af chain that was run, verbatim")
    raw_url: str = Field(description="The recorder's own audio")
    processed_url: str = Field(description="What the chain produced, as it would be stored")


# --------------------------------------------------------------------------------------
# audio profiles (docs/AUDIO.md)
# --------------------------------------------------------------------------------------

#: A profile is at most this many steps; the number is enforced by
#: ``app.audioproc.profiles.MAX_STEPS``, and repeated here so the schema refuses an
#: oversized body before it reaches the validator.
AUDIO_PROFILE_MAX_STEPS = 12


class AudioProfileStep(BaseModel):
    """One filter in a profile's chain: a whitelisted kind and its numbers.

    ``params`` is deliberately loose here and strict in
    :func:`app.audioproc.profiles.normalize_chain`, which is the single place that knows
    which parameters each kind takes and what range each one is confined to. Splitting that
    knowledge between a Pydantic model and the renderer would give two answers to "is this
    chain safe", and only one of them would be the one ffmpeg is handed.
    """

    kind: str = Field(
        description="A step kind from `GET /admin/audio/profiles/steps`", examples=["equalizer"]
    )
    params: dict[str, float] = Field(
        default_factory=dict,
        description="The step's numbers; anything omitted takes the kind's default",
        examples=[{"f": 1950, "width": 500, "gain_db": -40}],
    )


class AudioProfileOut(ORMModel):
    """A named filter chain talkgroups can be processed through."""

    id: int
    name: str = Field(examples=["Aircraft cabin"])
    description: str | None = None
    chain: list[AudioProfileStep] = Field(
        default_factory=list, description="The steps, in the order the audio meets them"
    )
    builtin: bool = Field(
        default=False,
        description=(
            "One of the three shipped profiles. Editable — an operator who wants a "
            "different noise floor on `Default` should have it — but not deletable."
        ),
    )
    rendered: str = Field(
        default="",
        description=(
            "The chain as a standalone ffmpeg `-af` string, loudness tail included. What "
            "the preview renders; the chain a *call* gets additionally carries the trim "
            "and tone stages measured from that call."
        ),
    )
    talkgroup_count: int = Field(default=0, description="Talkgroups assigned to this profile")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AudioProfileIn(BaseModel):
    """Body of ``POST /admin/audio/profiles``."""

    name: str = Field(min_length=1, max_length=120, examples=["Aircraft cabin"])
    description: str | None = Field(default=None, max_length=2000)
    chain: list[AudioProfileStep] = Field(
        default_factory=list, max_length=AUDIO_PROFILE_MAX_STEPS
    )


class AudioProfilePatch(BaseModel):
    """Body of ``PATCH /admin/audio/profiles/{id}``; only the keys present are written."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    chain: list[AudioProfileStep] | None = Field(default=None, max_length=AUDIO_PROFILE_MAX_STEPS)


class AudioProfileParamSpec(BaseModel):
    """One parameter of one step kind, as the editor needs to draw its field."""

    name: str
    low: float
    high: float
    default: float | None = None
    unit: str = ""
    help: str = ""
    optional: bool = Field(
        default=False,
        description=(
            "This parameter has no default: left blank it is not passed to ffmpeg at all, "
            "and the filter's own default applies. Nothing in a step is *required*."
        ),
    )


class AudioProfileStepSpec(BaseModel):
    """One entry of the step whitelist — what the editor offers and what it explains."""

    kind: str
    label: str
    help: str
    params: list[AudioProfileParamSpec] = Field(default_factory=list)


class AudioProfileCatalogOut(BaseModel):
    """``GET /admin/audio/profiles/steps`` — the whitelist, so the form is generated from it."""

    max_steps: int = Field(description="Longest chain a profile may hold")
    steps: list[AudioProfileStepSpec]


class AudioProfilePreviewIn(BaseModel):
    """Render an arbitrary chain over one call's **original** audio, storing nothing."""

    call_id: int = Field(description="The call to render")
    chain: list[AudioProfileStep] = Field(
        default_factory=list,
        max_length=AUDIO_PROFILE_MAX_STEPS,
        description="The steps to try. An empty chain renders the loudness tail alone.",
    )
    seconds: float | None = Field(
        default=None,
        gt=0,
        le=60,
        description="Render only the first N seconds (the cap is 60 either way)",
    )


class AudioProfileApplyIn(BaseModel):
    """Assign a profile to talkgroups and requeue their recent calls."""

    talkgroup_ids: list[int] | None = Field(
        default=None, description="Talkgroup **row ids** to assign this profile to"
    )
    all_with_profile: bool = Field(
        default=False,
        description=(
            "Skip the assignment and requeue every talkgroup already on this profile — "
            "what to press after *editing* a profile, as opposed to after assigning it."
        ),
    )
    hours: int = Field(
        default=24, ge=1, le=24 * 365, description="How far back to requeue calls; 24 h by default"
    )
    limit: int = Field(default=100_000, ge=1, le=1_000_000)


class AudioProfileApplyOut(BaseModel):
    """What the apply assigned and what it queued."""

    profile_id: int
    profile_name: str
    assigned: int = Field(description="Talkgroups whose profile changed")
    talkgroups: int = Field(description="Talkgroups now on this profile that were swept")
    matched: int = Field(description="Calls in the window on those talkgroups")
    queued: int
    already_open: int = Field(description="Matched calls that already had an open job")
    hours: int


class RetentionPolicyOut(ORMModel):
    id: int
    scope: RetentionScopeName
    scope_id: int | None = None
    scope_label: str = ""
    keep_days: int = 0
    keep_audio_days: int = 0
    keep_transcripts: bool = True
    archive_path: str | None = None
    enabled: bool = True


class RetentionPolicyIn(BaseModel):
    id: int | None = None
    scope: RetentionScopeName = "global"
    scope_id: int | None = None
    keep_days: int = 0
    keep_audio_days: int = 0
    keep_transcripts: bool = True
    archive_path: str | None = None
    enabled: bool = True


class RetentionRunResult(BaseModel):
    audio_deleted: int = 0
    audio_archived: int = 0
    calls_purged: int = 0
    #: ``radio_events`` rows aged out by ``RADIO_EVENTS_KEEP_DAYS``, which is a clock of its
    #: own and runs even when every call policy keeps for ever (docs/RADIO_EVENTS.md)
    radio_events_purged: int = 0
    bytes_freed: int = 0
    dry_run: bool = False


class SettingDefinition(BaseModel):
    """One row of ``GET /admin/settings``: the env default, plus any database override."""

    key: str
    value: str = Field(
        description=(
            "The value in force. Always empty for a `secret`, which is stored but never "
            "read back — `is_default` is how you tell whether one is set."
        )
    )
    kind: Literal["string", "int", "float", "bool", "text", "enum", "secret"] = Field(
        description=(
            "How to render and coerce the value. `secret` is write-only: send a new value "
            "to replace it, an empty string to clear it, and leave the key out to keep it."
        )
    )
    options: list[str] | None = None
    group: str
    label: str
    description: str
    default_value: str
    is_default: bool = Field(description="True while the value still comes from the environment")


class ApiTokenInfo(ORMModel):
    id: int
    label: str
    scopes: list[str]
    created_by: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    prefix: str | None = None
    suffix: str = Field(default="", description="Last four characters, to tell tokens apart")
    used_by_recorders: list[str] = Field(
        default_factory=list,
        description=(
            "Recorders that first reported holding this token (`recorders.token_id`). A report, "
            "not a permission — any `upload` token may poll for any recorder — but it is what "
            "says whether revoking this one takes a recorder off the air."
        ),
    )


class ApiTokenCreated(ApiTokenInfo):
    token: str = Field(description="The raw token — shown exactly once, never stored")


class WebhookDeliveryOut(ORMModel):
    """One queued or finished outbound webhook (``webhook_deliveries``)."""

    id: int
    event: str = Field(description="call.stored, call.transcribed, link.created, link.deleted")
    subject_id: int | None = Field(
        default=None, description="The call the event is about, when it is about one"
    )
    status: Literal["pending", "delivered", "failed"] = Field(
        description=(
            "`pending` is due or backing off, `delivered` got a 2xx, `failed` is parked "
            "after the attempt limit and is never retried automatically again."
        )
    )
    attempts: int = 0
    response_status: int | None = Field(
        default=None, description="What the receiver last answered with, when it answered"
    )
    last_error: str | None = Field(
        default=None,
        description="Why the last attempt failed, with every URL scrubbed out of it",
    )
    next_at: datetime | None = Field(default=None, description="When the next attempt is due")
    delivered_at: datetime | None = None
    created_at: datetime | None = None
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The body that goes out, frozen when the event happened. A retry re-sends this "
            "rather than a re-read of the row, which is what makes it an event log."
        ),
    )


class WebhookStatusOut(BaseModel):
    """``GET /admin/webhooks`` — the switch, the queue and the last few deliveries."""

    enabled: bool = Field(
        description="A URL is set *and* at least one event is subscribed; either missing is off"
    )
    url_set: bool
    secret_set: bool = Field(
        description="Whether a signing secret is configured. The secret itself is never returned"
    )
    events: list[str] = Field(default_factory=list, description="What is subscribed right now")
    known_events: list[str] = Field(
        default_factory=list, description="Every event name that may be subscribed"
    )
    max_attempts: int = 5
    counts: dict[str, int] = Field(default_factory=dict, description="Queue tallies by status")
    deliveries: list[WebhookDeliveryOut] = Field(default_factory=list)


class ExportLedgerRow(BaseModel):
    """One ``call_exports`` row, flattened for the Admin → Exports table."""

    id: int
    call_id: int
    service: str
    status: str = Field(description="`pending`, `uploaded`, `skipped` or `failed`")
    attempts: int = 0
    last_error: str | None = None
    skip_reason: str | None = None
    remote_id: str | None = None
    response: str | None = Field(
        default=None,
        description=(
            "What the service answered on the last attempt, as one line — Broadcastify "
            "Calls' stage-1 reply (`0 <upload id>`, `1 SKIPPED…`, `1 REJECTED…`, "
            "`100 NO-API-KEY-SPECIFIED`) or an HTTP status line. `null` on rows written "
            "before this was recorded, and on attempts that never reached an answer"
        ),
    )
    response_code: int | None = Field(
        default=None, description="The integer leading that line: `0`, `1` or `100`"
    )
    talkgroup: int | None = Field(default=None, description="Talkgroup number on the air")
    talkgroup_alias: str | None = None
    started_at: datetime | None = Field(default=None, description="When the call started")
    next_attempt_at: datetime | None = None
    uploaded_at: datetime | None = None
    updated_at: datetime | None = None


class ExportServiceStatus(BaseModel):
    """One outbound service: its switch, its configuration, and how the queue is doing."""

    service: str = Field(description="`broadcastify` or `openmhz`")
    enabled: bool = Field(description="The runtime switch. Both ship **off**")
    configured: bool = Field(description="Whether the credentials this client needs are present")
    enabled_at: datetime | None = Field(
        default=None,
        description="The enable watermark: nothing that started before this is ever uploaded",
    )
    talkgroup_mode: str = Field(
        default="all",
        description=(
            "`all` (ignore the per-talkgroup checkbox), `allow` (only ticked), "
            "`deny` (all but cleared)"
        ),
    )
    systems: list[int] = Field(
        default_factory=list, description="Keyup system ids selected; empty means every system"
    )
    counts: dict[str, int] = Field(default_factory=dict, description="Ledger tallies by status")
    counts_24h: dict[str, int] = Field(
        default_factory=dict, description="The same tallies over rows touched in the last 24 hours"
    )
    uploaded: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    duplicates: int = Field(
        default=0,
        description=(
            "All-time rows the service answered as a call it already has "
            "(`1 SKIPPED---ALREADY-RECEIVED-THIS-CALL`). Not a failure and not a decision "
            "taken here: somebody else's uploader, or an earlier run of this one, got "
            "there first"
        ),
    )
    duplicates_24h: int = Field(default=0, description="The same, over the last 24 hours")
    duplicate_uploads_24h: int = Field(
        default=0,
        description=(
            "Rows from the last 24 hours skipped here because another copy of the same "
            "transmission had already been uploaded (two recorders, and reconciliation "
            "changed which copy is primary after the first was published). Nothing was "
            "sent. Distinct from `duplicates_24h`, which is the service telling us it "
            "already holds a call somebody else sent"
        ),
    )
    late_24h: int = Field(
        default=0,
        description=(
            "Rows from the last 24 hours skipped here because the call was already past "
            "Broadcastify's skew window when its turn came — no request was sent. "
            "Broadcastify Calls only; the Feed and OpenMHz have no skew rule"
        ),
    )
    responses_24h: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "The last 24 hours of stored responses, counted by class: `0 OK`, "
            "`1 SKIPPED`, `1 REJECTED-CALL-SKEW-TOO-LONG`, `100 NO-API-KEY-SPECIFIED`, "
            "else the code and the first word. The per-call tail (the upload id, the skew "
            "in seconds) is stripped so the histogram has one entry per kind of answer"
        ),
    )
    upload_at: str = Field(
        default="keyup",
        description=(
            "Who POSTs the call: `keyup` (this host, after ingest) or `recorder` (each "
            "trunkd recorder, at call end, with keyup recording the outcome). "
            "`broadcastify_upload_at`; Broadcastify Calls only"
        ),
    )
    by_recorder_24h: int = Field(
        default=0,
        description=(
            "Uploaded rows from the last 24 hours a **recorder** sent and reported on the "
            "call push, rather than this host. The rest of `counts_24h.uploaded` is keyup's"
        ),
    )
    claimed_24h: int = Field(
        default=0,
        description=(
            "Rows from the last 24 hours skipped because another uploader had claimed the "
            "call first — no request was sent. Normal while `upload_at` is `recorder`: it "
            "is keyup standing down for the recorder that got there first"
        ),
    )
    queue_depth: int = Field(default=0, description="Pending rows waiting for this service")
    last_upload_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    environment: str | None = Field(
        default=None,
        description=(
            "Broadcastify Calls only: `production` or `development`. Broadcastify issues "
            "the two sets of credentials separately and each endpoint refuses the other's, "
            "so this picks the URL, the API key and the system id together"
        ),
    )
    status_url: str | None = Field(
        default=None, description="The service's own monitoring page for this environment"
    )
    manage_url: str | None = Field(
        default=None, description="The node-management page, where the environment has one"
    )
    encoding: str | None = Field(
        default=None, description="Broadcastify Calls only: `m4a` (AAC) or `mp3`"
    )
    clock_offset_s: float | None = Field(
        default=None,
        description=(
            "How far this host's clock is from its reference, in seconds; positive means "
            "ahead. `null` when it could not be measured, which is not the same as bad. "
            "Step 1 of the Broadcastify Calls workflow requires NTP accuracy because `ts` "
            "is part of the tuple they deduplicate on"
        ),
    )
    clock_ok: bool | None = Field(
        default=None,
        description="`|offset| <= 2 s`. `null` when the offset could not be measured",
    )
    clock_source: str | None = Field(
        default=None,
        description="`ntp` (an SNTP query), `http` (a response's Date header), or `null`",
    )


class BroadcastifyFeedStatus(BaseModel):
    """The live Icecast stream — a different Broadcastify product from Calls above.

    Not a ledger: a feed has no per-call outcome to record and nothing to retry, so what
    it reports is the state of one socket. The worker process holds that socket and this
    endpoint is answered by the API process, so the numbers come from the status row the
    worker writes; one that has not been written for 90 s reads as disconnected.
    """

    enabled: bool = Field(default=False, description="The runtime switch. It ships **off**")
    configured: bool = Field(
        default=False, description="Whether the Server, Mount and Password are all set"
    )
    connected: bool = Field(default=False, description="Whether the mount currently holds our source")
    since: datetime | None = Field(default=None, description="When the current connection opened")
    enabled_at: datetime | None = Field(
        default=None, description="The feed's own enable watermark; nothing older is ever played"
    )
    server: str = Field(default="", description="Broadcastify's **Server** field")
    port: int = Field(default=80, description="Broadcastify's **Port** field")
    mount: str = Field(default="", description="Broadcastify's **Mount** field")
    username: str = Field(default="source", description="Broadcastify's **Username** field")
    bitrate_kbps: int = 16
    sample_rate: int = 22_050
    max_backlog_s: float = 120.0
    silence: bool = True
    bytes_sent: int = Field(default=0, description="Bytes written on the current connection")
    calls_played: int = 0
    dropped: int = Field(default=0, description="Queued calls dropped to stay near live")
    queued_s: float = Field(default=0.0, description="Seconds of audio waiting to play")
    last_error: str | None = None
    last_error_at: datetime | None = None


class TrunkedSystem(BaseModel):
    """One system Broadcastify Calls is allowed to publish, for the Exports page."""

    id: int
    name: str


class ExportStatusOut(BaseModel):
    """``GET /admin/exports`` — both services, plus the last few ledger rows."""

    services: list[ExportServiceStatus] = Field(default_factory=list)
    max_attempts: int = 5
    recent: list[ExportLedgerRow] = Field(default_factory=list)
    broadcastify_feed: BroadcastifyFeedStatus = Field(
        default_factory=lambda: BroadcastifyFeedStatus(),
        description="The Broadcastify **Feed** (live audio stream), which has no ledger",
    )
    trunked_systems: list[TrunkedSystem] = Field(
        default_factory=list,
        description=(
            "Every system Broadcastify **Calls** may publish — the trunked ones "
            "(`systems.is_trunked`). An empty list means the Calls uploader has nothing it "
            "could ever send, whatever its switch says, and the page offers the switch "
            "disabled rather than letting it be turned on against a conventional archive."
        ),
    )


class ExportTestResult(BaseModel):
    """``POST /admin/exports/{service}/test`` — what could be checked without publishing.

    `checked` says in prose exactly what was proven; read it before reading `ok`. Nothing
    is ever uploaded here. Broadcastify Calls does have a real credential check — the
    documented `test=1` parameter alongside `apiKey` and `systemId` — and it registers no
    call and returns no upload URL; OpenMHz has none, and its public `/{shortName}/calls`
    read resolves the short name only.
    """

    service: str
    ok: bool
    checked: str = Field(description="What this test was actually able to verify")
    detail: str = ""
    environment: str | None = Field(
        default=None, description="Which credential set was tested: `production` or `development`"
    )
    clock_offset_s: float | None = Field(
        default=None,
        description=(
            "Host clock minus its reference, seconds; positive means ahead. `null` if unmeasured"
        ),
    )
    clock_ok: bool | None = Field(
        default=None, description="`|offset| <= 2 s`; `null` when it could not be measured"
    )
    clock_source: str | None = Field(default=None, description="`ntp`, `http`, or `null`")
    used_draft: bool = Field(
        default=False,
        description=(
            "True when the test ran against **unsaved** values sent in the request body "
            "rather than what is stored. Nothing sent that way is ever persisted."
        ),
    )
    draft_keys: list[str] = Field(
        default_factory=list,
        description="Which setting keys the draft supplied, in the order they were applied",
    )


class ExportTestRequest(BaseModel):
    """Optional body of the two test endpoints: the card's **unsaved** values.

    The test button is pressed while filling the form in, so testing what is *stored*
    answers a question nobody asked — "is the key I have not saved yet correct" is the
    whole point of pressing it. `overrides` is that draft: keys of `SETTINGS_SPEC`,
    restricted to the service being tested, coerced exactly as a save would coerce them,
    layered over the effective settings **for this request only** and never written.
    """

    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "`{setting_key: value}` for keys belonging to this service. A key from another "
            "service, or one that is not a runtime setting at all, is a 422 — a test must "
            "not be a way to smuggle a value past the settings API. Secrets are accepted "
            "(that is what is being tested); an empty secret means 'use the stored one'."
        ),
    )


class ApiTokenIn(BaseModel):
    label: str
    scopes: list[ScopeName] = Field(default_factory=lambda: ["read"])
    expires_at: datetime | None = None


class UserOut(ORMModel):
    id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    is_admin: bool = False
    is_active: bool = True
    created_at: datetime | None = None
    last_login_at: datetime | None = None


class UserIn(BaseModel):
    username: str
    password: str | None = None
    display_name: str | None = None
    email: str | None = None
    scopes: list[ScopeName] | None = None
    is_admin: bool = False
    is_active: bool = True


class UserPatch(BaseModel):
    password: str | None = None
    display_name: str | None = None
    email: str | None = None
    scopes: list[ScopeName] | None = None
    is_admin: bool | None = None
    is_active: bool | None = None


class LoginRequest(BaseModel):
    username: str = Field(examples=["admin"])
    password: str


class MeResponse(BaseModel):
    user: UserOut | None = None
    scopes: list[str] = Field(default_factory=list)
    auth_kind: str | None = Field(default=None, description="'session', 'token' or null")
    auth_required_read: bool = Field(description="False while reads are open to anonymous visitors")


class DiskStatusOut(BaseModel):
    """Free space on one filesystem the app writes to (app/disk_watch.py)."""

    path: str = Field(description="The directory measured — the first watched path on that filesystem")
    free_bytes: int
    total_bytes: int
    free_percent: float
    level: Literal["ok", "warn", "critical"] = Field(
        description=(
            "`warn` under `disk_warn_free_percent` or `disk_warn_free_gb`, `critical` under the "
            "critical pair — whichever of each pair trips first"
        )
    )
    growth_bytes_per_day: float | None = Field(
        default=None,
        description="How fast free space is shrinking, from the last 24 h of five-minute samples",
    )
    days_to_full: float | None = Field(
        default=None,
        description=(
            "free_bytes ÷ growth — null when free space is not shrinking or there is under an "
            "hour of samples"
        ),
    )


class HealthConditionOut(BaseModel):
    """One health condition the worker currently holds raised (app/health.py)."""

    key: str = Field(description="The subject: the check, plus `:<instance_id>` for a recorder")
    check: Literal["recorder_silent", "ingest_lag", "stt_backlog", "backup_stale"]
    raised_at: datetime | None = None
    checked_at: datetime | None = None
    value: float | None = Field(
        default=None, description="The last measurement, in minutes (hours for backups)"
    )
    message: str | None = None


class SystemStatusOut(BaseModel):
    status: Literal["ok", "degraded", "down"]
    ok: bool
    version: str
    db_ok: bool
    database: dict[str, Any] = Field(default_factory=dict)
    audio_root: str
    tz: str
    counts: dict[str, int] = Field(default_factory=dict)
    queue_depth: int = 0
    #: The upload admission gauge (app/ingest/flow.py):
    #: ``{live, backlog, audio_seconds, eta_seconds, accepting: {live, backlog},
    #: paused_backlog}``.  Empty when the database is unreachable.
    queue: dict[str, Any] = Field(default_factory=dict)
    disk_free_bytes: int | None = None
    disk_total_bytes: int | None = None
    disk: DiskStatusOut | None = Field(
        default=None, description="Free space under `AUDIO_ROOT`, its level and the days-to-full estimate"
    )
    disk_volumes: list[DiskStatusOut] = Field(
        default_factory=list,
        description="Every filesystem the worker watches (audio root, archive paths, DISK_WATCH_PATHS)",
    )
    uptime_s: float = 0.0
    ingest: IngestStatusOut | None = None
    health: list[HealthConditionOut] = Field(
        default_factory=list,
        description=(
            "The health conditions raised right now — a hot recorder silent, no new calls, "
            "transcription stalled, the database backup overdue — as the worker's once-a-minute "
            "check last judged them. Empty when all is well."
        ),
    )
    server_time: datetime | None = None


class IngestLagOut(BaseModel):
    """``GET /api/status/lag`` — how stale the archive is, and nothing else."""

    ingest_lag_s: float | None = Field(
        default=None, description="Seconds since the newest call was recorded; null on an empty archive"
    )
    last_recorded_at: datetime | None = None
    server_time: datetime


class AlertRuleOut(ORMModel):
    id: int
    name: str
    kind: AlertKindName
    pattern: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    talkgroup_ids: list[int] = Field(default_factory=list)
    radio_ids: list[int] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    channel_config: dict[str, Any] = Field(default_factory=dict)
    target: str = ""
    cooldown_s: int = 300
    enabled: bool = True
    last_fired_at: datetime | None = None
    fire_count: int = 0
    created_by: str | None = None
    created_at: datetime | None = None


class AlertRuleIn(BaseModel):
    name: str
    kind: AlertKindName
    pattern: str | None = None
    filters: dict[str, Any] | None = None
    talkgroup_ids: list[int] | None = None
    radio_ids: list[int] | None = None
    channels: list[str] | None = None
    channel_config: dict[str, Any] | None = None
    target: str | None = None
    cooldown_s: int | None = None
    enabled: bool | None = None


class NotificationOut(ORMModel):
    id: int
    rule_id: int | None = None
    rule_name: str | None = None
    call_id: int | None = None
    channel: str
    status: Literal["sent", "failed", "skipped"] = "sent"
    severity: str | None = None
    subject: str | None = None
    error: str | None = None
    excerpt: str | None = None
    created_at: datetime | None = None


class ExportJobOut(ORMModel):
    """``POST /exports`` — an async zip of audio plus metadata for a filter."""

    id: str
    status: JobStatusName
    progress: float = Field(default=0.0, description="0..1")
    call_count: int = 0
    bytes: int | None = None
    download_url: str | None = None
    error: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


class ImportResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    source: str | None = None
    aliases: int = 0
    skipped_service_tags: int = Field(
        default=0,
        description=(
            "CSV rows whose `service_tag`/`tag` cell named no RadioReference tag. "
            "The rest of the row was imported; only the tag was dropped."
        ),
    )
    skipped_agency_codes: int = Field(
        default=0,
        description=(
            "CSV rows whose `agency_code` cell named no agency in this deployment (see "
            "`GET /agencies`). Dropped the same way, and for a harder reason: the column "
            "is a foreign key, so writing one would abort the whole import at that row."
        ),
    )


# --------------------------------------------------------------------------------------
# site channel audit (§3.5, docs/CHANNEL_AUDIT.md)
# --------------------------------------------------------------------------------------

#: the longest an ``instance_id`` / ``tuner_key`` a recorder may send, and the most tuners
#: it may report in one status push — the contract trunkd implements against.  A recorder
#: exceeding these gets a 422 rather than being allowed to grow keyup's storage unbounded.
#:
#: They live up here rather than beside the tuner models because the *observation* and
#: *radio event* pushes below name the same ``instance_id``, and one recorder identity
#: should be one constant however many endpoints it is spelled on.
MAX_INSTANCE_ID_LEN = 128
MAX_TUNER_KEY_LEN = 64
MAX_TUNERS_PER_REPORT = 64


class ObservationSystemIn(BaseModel):
    """The system the recorder is decoding, as its control channel identifies it."""

    label: str = Field(description="The system name the recorder uses", examples=["Example System"])
    wacn: int | None = Field(default=None, examples=[101010])
    system_id: int | None = Field(default=None, description="P25 system id", examples=[52])
    nac: int | None = Field(default=None, description="Network access code", examples=[52])


class ObservationSiteIn(BaseModel):
    """The site, from the RFSS/Network Status broadcast."""

    label: str | None = Field(default=None, examples=["Central"])
    rfss: int | None = None
    site: int | None = Field(default=None, description="Site number inside the RFSS")
    lra: int | None = None


class ObservationChannelIn(BaseModel):
    """A control channel the site is using or announcing as a backup."""

    #: ``None`` when the recorder knows the channel number but not the band it sits in, so
    #: it cannot say the frequency yet — a site can announce a secondary control channel
    #: before the IDEN_UP for its band has been heard.  One such entry used to fail the
    #: whole five-minute report with a 422; the consumer (app.rr.observations) skips it.
    frequency_hz: int | None = Field(default=None, examples=[851050000])
    channel: str | None = Field(default=None, description="The recorder's channel label", examples=["1-3"])
    tsbk_rate: float | None = Field(default=None, description="Trunking control blocks per second")
    service_class: list[str] | None = None


class ObservationTrafficIn(BaseModel):
    """A traffic channel the site granted during the window."""

    frequency_hz: int | None = None
    channel: str | None = None
    grants: int = 0
    calls: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class ObservationBandIn(BaseModel):
    """One IDEN band-plan entry, stored with the document and not normalized."""

    id: int | None = None
    base_hz: int | None = None
    spacing_hz: int | None = None
    offset_hz: int | None = None
    bandwidth_hz: int | None = None
    tdma: bool | None = None


class ObservationAdjacentIn(BaseModel):
    """A neighbor from an Adjacent Status Broadcast."""

    system_id: int | None = None
    rfss: int | None = None
    site: int | None = None
    lra: int | None = None
    control_frequency_hz: int | None = None
    channel: str | None = None
    flags: dict[str, Any] | None = None


class ObservationServicesIn(BaseModel):
    """The SYS_SVC_BCAST bitmap as the recorder decodes it: which services the site says it
    offers and which it supports, as names and as the raw 24-bit hex, plus the request
    priority.  It was sent from the first day the recorder decoded the message and dropped
    here for want of a field — ``null`` in 492 windows across three sites before this."""

    model_config = ConfigDict(extra="ignore")

    available: list[str] = Field(default_factory=list)
    supported: list[str] = Field(default_factory=list)
    available_raw: str | None = None
    supported_raw: str | None = None
    request_priority: int | None = None


class ObservationExemplarIn(BaseModel):
    """One vendor message the recorder counted but could not name.

    25 % of every TSBK on one L3Harris control channel is one of two L3Harris messages that
    never change a byte (docs/NON-VOICE-DATA.md §3.1).  Storing 23 400 identical rows an
    hour would be absurd; one exemplar per window is enough to notice the day they change.
    """

    mfid: int = Field(examples=[164])
    opcode: int = Field(examples=[15])
    count: int = 0
    exemplar: str | None = Field(default=None, description="Hex of one message body")


class RecorderObservationIn(BaseModel):
    """One window of what a recorder saw of one site (``POST /recorder/observations``).

    Only ``recorder``, ``system`` and ``observed_at`` are required; a recorder that can
    only report its control channel posts those three and ``control``.  A document without
    ``site.label`` is stored verbatim and not normalized — every audit row hangs off a site.
    """

    recorder: str = Field(description="Which recorder is speaking", examples=["trunkd"])
    recorder_version: str | None = None
    instance: str | None = Field(
        default=None,
        max_length=MAX_INSTANCE_ID_LEN,
        description=(
            "Which *instance* is speaking — the recorder's `instance_id`, the same identity "
            "`/recorder/tuners/status` uses. `recorder` names the software; this names the "
            "machine, and two boxes watching one site are told apart by nothing else. "
            "Omitted by an older recorder, which is stored as null."
        ),
        examples=["trunkd-garage"],
    )
    observed_at: datetime = Field(description="End of the observation window, ISO-8601 UTC")
    window_s: int | None = Field(default=None, description="Length of the window in seconds")
    system: ObservationSystemIn
    site: ObservationSiteIn | None = None
    control: ObservationChannelIn | None = Field(default=None, description="The live control channel")
    services: ObservationServicesIn | None = Field(
        default=None, description="The site's SYS_SVC_BCAST service bitmap, when decoded"
    )
    secondary_control: list[ObservationChannelIn] = Field(
        default_factory=list, description="Secondary Control Channel Broadcast entries"
    )
    configured_control: list[int] = Field(
        default_factory=list, description="Control frequencies the recorder is configured to hunt"
    )
    bands: list[ObservationBandIn] = Field(default_factory=list)
    traffic: list[ObservationTrafficIn] = Field(default_factory=list)
    adjacent_sites: list[ObservationAdjacentIn] = Field(default_factory=list)
    # The message census (docs/NON-VOICE-DATA.md §5, "store the histogram, never the
    # firehose").  All five are stored verbatim in ``recorder_observations.payload`` and
    # normalized nowhere: they are 20-odd numbers per window whose whole purpose is to make
    # "a new opcode appeared" answerable later.  `GET /api/sites/{id}/message-mix`
    # aggregates them.
    message_mix: dict[str, int] | None = Field(
        default=None,
        description="TSBK census for the window, keyed `mfid:opcode` (or `mfid/opcode`)",
        examples=[{"0:2": 695, "164:15": 488, "0:44": 12}],
    )
    unknown_exemplars: list[ObservationExemplarIn] = Field(
        default_factory=list, description="One sample of each message the decoder could not name"
    )
    link_control_mix: dict[str, int] | None = Field(
        default=None, description="Traffic-channel Link Control census, keyed `mfid:lco`"
    )
    data_channel: dict[str, Any] | None = Field(
        default=None,
        description="SNDCP activity in the window: grants, pages, PDU counts, channels",
        examples=[{"grants": 19, "pages": 2, "pdus": 3, "channels": [858987500]}],
    )
    lsd_nonzero: int | None = Field(
        default=None, description="Low-speed-data octet pairs that were not all zero"
    )


class RecorderObservationAccepted(BaseModel):
    """What the observation endpoint answers: what it stored and what was new."""

    ok: bool = True
    observation_id: int
    system_id: int | None = None
    site_id: int | None = None
    frequencies_upserted: int = 0
    frequencies_new: int = 0
    adjacent_upserted: int = 0
    adjacent_new: int = 0
    alerts: int = Field(
        default=0,
        description=(
            "Notifications this document raised — `channel_audit` findings plus `new_kind` / "
            "`exemplar_changed` ones from the message census"
        ),
    )
    note: str | None = None


class ChannelAuditSite(ORMModel):
    """The site the report is about, with the identity the air gave it."""

    id: int
    system_id: int
    system: str | None = None
    name: str
    site_number: int | None = None
    description: str | None = None
    nac: int | None = None
    rfss: int | None = None
    lra: int | None = None
    rr_site_id: int | None = None


class SiteFrequencyOut(ORMModel):
    """One catalogued frequency at one site, as one source describes it.

    A site has a row per ``(frequency, source)`` — what the air said, what RadioReference
    documents, what the recorder is configured to hunt — so the same frequency can appear
    more than once with different roles.
    """

    id: int
    site_id: int
    frequency_hz: int
    role: SiteFrequencyRoleName
    source: SiteFrequencySourceName
    channel_ref: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    observation_count: int = 0
    grant_count: int = 0
    export_broadcastify: bool = Field(
        default=True,
        description=(
            "Whether *conventional* calls heard on this frequency — calls with no talkgroup "
            "— may be published to Broadcastify Calls. Read only when "
            "`export_broadcastify_talkgroup_mode` is `allow` or `deny`; ignored in `all`."
        ),
    )
    export_openmhz: bool = Field(
        default=True, description="The same, for OpenMHz, under `export_openmhz_talkgroup_mode`."
    )


class SiteFrequencyPatch(BaseModel):
    """Body of ``PATCH /site-frequencies/{id}`` — only the export checkboxes are editable.

    Everything else on the row is *observed*: the frequency, its role and its counters are
    what the air, RadioReference or the recorder config said, and editing them by hand would
    only make the channel audit lie.
    """

    export_broadcastify: bool | None = None
    export_openmhz: bool | None = None


class ChannelAuditControl(ORMModel):
    """The control-channel row: what is live, what is documented, what is configured."""

    observed: int | None = Field(default=None, description="The control channel heard now")
    rr_primary: list[int] = Field(default_factory=list, description="RadioReference `use='d'`")
    rr_alternate: list[int] = Field(default_factory=list, description="RadioReference `use='a'`")
    observed_alternate: list[int] = Field(
        default_factory=list, description="Secondary control channels the site announces"
    )
    configured: list[int] = Field(default_factory=list, description="What the recorder hunts")


class ChannelAuditUndocumented(ORMModel):
    """Heard on the air, absent from RadioReference."""

    frequency_hz: int
    role: SiteFrequencyRoleName
    channel_ref: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    grant_count: int = 0
    observation_count: int = 0


class ChannelAuditMissing(ORMModel):
    """Documented by RadioReference, never heard or not heard lately."""

    frequency_hz: int
    rr_role: SiteFrequencyRoleName
    channel_ref: str | None = None
    last_seen_at: datetime | None = Field(
        default=None, description="Last time the air carried it; null means never"
    )


class ChannelAuditDrift(ORMModel):
    """A frequency both sources know and disagree about."""

    frequency_hz: int
    rr_role: SiteFrequencyRoleName
    observed_role: SiteFrequencyRoleName


class ChannelAuditRecorderConfig(ORMModel):
    """The gap between what the site announces and what the recorder is told to hunt."""

    announced_not_configured: list[int] = Field(default_factory=list)
    configured_not_announced: list[int] = Field(default_factory=list)
    suggested_control_frequencies: list[int] = Field(
        default_factory=list, description="Paste into the recorder's control frequency list; live first"
    )


class ChannelAuditAdjacent(ORMModel):
    """A neighbor the control channel announces, resolved against the directory."""

    system_id_p25: int
    rfss: int
    site_number: int
    lra: int | None = None
    control_frequency_hz: int | None = None
    channel_ref: str | None = None
    flags: dict[str, Any] | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    known_in_rr: bool = Field(
        default=False, description="Resolves to a site in the RadioReference site cache"
    )
    known_site_id: int | None = Field(
        default=None, description="The Keyup site the recorder also hears, if any"
    )
    known_site_name: str | None = Field(
        default=None, description="The RadioReference name for the tower (or the Keyup site's)"
    )
    known_county: str | None = Field(
        default=None, description="The county RadioReference lists for the tower, if any"
    )
    match: str | None = Field(
        default=None,
        description="Which key resolved it against RR: rfss+site_number | control_frequency | system_id",
    )


class SiteChannelAudit(ORMModel):
    """``GET /directory/sites/{id}/channel-audit`` — computed live, never stored."""

    site: ChannelAuditSite
    rr_synced_at: datetime | None = None
    last_observed_at: datetime | None = None
    stale_days: int = Field(description="`CHANNEL_AUDIT_STALE_DAYS`: how old is 'not heard lately'")
    control: ChannelAuditControl
    undocumented: list[ChannelAuditUndocumented] = Field(default_factory=list)
    missing: list[ChannelAuditMissing] = Field(default_factory=list)
    control_drift: list[ChannelAuditDrift] = Field(default_factory=list)
    recorder_config: ChannelAuditRecorderConfig
    adjacent_sites: list[ChannelAuditAdjacent] = Field(default_factory=list)


class SystemChannelAuditRow(ORMModel):
    """One site's counts in the system-wide summary."""

    site_id: int
    site: str
    last_observed_at: datetime | None = None
    rr_synced_at: datetime | None = None
    control_observed: int | None = None
    undocumented: int = 0
    missing: int = 0
    control_drift: int = 0
    announced_not_configured: int = 0
    adjacent_unknown: int = 0


class SystemChannelAuditSystem(ORMModel):
    id: int
    name: str
    rr_system_id: int | None = None
    rr_synced_at: datetime | None = None


class SystemChannelAudit(ORMModel):
    """``GET /directory/systems/{id}/channel-audit`` — one row per site."""

    system: SystemChannelAuditSystem
    sites: list[SystemChannelAuditRow] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)


class RadioReferenceIdIn(BaseModel):
    """``PUT /admin/systems/{id}/radioreference`` / ``…/sites/{id}/radioreference``."""

    rr_id: int | None = Field(
        default=None, description="RadioReference system (`sid`) or site id; null clears it"
    )


class RadioReferenceSyncResult(BaseModel):
    """One system's RadioReference sync."""

    system_id: int
    system: str
    rr_system_id: int | None = None
    sites_seen: int = 0
    sites_matched: int = 0
    rr_sites_cached: int = 0
    frequencies: int = 0
    frequencies_removed: int = 0
    talkgroups_named: int = 0
    talkgroups_tagged: int = Field(
        default=0, description="Talkgroups that gained a RadioReference service tag"
    )
    skipped: str | None = None
    unmatched: list[str] = Field(default_factory=list)


class RadioReferenceSyncOut(BaseModel):
    """``POST /admin/radioreference/sync``."""

    enabled: bool = Field(description="False when RR_APP_KEY/RR_USERNAME/RR_PASSWORD are unset")
    synced: list[RadioReferenceSyncResult] = Field(default_factory=list)
    detail: str | None = None



# --------------------------------------------------------------------------------------
# radio events (§3.6, docs/RADIO_EVENTS.md) — presence from the control channel
# --------------------------------------------------------------------------------------


class RadioEventIn(BaseModel):
    """One decoded control-channel message about a radio.

    Every field is optional and every field is lenient, which is deliberate and is the one
    unusual thing about this model.  A recorder posts a thousand of these at a time and
    cannot inspect the archive's reply per event; if pydantic refused the *batch* because
    one event carried a timestamp its clock produced badly, the recorder's only recourse
    would be to drop 999 good events or to retry the same bad batch for ever.  So the
    validation happens in the fold instead (:mod:`app.ingest.events`), which counts what it
    could not use and stores the rest — see ``rejected`` on the response.

    The messages differ from each other too: a de-registration names a radio and no
    talkgroup, an affiliation names both, an SNDCP grant names a frequency.  A ``kind`` the
    archive does not know is stored as ``other`` with the opcode kept.
    """

    model_config = ConfigDict(coerce_numbers_to_str=False)

    at: datetime | str | None = Field(
        default=None, description="When the message was decoded, ISO-8601 UTC; required in practice"
    )
    kind: str = Field(
        default="other", description="One of the `radio_event_kind` values; anything else becomes `other`"
    )
    opcode: int | None = Field(default=None, description="The TSBK/LC opcode", examples=[44])
    mfid: int | None = Field(default=None, description="Manufacturer id; 0 is standard", examples=[0])
    radio_id: int | None = Field(default=None, description="The unit the message is about")
    radio_wacn: int | None = Field(default=None, description="From LC 9 / a fully qualified id")
    radio_system: int | None = Field(default=None, description="The unit's home P25 system id")
    target_radio_id: int | None = Field(default=None, description="Unit-to-unit or query target")
    talkgroup: int | None = Field(default=None, description="The tgid on the air", examples=[1101])
    result: str | None = Field(default=None, examples=["accept", "deny", "refused", "fail"])
    frequency_hz: int | None = Field(default=None, description="For a grant")
    meta: dict[str, Any] | None = Field(default=None, description="Whatever else was decoded")

    @field_validator(
        "opcode",
        "mfid",
        "radio_id",
        "radio_wacn",
        "radio_system",
        "target_radio_id",
        "talkgroup",
        "frequency_hz",
        mode="before",
    )
    @classmethod
    def _tolerate_junk(cls, value: Any) -> Any:
        """An unusable number becomes ``None`` rather than a 422 for the whole batch."""
        if value is None or isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class RecorderEventsIn(BaseModel):
    """A batch of radio events from one recorder watching one site.

    The site is named once for the whole batch, exactly as an observation names it, so a
    thousand events cost one system/site resolution.  Batches are capped at 1 000 events.
    """

    recorder: str = Field(description="Which recorder is speaking", examples=["trunkd"])
    recorder_version: str | None = None
    instance: str | None = Field(
        default=None,
        max_length=MAX_INSTANCE_ID_LEN,
        description=(
            "Which *instance* decoded these — the recorder's `instance_id`. Stamped on every "
            "row in the batch, so a site heard by two boxes can be read one box at a time. "
            "Omitted by an older recorder, which is stored as null."
        ),
        examples=["trunkd-garage"],
    )
    system: ObservationSystemIn
    site: ObservationSiteIn | None = None
    events: list[RadioEventIn] = Field(default_factory=list, max_length=1000)


class RecorderEventsAccepted(BaseModel):
    """What the batch did.

    ``stored`` counts rows written, ``duplicates`` events the archive already had (the
    recorder retries spooled batches, and a retry is free), ``rejected`` events that could
    not be stored at all — a missing timestamp, an unusable field.  One bad event never
    costs the batch: the counts are the report.
    """

    ok: bool = True
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0
    system_id: int | None = None
    site_id: int | None = None
    radios_touched: int = 0
    affiliations_touched: int = 0
    errors: list[str] = Field(default_factory=list, description="At most five, for the recorder's log")


class RadioEventsSummaryKind(BaseModel):
    kind: RadioEventKindName
    count: int


class RadioEventsHourPoint(BaseModel):
    """One hour bucket of the summary window, keyed by the hour it starts."""

    at: datetime
    count: int


class RadioEventsSummaryRadio(BaseModel):
    radio_pk: int | None = None
    radio_id: int | None = None
    alias: str | None = None
    unit_id: str | None = None
    count: int


class RadioEventsSummary(BaseModel):
    """``GET /api/sites/{id}/radio-events/summary`` — is the site busy, and with whom."""

    site_id: int
    site_name: str | None = None
    instance: str | None = Field(
        default=None, description="The recorder instance this was narrowed to, when one was named"
    )
    hours: int
    since: datetime
    total: int = 0
    by_kind: list[RadioEventsSummaryKind] = Field(default_factory=list)
    by_result: list[LabelCount] = Field(default_factory=list)
    top_radios: list[RadioEventsSummaryRadio] = Field(default_factory=list)
    registrations_per_hour: list[RadioEventsHourPoint] = Field(
        default_factory=list,
        description="`unit_registration` rows per hour bucket of the window, oldest first",
    )


class MessageMixEntry(BaseModel):
    """One `mfid:opcode` across the window."""

    key: str = Field(examples=["164:15"])
    mfid: int
    opcode: int
    count: int
    windows: int = Field(default=0, description="Observation windows this message appeared in")
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    exemplar: str | None = Field(default=None, description="Hex body, when the decoder could not name it")


class SiteMessageMix(BaseModel):
    """``GET /api/sites/{id}/message-mix`` — the control channel's message census.

    Aggregated out of ``recorder_observations.payload`` rather than a table of its own:
    the histogram is the thing worth keeping, and it already arrives with every window.
    """

    site_id: int
    site_name: str | None = None
    instance: str | None = Field(
        default=None, description="The recorder instance this was narrowed to, when one was named"
    )
    days: int
    since: datetime
    windows: int = Field(default=0, description="Observation windows in the range")
    total: int = 0
    messages: list[MessageMixEntry] = Field(default_factory=list)
    link_control: list[MessageMixEntry] = Field(default_factory=list)


class SiteMessageKindOut(BaseModel):
    """One row of ``GET /api/sites/{id}/message-kinds``.

    The census (``/message-mix``) is a rolling window; this is the archive's *memory* of
    what a site has ever transmitted, which is what makes a first sighting an event.
    """

    site_id: int
    key: str = Field(examples=["164:34"], description="`mfid:opcode`")
    mfid: int
    opcode: int
    count: int = Field(description="Messages of this kind counted across every window, ever")
    first_seen_at: datetime
    last_seen_at: datetime
    exemplar: str | None = Field(default=None, description="Newest hex body, when it cannot be decoded")
    exemplar_previous: str | None = None
    exemplar_changed_at: datetime | None = None
    known: bool = Field(default=False, description="The operator (or the registry) has named it")
    label: str | None = None
    alerted_at: datetime | None = Field(
        default=None, description="When this kind last raised an alert; one per 24 h"
    )


class SiteMessageKindPatch(BaseModel):
    """``PATCH /api/admin/sites/{id}/message-kinds/{mfid}/{opcode}`` — name a message kind."""

    model_config = ConfigDict(extra="forbid")

    known: bool | None = Field(default=None, description="True once it is named and no longer news")
    label: str | None = Field(default=None, max_length=200, description="What it is, in words")


# --------------------------------------------------------------------------------------
# recorder & tuner management (docs/ARCHITECTURE.md §4.8)
#
# The recorder polls ``GET /recorder/desired-state`` and posts ``POST /recorder/tuners/
# status``; an operator reads ``GET /admin/recorders`` and writes one tuner's desired
# state with ``PUT /admin/recorders/{id}/tuners/{key}``.  ``reported`` and ``desired`` are
# free-form JSON objects (they map to JSONB columns) so the recorder and keyup can add
# fields without a migration on either side; the models below name the fields both sides
# rely on today and allow the rest through.
# --------------------------------------------------------------------------------------

#: what a tuner may be doing, from the operator's point of view
TunerRoleName = Literal["auto", "control", "voice", "data"]

#: What a recorder is *for* when several of them hear the same site.  ``hot`` records
#: whenever it is healthy; ``warm`` is a spare kept registered with its tuners disabled,
#: which the arbiter may enable when the hot side goes quiet; ``cold`` is never touched
#: automatically.  See :data:`app.models.RECORDER_STANDBY`.
RecorderStandbyName = Literal["hot", "warm", "cold"]

#: The longest a piece of operator-entered self-description may be — a station name, an
#: antenna, a contact — and the longer allowance for the free-text ``notes`` fields.  The
#: recorder caps these on its own side too; keyup caps them again because a limit only the
#: sender enforces is not a limit.  Over-long is a 422, exactly as an over-long ``serial``
#: or ``tuner_key`` already is: the recorder is sending something outside the contract, and
#: silently truncating it would store a half-sentence that looks like the operator's words.
MAX_DESCRIPTION_LEN = 256
MAX_NOTES_LEN = 1024

#: The ceiling on a lifetime counter.  The recorder sends u64s; anything past 2**63-1 is a
#: wrapped or garbage counter rather than a real total, and is refused rather than stored.
MAX_COUNTER = 2**63 - 1


#: The coarse band labels a tuner's ``bands`` list may name, alongside explicit ``"lo-hi"``
#: Hz ranges.  These are trunkd's own labels (``resolve_band`` in its config); keyup never
#: resolves one to hertz — it passes the string through — and keeps the list only so it can
#: refuse a label trunkd would refuse, before the write rather than after it.
BAND_LABELS = ("vhf", "uhf", "700", "800", "900")

#: The caps trunkd's own ``parse_bands`` enforces.  A list over either of them is refused
#: *whole* on the recorder — it drops the tuner's entire desired entry rather than applying
#: part of a band list — so keyup refuses it at the edit instead, where an operator is
#: standing there to read the message.
MAX_BANDS = 16
MAX_BAND_LEN = 64

#: An explicit ``"lo-hi"`` downlink range in Hz, with the whitespace trunkd tolerates.
_BAND_RANGE_RE = re.compile(r"^([0-9]+)\s*-\s*([0-9]+)$")

# ---- the tuner front-end settings an operator may write (§4.8, docs/RECORDERS.md) -------
#
# Every rule below is trunkd's, restated here.  keyup checks them at the edit rather than
# leaving them to the recorder for the same reason ``bands`` is checked here: trunkd
# *rejects a field and carries on*, silently, on its next poll — the operator sees the value
# they typed sitting in "desired" and nothing happening.  A 422 while they are still looking
# at the form costs one retry; the alternative costs a support conversation.

#: The widest crystal correction worth entertaining.  A real dongle is out by tens of ppm;
#: a hundreds-of-ppm figure is a typo, and applying it walks the tuner off the system.
MAX_PPM = 200.0

#: P25 channel spacing.  Every control candidate and every sample rate is a multiple of it.
CHANNEL_STEP_HZ = 12_500

#: ``sample_rate / 12500`` must be an **even** integer of at least this many channels: the
#: channelizer works in 12.5 kHz units and its polyphase filter needs an even count, and
#: fewer than four channels is not a trunked front end.  So 50 kHz is the floor.
MIN_SAMPLE_RATE_CHANNELS = 4

#: HackRF's front-end steps, from its own API: the LNA moves in 8 dB steps to 40, the
#: baseband VGA in 2 dB steps to 62.  A value off the step is silently rounded by libhackrf,
#: which is exactly the kind of "it saved but did something else" this refuses.
HACKRF_LNA_MAX_DB = 40
HACKRF_LNA_STEP_DB = 8
HACKRF_VGA_MAX_DB = 62
HACKRF_VGA_STEP_DB = 2

#: The most control-channel candidates one tuner may be given, and the most named gain
#: elements or driver settings it may carry.  Caps, not opinions: a list past these is a
#: mistake rather than a configuration.
MAX_CONTROL_CANDIDATES = 32
MAX_GAIN_ELEMENTS = 32
MAX_TUNER_SETTINGS = 64
MAX_SETTING_KEY_LEN = 64
MAX_SETTING_VALUE_LEN = 256

#: Fields that only exist on a Soapy device, and fields that only exist on a HackRF.  A
#: write naming one of the wrong set is refused with the field named: the recorder would
#: drop it, and a silently dropped setting is worse than a refused one.  ``bandwidth_hz``,
#: ``ppm``, ``sample_rate`` and the rest are common to both and appear in neither list.
SOAPY_ONLY_TUNER_FIELDS = ("gain", "gains", "agc", "rx_port", "settings")
HACKRF_ONLY_TUNER_FIELDS = ("lna_db", "vga_db", "amp")

# ---- the recorder-to-recorder link ----------------------------------------------------
#
# The link is a mesh of recorders that hear the same system telling each other what they
# have claimed, so two boxes on one site do not both record the same grant.  Every member
# authenticates with **one shared secret**, which is why the key rules are the strict half
# of what follows: a 16-character floor is not cryptography, it is the line under which a
# key is obviously a person typing "test" and the whole mesh is open to whoever guesses it.

#: The shortest shared key keyup will store.  ``POST .../link/generate-key`` hands out 64
#: hex characters, so this floor only ever catches a hand-typed one.
LINK_MIN_KEY_LEN = 16
LINK_MAX_KEY_LEN = 256
#: A link with more peers than this is a mistake rather than a topology.
LINK_MAX_PEERS = 32
LINK_ADDRESS_MAX_LEN = 255
#: How often a member announces itself, in seconds.  Below a second is a flood and above a
#: minute a peer is called dead before it has said anything.
LINK_HELLO_MIN_S = 1
LINK_HELLO_MAX_S = 60
#: The three grant flags, each an ordinary bool with a default of *off*: a link that is
#: merely up must not start changing what the recorder records until it is told to.
LINK_FLAGS = ("accept_remote_grants", "publish_grants", "defer_to_claims")

#: One RFC 1123 hostname label — letters, digits and inner hyphens.  A bare label ("recorder-a")
#: is as valid as a dotted name, because on a home LAN that is exactly what a peer is called.
_HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_link_address(raw: Any, field: str) -> str:
    """``host:port`` — a hostname, an IPv4 address or a bracketed IPv6 one, and a port.

    Returns the address as it will be stored, whitespace stripped and nothing else changed:
    the recorder binds and dials exactly this string, so keyup lower-casing a hostname or
    unbracketing an address would be keyup rewriting a peer's name.

    The ``field`` is carried into every message (``link.peers[2]``, ``link.bind``) because
    these are typed by hand into a list editor and "which one" is most of the answer.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{field}: must be a host:port string")
    text = raw.strip()
    if not text:
        raise ValueError(f"{field}: must not be empty")
    if len(text) > LINK_ADDRESS_MAX_LEN:
        raise ValueError(f"{field}: longer than {LINK_ADDRESS_MAX_LEN} characters")

    if text.startswith("["):
        # An IPv6 literal has to be bracketed, or its own colons and the port's cannot be
        # told apart — `::1:9000` is a perfectly good address with no port in it at all.
        host, sep, port_text = text.partition("]:")
        if not sep:
            raise ValueError(f"{field}: a bracketed IPv6 address needs a port, as [::1]:9000")
        try:
            ipaddress.IPv6Address(host[1:])
        except ValueError:
            raise ValueError(f"{field}: {host[1:]!r} is not an IPv6 address") from None
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            raise ValueError(f"{field}: must be host:port, as 192.0.2.30:9000")
        if ":" in host:
            raise ValueError(f"{field}: an IPv6 address must be bracketed, as [::1]:9000")
        if not host:
            raise ValueError(f"{field}: must name a host before the port")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            if not all(_HOSTNAME_LABEL_RE.match(label) for label in host.rstrip(".").split(".")):
                raise ValueError(f"{field}: {host!r} is not a hostname or an IP address") from None

    if not port_text.isdigit():
        raise ValueError(f"{field}: {port_text!r} is not a port number")
    port = int(port_text)
    if not (1 <= port <= 65535):
        raise ValueError(f"{field}: the port must be between 1 and 65535")
    return text


def normalize_link(raw: Any, *, existing_key: str | None = None) -> dict[str, Any]:
    """The whole link block an operator wrote, checked and normalized — or a ``ValueError``.

    Every message is prefixed ``link.<field>`` so the route can hand it back as the same
    ``"field: reason"`` 422 the tuner checks produce and the dialog can hang it under the
    input it came from.

    ``key`` is the one field that may be *omitted* on a write: the edit dialog shows the
    secret masked and does not have it unless the operator asked to reveal it, so an
    omitted key means "keep the one already stored".  There has to be one to keep — a link
    with no key at all is refused rather than saved half-configured — and ``existing_key``
    is what the caller has on the row.

    ``peers`` must be non-empty **only when the link is enabled**: a disabled link with the
    bind address and the key already filled in is how an operator stages one before the
    second box exists, and refusing that would make the form impossible to fill in order.
    """
    if not isinstance(raw, dict):
        raise ValueError("link: must be an object, or null to stop managing it")

    unknown = set(raw) - {"enabled", "bind", "peers", "key", "hello_interval_s", *LINK_FLAGS}
    if unknown:
        raise ValueError(f"link: unknown field {sorted(unknown)[0]!r}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("link.enabled: must be true or false")

    bind = normalize_link_address(raw.get("bind"), "link.bind")

    peers_raw = raw.get("peers", [])
    if not isinstance(peers_raw, list):
        raise ValueError("link.peers: must be a list of host:port addresses")
    if len(peers_raw) > LINK_MAX_PEERS:
        raise ValueError(f"link.peers: at most {LINK_MAX_PEERS} peers, got {len(peers_raw)}")
    peers: list[str] = []
    for index, entry in enumerate(peers_raw):
        peer = normalize_link_address(entry, f"link.peers[{index}]")
        if peer in peers:
            # Duplicates are refused rather than deduplicated: a list with the same box in
            # it twice is a typo in one of them, and silently dropping one hides which.
            raise ValueError(f"link.peers[{index}]: {peer} is already in the list")
        peers.append(peer)
    if enabled and not peers:
        raise ValueError("link.peers: an enabled link needs at least one peer")

    key = raw.get("key")
    if key is None or (isinstance(key, str) and not key.strip()):
        if not existing_key:
            raise ValueError(
                f"link.key: a shared key of at least {LINK_MIN_KEY_LEN} characters is required"
            )
        key = existing_key
    if not isinstance(key, str):
        raise ValueError("link.key: must be a string")
    key = key.strip()
    if len(key) < LINK_MIN_KEY_LEN:
        raise ValueError(f"link.key: must be at least {LINK_MIN_KEY_LEN} characters")
    if len(key) > LINK_MAX_KEY_LEN:
        raise ValueError(f"link.key: longer than {LINK_MAX_KEY_LEN} characters")

    hello = raw.get("hello_interval_s", 5)
    if isinstance(hello, bool) or not isinstance(hello, int):
        raise ValueError("link.hello_interval_s: must be a whole number of seconds")
    if not (LINK_HELLO_MIN_S <= hello <= LINK_HELLO_MAX_S):
        raise ValueError(
            f"link.hello_interval_s: must be between {LINK_HELLO_MIN_S} and {LINK_HELLO_MAX_S} seconds"
        )

    block: dict[str, Any] = {
        "enabled": enabled,
        "bind": bind,
        "peers": peers,
        "key": key,
        "hello_interval_s": hello,
    }
    for flag in LINK_FLAGS:
        value = raw.get(flag, False)
        if not isinstance(value, bool):
            raise ValueError(f"link.{flag}: must be true or false")
        block[flag] = value
    return block


def link_key_fingerprint(key: str | None) -> str | None:
    """The first 8 hex of ``sha256(key)`` — enough to see that two peers match, and no more.

    It is what the list view shows in place of the key.  Two recorders whose fingerprints
    differ cannot possibly link, and that is the one question a page can answer without
    ever rendering the secret.
    """
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def public_link(block: Any) -> dict[str, Any] | None:
    """A stored link block with the secret taken out and its fingerprint put in.

    Every route that renders a recorder goes through this, which is the whole leak defence:
    there is one place the key is removed rather than one per response model.
    """
    if not isinstance(block, dict):
        return None
    out = {key: value for key, value in block.items() if key != "key"}
    stored = block.get("key")
    out["key_set"] = bool(stored)
    out["key_fingerprint"] = link_key_fingerprint(stored if isinstance(stored, str) else None)
    return out


def public_broadcastify(block: Any) -> dict[str, Any] | None:
    """A desired-state Broadcastify block with the API key taken out, for an admin read.

    The twin of :func:`public_link`, and it exists for the same reason: the block is
    assembled with the Calls API key in clear because the recorder that has to register
    calls needs it, and exactly one response — the recorder's own desired-state poll —
    may carry it.  Anything that renders a recorder to an operator goes through here
    instead, so the defence is one function rather than a rule every response model has to
    remember.
    """
    if not isinstance(block, dict):
        return None
    out = {key: value for key, value in block.items() if key != "api_key"}
    stored = block.get("api_key")
    out["api_key_set"] = bool(stored)
    out["api_key_fingerprint"] = link_key_fingerprint(stored if isinstance(stored, str) else None)
    return out


def normalize_bands(values: list[str]) -> list[str]:
    """One tuner's ``bands`` list, normalized — or a :class:`ValueError` naming the entry.

    Each entry is either a known label from :data:`BAND_LABELS` (case-insensitive, stored
    lowercase) or an explicit ``"lo-hi"`` range in Hz with ``lo <= hi`` (stored as bare
    digits either side of one hyphen, whatever spacing it arrived with).  Duplicates are
    dropped after normalization, so ``["VHF", "vhf"]`` is one band and not two.

    The error message names the offending index and the value: a band list is typed by
    hand, and "bands[2]" is the difference between a fixable mistake and a rejected form.
    """
    if len(values) > MAX_BANDS:
        raise ValueError(f"at most {MAX_BANDS} bands, got {len(values)}")
    out: list[str] = []
    for index, raw in enumerate(values):
        if len(raw) > MAX_BAND_LEN:
            raise ValueError(f"bands[{index}]: longer than {MAX_BAND_LEN} characters")
        entry = raw.strip().lower()
        if entry in BAND_LABELS:
            band = entry
        elif match := _BAND_RANGE_RE.match(entry):
            lo, hi = int(match.group(1)), int(match.group(2))
            if lo > hi:
                raise ValueError(f"bands[{index}]: {raw!r} runs backwards — lo must not exceed hi")
            band = f"{lo}-{hi}"
        else:
            raise ValueError(
                f"bands[{index}]: {raw!r} is neither a band label "
                f"({', '.join(BAND_LABELS)}) nor a \"lo-hi\" range in Hz"
            )
        if band not in out:
            out.append(band)
    return out


class DesiredTuner(BaseModel):
    """One tuner's desired state as the recorder receives it: its own version plus config.

    ``version`` is that tuner's ``desired_version``.  The recorder applies ``config`` and
    echoes ``version`` back as ``applied_version`` in its next status push; keyup calls the
    tuner *drifted* while the two differ, so the recorder must be told the per-tuner version
    (a recorder-level counter alone cannot tell it which tuner changed).
    """

    version: int = Field(description="This tuner's desired_version; echo it back as applied_version")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="What to tune to: enabled, role, site, center_frequency, gains, spectrum …",
    )


class DesiredBroadcastify(BaseModel):
    """Everything a recorder needs to upload its own calls to Broadcastify Calls (§4.8).

    Sent only while ``broadcastify_upload_at`` is ``recorder``, the Calls switch is on and
    the credentials for the selected environment are present; ``null`` in every other
    case, and ``null`` is the instruction — a recorder that stops being sent this block
    stops uploading and keyup takes the calls back.

    Like ``link`` it is all-or-nothing and, like ``link``, it is one of the two responses
    that carry a credential in clear: it goes to the recorder that has to register with
    it and nowhere else (:func:`public_broadcastify` is what an operator sees).

    ``revision`` rather than a version counter: this block is derived from runtime
    *settings*, which no per-recorder generation number tracks, so the recorder compares
    the hash and re-reads its credentials when it changes.  ``DesiredRecorder.version`` is
    deliberately **not** bumped for it.
    """

    enabled: bool = Field(description="Upload from the recorder. Always true when the block is sent")
    endpoint_url: str = Field(
        description="The call-registration endpoint for the configured environment"
    )
    api_key: str = Field(description="The Calls API key for that environment, in clear")
    system_id: str = Field(description="Broadcastify's numeric system id, as a string")
    encoding: str = Field(
        default="mp3",
        description=(
            "The audio format the recorder uploads in. `mp3` — what a recorder can "
            "reliably encode at call end"
        ),
    )
    talkgroups: list[int] | None = Field(
        default=None,
        description=(
            "The tgids to upload, or `null` for every trunked group call. Resolved here "
            "from `export_broadcastify_talkgroup_mode` and the per-talkgroup checkbox, so "
            "the recorder holds a list and not a rule"
        ),
    )
    skew_limit_s: float = Field(
        default=15.0,
        description="Seconds after a call ends that Broadcastify still accepts it; past it, do not send",
    )
    claim: bool = Field(
        default=True, description="Ask `claim_url` before uploading. Two recorders hear every call"
    )
    claim_url: str = Field(
        default="/api/recorder/broadcastify/claim",
        description="Where to ask, on this host, with the same Bearer token as this poll",
    )
    revision: str = Field(
        description="12 hex characters over the fields above; unchanged means nothing to re-read"
    )


class DesiredSkipTalkgroups(BaseModel):
    """The talkgroups a recorder should not upload at all (§4.8).

    A talkgroup lands here only when *every* use keyup could put its audio to has been
    turned off: ``record`` is false (do not keep the recording), ``transcribe`` is false (do
    not keep the words), and no export service's selection publishes it.  With all three
    false the upload would be a file keyup ingests, stores, and deletes on the next discard
    pass, having learned nothing from it — so the cheapest place to stop it is before it is
    ever sent.

    It is an **exclusion** list rather than a record list on purpose.  A recorder that
    cannot reach keyup, or one running a build that does not know this field, keeps
    uploading everything: the failure mode of a missing list is a full archive, never a
    silent hole in one.

    ``null`` when the list is empty, which is what every deployment ships as.  Like
    :class:`DesiredBroadcastify`, this is derived from the talkgroup table rather than from
    anything stored per recorder, so it carries its own ``revision`` and
    ``DesiredRecorder.version`` is **not** bumped for it.
    """

    tgids: list[int] = Field(
        description=(
            "The tgids to drop at the recorder, ascending. Keyed by tgid alone — a "
            "recorder knows the number it decoded, not which of keyup's systems it "
            "belongs to — so a tgid is listed only when it is skippable everywhere it "
            "appears"
        ),
        examples=[[1055, 1099]],
    )
    revision: str = Field(
        description="12 hex characters of a sha256 over the sorted list; unchanged means nothing to do"
    )


class DesiredRecorder(BaseModel):
    """The desired state of the **recorder itself**, alongside its tuners' (§4.8).

    Its ``version`` is deliberately not the recorder-level ``version`` beside it: that one
    moves whenever any *tuner* is edited, and a counter the recorder echoes back cannot be
    one an unrelated tuner write keeps shifting.  This one is bumped by an operator write of
    any field in *this* block, and the recorder returns it as ``recorder_applied.version``.

    **Every field here is three-valued and null is the important value**: null means keyup
    has no opinion, so the recorder keeps whatever its own TOML or front panel says.  Null is
    the default on all of them, which is what stops a recorder keyup has never been told
    anything about from being reconfigured by silence.

    ``link`` is the odd one only in size: it is the whole recorder-to-recorder block or
    nothing at all, because half a link — peers without the key they authenticate with — is
    not a link.  It is the one place the shared ``key`` is sent in clear, and it goes only
    here, to the recorder that has to hold it.
    """

    version: int = Field(
        description="The recorder-level desired generation; echo it back as recorder_applied.version"
    )
    spool_only: bool | None = Field(
        default=None,
        description=(
            "true: hold uploads in the spool. false: upload. null: keyup has no opinion — "
            "keep the recorder's own config/panel value."
        ),
    )
    event_log_archive: bool | None = Field(
        default=None,
        description=(
            "true: ship the daily raw event log to keyup. false: do not. null: keyup has no "
            "opinion — keep the recorder's own config value."
        ),
    )
    link: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The whole recorder-to-recorder link block to apply — `{enabled, bind, peers[], "
            "key, hello_interval_s, accept_remote_grants, publish_grants, defer_to_claims}` "
            "— or null for 'keyup does not manage the link; keep the configured one'. Sent "
            "as a unit: a peer list is meaningless without the key it authenticates with. "
            "This is the only response that carries the shared `key` in clear, and it goes "
            "only to the recorder that must hold it."
        ),
        examples=[
            {
                "enabled": True,
                "bind": "0.0.0.0:9110",
                "peers": ["recorder-a:9110", "192.0.2.31:9110"],
                "key": "0f3c…",
                "hello_interval_s": 5,
                "accept_remote_grants": True,
                "publish_grants": True,
                "defer_to_claims": True,
            }
        ],
    )
    skip_talkgroups: DesiredSkipTalkgroups | None = Field(
        default=None,
        description=(
            "Talkgroups to drop at the recorder without uploading — the ones with "
            "`record` off, `transcribe` off and no export service selecting them, so "
            "keyup would delete the audio having done nothing with it. `null` when the "
            "list is empty, which is the shipped state. Not covered by `version`: it is "
            "derived from the talkgroup table, so the recorder compares its `revision`. "
            "An older recorder that ignores this field is not wrong, only wasteful — "
            "keyup stores the metadata and deletes the audio at once."
        ),
    )
    broadcastify: DesiredBroadcastify | None = Field(
        default=None,
        description=(
            "Upload this recorder's own calls to Broadcastify Calls, with the credentials "
            "and the talkgroup list to do it with — or null for 'keyup uploads them', "
            "which is the default and what every recorder is sent until an operator sets "
            "`broadcastify_upload_at` to `recorder`. Carries the Calls API key in clear, "
            "like `link` above, and for the same reason. Not covered by `version`: it is "
            "derived from runtime settings, so the recorder compares its `revision`."
        ),
    )


class DesiredStateOut(BaseModel):
    """``GET /recorder/desired-state`` — every tuner's desired configuration for a recorder.

    ``version`` is the recorder-level generation: it changes whenever any tuner's desired
    state is written, so a poller that sees the same value has nothing to do and need not
    look further.  ``tuners`` maps the recorder's own tuner key to that tuner's
    :class:`DesiredTuner` (its own ``version`` and the ``config`` to apply).  ``recorder``
    carries what keyup wants of the *instance* rather than of a tuner — currently the
    upload hold — with its own version to echo.
    """

    version: int = Field(description="Recorder-level generation; unchanged means nothing to apply")
    tuners: dict[str, DesiredTuner] = Field(
        default_factory=dict, description="tuner_key → {version, config}"
    )
    recorder: DesiredRecorder = Field(
        description=(
            "What keyup wants of the recorder itself: "
            "{version, spool_only, event_log_archive, link, broadcastify}"
        )
    )


class BroadcastifyClaimIn(BaseModel):
    """"I am about to upload this call to Broadcastify Calls" (docs/EXPORTERS.md).

    The call is identified the way Broadcastify identifies one — talkgroup and a
    **whole-second** start — because that is the identity the duplicate would be judged
    on, and because at the moment a recorder asks, the call does not exist here yet: the
    audio is still being encoded and the push is one request behind.
    """

    instance_id: str = Field(
        max_length=MAX_INSTANCE_ID_LEN, description="The claiming recorder's instance_id"
    )
    tgid: int = Field(description="The talkgroup the call was on")
    ts: int = Field(description="Call start, whole Unix seconds — Broadcastify's own `ts`")
    duration_s: float = Field(default=0.0, ge=0, description="How long the transmission ran")


class BroadcastifyClaimOut(BaseModel):
    """Who may upload it.  ``granted`` false is an ordinary answer, not an error."""

    granted: bool = Field(description="May *you* upload it? False means somebody else already may")
    holder: str = Field(description="Whose claim it is — a recorder's instance_id, or `keyup`")
    claim_id: int = Field(description="The registry row, for tracing an upload back to its claim")


class StationIn(BaseModel):
    """Where and what the recorder *is*, as its operator described it.

    Facts nobody but the person who built the station knows: what it is called, where it
    stands, how to reach whoever runs it.  keyup stores them verbatim and never overrides
    them — the recorder is the authority here, not the archive.  Every field is optional
    because a station that has filled in only a name is a perfectly ordinary station.

    Coordinates are WGS84 decimal degrees and are range-checked rather than clamped: a
    longitude of −1810 clamped to −180 would put the station in the Pacific and say nothing
    about it, while a refused push tells the operator the config is wrong.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    location: str | None = Field(
        default=None,
        max_length=MAX_DESCRIPTION_LEN,
        description="Free text — a town, a street address, a grid square are all valid",
    )
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    elevation_m: float | None = Field(
        default=None,
        ge=-500,
        le=10_000,
        allow_inf_nan=False,
        description="Ground elevation above sea level, meters; the mast height is per tuner",
    )
    contact: str | None = Field(
        default=None, max_length=MAX_DESCRIPTION_LEN, description="Callsign, email or handle"
    )
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LEN)


class CountersIn(BaseModel):
    """A recorder's instance-level lifetime counters, reported alongside its tuners.

    Deliberately a handful of totals rather than a metrics dump: enough to see that a
    recorder is delivering calls and not filling its disk.  All are non-negative — they are
    monotonic counters and two are byte gauges — and a value past :data:`MAX_COUNTER` is
    refused as wrapped rather than stored as a total.
    """

    model_config = ConfigDict(extra="ignore")

    calls_started: int | None = Field(default=None, ge=0, le=MAX_COUNTER)
    calls_ended: int | None = Field(default=None, ge=0, le=MAX_COUNTER)
    uploaded: int | None = Field(default=None, ge=0, le=MAX_COUNTER)
    spooled: int | None = Field(default=None, ge=0, le=MAX_COUNTER)
    failed: int | None = Field(default=None, ge=0, le=MAX_COUNTER)
    spool_bytes: int | None = Field(
        default=None, ge=0, le=MAX_COUNTER, description="Bytes currently held in the spool"
    )
    disk_free_bytes: int | None = Field(
        default=None, ge=0, le=MAX_COUNTER, description="Free space on the spool's filesystem"
    )


class TunerDescriptionIn(BaseModel):
    """One tuner's operator-entered hardware description: what is actually on the mast.

    keyup cannot derive any of this — a receiver model, an antenna, the run of coax, a
    preamp, the height it is at — so the recorder carries it through from its own config and
    keyup only displays it.  It is what makes an RSSI figure mean something.
    """

    model_config = ConfigDict(extra="ignore")

    receiver: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    antenna: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    cable: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    amplifier: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    height_m: float | None = Field(
        default=None, ge=-100, le=1_000, allow_inf_nan=False, description="Antenna height, meters"
    )
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LEN)


class TunerGainIn(BaseModel):
    """The gain settings a tuner is actually running.

    These are the frame ``rssi_dbfs`` has to be read against: the level is ADC-referenced,
    so it moves with every one of these and is not a dBm figure.  Which fields are present
    depends on the backend — ``gain`` for a generic Soapy device, ``lna_db``/``vga_db``/
    ``amp`` for a HackRF.
    """

    model_config = ConfigDict(extra="ignore")

    gain: float | None = Field(
        default=None, ge=-50, le=100, allow_inf_nan=False, description="Backend-generic gain, dB"
    )
    lna_db: float | None = Field(default=None, ge=0, le=128, allow_inf_nan=False)
    vga_db: float | None = Field(default=None, ge=0, le=128, allow_inf_nan=False)
    amp: bool | None = Field(default=None, description="Front-end amplifier on")


class TunerGainElementIn(BaseModel):
    """One named gain stage the tuner has, and the range it accepts.

    A Soapy device describes its front end as a list of these (``LNA``, ``MIX``, ``IF``…),
    and the list is what makes an editable gain form possible at all: without it keyup would
    be offering a slider whose ends it had guessed.  Every desired ``gains`` key is checked
    against this list and every value against this element's ``[min, max]``, so a tuner that
    has not reported its elements yet simply cannot have per-element gain written.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(max_length=MAX_SETTING_KEY_LEN, description="The element's own name")
    min: float | None = Field(default=None, description="Lowest accepted value, dB")
    max: float | None = Field(default=None, description="Highest accepted value, dB")
    step: float | None = Field(default=None, description="Granularity, dB; 0/absent = continuous")


class TunerCapabilitiesIn(BaseModel):
    """What the tuner can change *without restarting*, and what costs a restart.

    ``restart_on`` names the desired fields whose change tears the stream down and builds it
    again — a gap in the recording, on a live site.  keyup never refuses one; it says so next
    to the field, because an operator who knows is choosing and an operator who does not is
    surprised.
    """

    model_config = ConfigDict(extra="allow")

    runtime_gain: bool | None = Field(
        default=None, description="Gain can be changed without restarting the stream"
    )
    runtime_ppm: bool | None = Field(
        default=None, description="Crystal correction can be changed without restarting"
    )
    restart_on: list[Annotated[str, StringConstraints(max_length=MAX_SETTING_KEY_LEN)]] | None = Field(
        default=None,
        max_length=64,
        description="Desired fields whose change restarts the tuner",
        examples=[["sample_rate", "bandwidth_hz"]],
    )


class TunerStatusEntry(BaseModel):
    """One tuner's reported status inside ``POST /recorder/tuners/status``.

    Only ``tuner_key`` is required; every other field is what the recorder happens to know.
    Unknown fields are accepted and stored — the reported set evolves with the recorder.

    ``bands`` is capped in size and in entry length like every other reported string, but
    deliberately *not* run through :func:`normalize_bands`.  This is the recorder telling
    keyup what it applied, and a Pydantic failure here fails the whole push — every tuner in
    it — rather than one entry.  A band list a recorder read out of its own TOML is its
    business to state and keyup's only to display; refusing to hear it would cost the
    operator the whole status report to make a point about spelling.
    """

    model_config = ConfigDict(extra="allow")

    tuner_key: str = Field(max_length=MAX_TUNER_KEY_LEN)
    serial: str | None = Field(default=None, max_length=128)
    backend: str | None = Field(default=None, max_length=64)
    center_hz: int | None = None
    span_hz: int | None = None
    role: str | None = Field(default=None, max_length=32)
    site: str | None = Field(default=None, max_length=128)
    locked: bool | None = None
    control_hz: int | None = None
    nac: int | None = None
    tsbk_rate: float | None = None
    pool_in_use: int | None = None
    pool_size: int | None = None
    overflow_per_s: float | None = None
    calls_active: int | None = None
    applied_version: int | None = Field(
        default=None, description="The desired_version the recorder has applied to this tuner"
    )
    bands: list[Annotated[str, StringConstraints(max_length=MAX_BAND_LEN)]] | None = Field(
        default=None,
        max_length=MAX_BANDS,
        description=(
            "The band list this tuner has actually applied — labels or explicit `lo-hi` Hz "
            "ranges, the same vocabulary the desired `bands` uses. Omitted when the tuner is "
            "unrestricted and records anything inside its capture span."
        ),
    )
    description: TunerDescriptionIn | None = Field(
        default=None, description="What the operator says is on this tuner's antenna run"
    )
    # `gain` is two shapes for one field, and that is deliberate rather than sloppy.  It
    # began as the nested :class:`TunerGainIn` object and trunkd now also sends a bare
    # overall gain in dB alongside the flat fields below.  Refusing either would fail
    # validation for the *whole push* — every tuner in it — over a recorder version, so both
    # are accepted and stored verbatim, and the reader takes the flat fields first.
    gain: float | TunerGainIn | None = Field(
        default=None, description="Overall gain in dB, or the older nested gain object"
    )
    # ---- the front end, as the tuner reports it ---------------------------------------
    #
    # Every field here is the reported twin of an editable one on
    # :class:`TunerDesiredIn`, and every one of them is lenient: a status push carries every
    # tuner of a recorder, so one over-strict annotation costs the operator the entire
    # report to make a point about a type.  The values are stored verbatim in
    # ``tuners.reported`` and are what the edit dialog validates the operator's input
    # against.
    gains: dict[str, Any] | None = Field(
        default=None, description="Per-element gain in dB, keyed as in `gain_elements`"
    )
    agc: bool | None = Field(default=None, description="Hardware AGC is on")
    ppm: float | None = Field(default=None, description="Crystal correction currently applied")
    auto_ppm: bool | None = Field(default=None, description="The tuner is tracking its own error")
    auto_ppm_applied: float | None = Field(
        default=None, description="The correction auto-ppm has actually settled on, ppm"
    )
    bandwidth_hz: int | None = Field(default=None, description="Analog filter bandwidth in Hz")
    rx_port: str | None = Field(default=None, max_length=MAX_SETTING_KEY_LEN)
    settings: dict[str, Any] | None = Field(default=None, description="Raw driver settings in force")
    lna_db: float | None = Field(default=None, description="HackRF front-end LNA gain, dB")
    vga_db: float | None = Field(default=None, description="HackRF baseband VGA gain, dB")
    amp: bool | None = Field(default=None, description="HackRF front-end amplifier is on")
    sample_rate: int | None = Field(default=None, description="Capture sample rate in Hz")
    data_follow: bool | None = Field(default=None, description="Following SNDCP data grants")
    control_frequencies: list[int] | None = Field(
        default=None, max_length=64, description="The control candidates this tuner is hunting"
    )
    gain_elements: list[TunerGainElementIn] | None = Field(
        default=None,
        max_length=MAX_GAIN_ELEMENTS,
        description="The named gain stages this device has, with their accepted ranges",
    )
    capabilities: TunerCapabilitiesIn | None = Field(
        default=None, description="What can be changed live, and what restarts the tuner"
    )
    rejected: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Desired fields the recorder refused, keyed by field name with the reason. The "
            "version is still acknowledged — the tuner is *not* left pending over a refused "
            "field — so this is the only place the refusal is ever visible."
        ),
        examples=[{"lna_db": "not supported by this backend"}],
    )
    rssi_dbfs: float | None = Field(
        default=None,
        ge=-200,
        le=50,
        allow_inf_nan=False,
        description=(
            "Wideband level in dBFS, ADC-referenced: it moves with the gain settings and is "
            "not a dBm figure. NaN/inf are refused — a level that is not a number says nothing."
        ),
    )
    freq_error_hz: float | None = Field(
        default=None,
        ge=-10_000_000,
        le=10_000_000,
        allow_inf_nan=False,
        description="Control-channel tracking error in Hz; only the control-following tuner has one",
    )


class UplinkIn(BaseModel):
    """What the recorder says about its **upload path**, inside the status push.

    The other half of the hold: keyup says what it wants in ``desired-state.recorder`` and
    the recorder answers here with what it is actually doing, where that came from — its own
    ``config``, this ``keyup``, or the operator's front ``panel`` — and how much is waiting
    in the spool while it holds.  Every field is optional and unknown ones are kept: an
    older recorder omits the whole block, and a newer one may grow it.

    ``source`` is not an enum on the wire.  A value keyup does not recognize is worth
    storing and showing verbatim — the recorder is the authority on why it is holding — and
    refusing the push over a word would cost the operator the whole report.
    """

    model_config = ConfigDict(extra="allow")

    spool_only: bool | None = Field(
        default=None, description="True while the recorder is holding calls in its spool"
    )
    source: str | None = Field(
        default=None,
        max_length=32,
        description="Where the current setting came from: config | keyup | panel",
    )
    spooled_calls: int | None = Field(
        default=None, ge=0, le=MAX_COUNTER, description="Calls waiting in the spool right now"
    )
    spool_bytes: int | None = Field(default=None, ge=0, le=MAX_COUNTER)
    url: str | None = Field(
        default=None, max_length=MAX_DESCRIPTION_LEN, description="Where the recorder uploads to"
    )


class MonitorIn(BaseModel):
    """``monitor`` on the status push — the recorder's own live audio panel, if it serves one.

    Keyup does not carry this audio.  The recorder streams what it is hearing from a small
    server of its own and this block says where that is, so the status page can offer the
    operator a link and nothing more.  A recorder built without the panel sends no block at
    all, which is what ``enabled``/``url`` being absent has to mean here as well.

    Unknown keys are **dropped** rather than kept — the opposite of ``UplinkIn``, and for a
    reason: this block is read for one purpose (render a link) and is the only recorder
    report whose stored content becomes an ``href`` on somebody's status page, so the column
    holds the six fields keyup knows the meaning of and nothing a future recorder invents.

    ``url`` is very often a loopback address: the panel binds to 127.0.0.1 by default, and
    that link works only in a browser running on the recorder itself.  Keyup stores it
    exactly as reported and says so beside the link rather than trying to rewrite it into
    something routable, which it has no way to know.
    """

    enabled: bool | None = Field(
        default=None, description="True when the recorder is serving its live monitor"
    )
    url: str | None = Field(
        default=None,
        max_length=MAX_DESCRIPTION_LEN,
        description="The panel's base URL — often a loopback address that only works on that host",
    )
    stream_url: str | None = Field(
        default=None, max_length=MAX_DESCRIPTION_LEN, description="The audio stream endpoint itself"
    )
    token_required: bool | None = Field(
        default=None, description="True when the panel asks for the recorder's token first"
    )
    streams: int | None = Field(
        default=None, ge=0, le=MAX_COUNTER, description="Listeners attached right now"
    )
    max_streams: int | None = Field(
        default=None, ge=0, le=MAX_COUNTER, description="How many listeners it will serve at once"
    )


class RecorderAppliedIn(BaseModel):
    """The recorder echoing back the recorder-level desired state it has applied.

    Exactly the tuner ``applied_version`` rule, one level up: keyup calls the recorder's
    desired state *pending* while this ``version`` lags ``desired_recorder_version``, so the
    operator can tell "saved" from "the recorder has taken it up".

    The fields beside the version are what it applied, echoed so the page can show applied
    against pending per field rather than one badge for the block.  ``extra="allow"`` and a
    permissive ``link``: a newer recorder growing a field of its own must not have its whole
    report refused by an older keyup, and the block is rendered, never acted on.

    The echoed ``link`` deliberately carries ``key_set``/``key_fingerprint`` and **not** the
    key: trunkd does not send the secret back, and a second copy of it arriving over the
    status push is something keyup would rather not have to think about.
    """

    model_config = ConfigDict(extra="allow")

    version: int = Field(ge=0, description="The desired_recorder_version this recorder has applied")
    spool_only: bool | None = Field(
        default=None, description="The spool_only value it applied at that version"
    )
    event_log_archive: bool | None = Field(
        default=None, description="The event_log_archive value it applied at that version"
    )
    link: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The link block it applied — `{enabled, bind, peers[], hello_interval_s, "
            "accept_remote_grants, publish_grants, defer_to_claims, key_set, "
            "key_fingerprint}`. The key itself is never echoed; the fingerprint (first 8 hex "
            "of its sha256) is what shows two peers hold the same one."
        ),
    )


class TunerStatusIn(BaseModel):
    """``POST /recorder/tuners/status`` — a recorder's periodic report of what its tuners are doing."""

    instance_id: str = Field(
        description="The recorder's stable identity", max_length=MAX_INSTANCE_ID_LEN
    )
    recorder_version: str | None = Field(
        default=None, description="Recorder software version", max_length=64
    )
    tuners: list[TunerStatusEntry] = Field(
        default_factory=list,
        max_length=MAX_TUNERS_PER_REPORT,
        description=f"At most {MAX_TUNERS_PER_REPORT} tuners per report",
    )
    station: StationIn | None = Field(
        default=None,
        description=(
            "The operator's description of the site. Omitting it means 'no change' — keyup "
            "keeps whatever was last reported rather than clearing it."
        ),
    )
    counters: CountersIn | None = Field(
        default=None,
        description=(
            "Instance-level lifetime counters. Omitting it means 'no change', exactly as for "
            "`station`."
        ),
    )
    uplink: UplinkIn | None = Field(
        default=None,
        description=(
            "The recorder's upload path: whether it is holding calls in its spool, where that "
            "setting came from, and how much is waiting. Omitting it means 'no change' — an "
            "older recorder sends none and keeps whatever was last reported."
        ),
    )
    recorder_applied: RecorderAppliedIn | None = Field(
        default=None,
        description=(
            "The recorder-level desired version this recorder has applied. Omitted by a "
            "recorder that does not know about recorder-level desired state at all."
        ),
    )
    link: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What this recorder knows about the peers it is linked to — conventionally "
            "`{\"peers\": [{\"instance\", \"alive\", \"last_seen\", \"claims\"}]}`. Stored "
            "**verbatim and opaque**: keyup renders it and decides nothing from it, so the "
            "shape is the recorder's to grow. Omitting it means 'no change'."
        ),
        examples=[{"peers": [{"instance": "trunkd-b", "alive": True, "claims": ["site:7"]}]}],
    )
    event_log: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the recorder says about its own event-log archive: whether it is "
            "`archive`-ing at all, the `source` of that setting (`config` when its own TOML "
            "decided, `keyup` when this desired state did), how many days are `pending`, the "
            "`last_archived` day and the `last_error` if the last attempt failed. Opaque like "
            "`link`; the authoritative record of what actually arrived is "
            "`GET /admin/event-logs`. Omitting it means 'no change'."
        ),
        examples=[
            {"archive": True, "source": "keyup", "pending": 2, "last_archived": "2026-09-01"}
        ],
    )
    monitor: MonitorIn | None = Field(
        default=None,
        description=(
            "Where the recorder serves its own live audio monitor, if it serves one: "
            "`{enabled, url, stream_url, token_required, streams, max_streams}`. Unlike every "
            "other block here, **omitting it clears the stored one**: the value becomes a link "
            "on the status page, and a link to a panel the recorder has stopped serving is "
            "worse than no link. A recorder with no panel sends nothing and gets none."
        ),
        examples=[
            {
                "enabled": True,
                "url": "http://127.0.0.1:8095",
                "stream_url": "http://127.0.0.1:8095/api/monitor/stream",
                "token_required": False,
                "streams": 0,
                "max_streams": 4,
            }
        ],
    )
    rejected: dict[str, str] | None = Field(
        default=None,
        description=(
            "Recorder-level desired fields this recorder **refused**, keyed by field name "
            "with the reason — the instance-level twin of a tuner's `rejected`. A refusal "
            "does not hold the recorder pending: it acknowledges the version either way, so "
            "this map is the only trace one leaves. Omitting it means 'no change'; send `{}` "
            "to say the last apply refused nothing."
        ),
        examples=[{"link": "built without the link feature"}],
    )
    capabilities: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the recorder can and cannot do live, conventionally "
            "`{\"recorder\": {\"restart_on\": [\"link\"]}}` — which recorder-level fields cost "
            "a restart of the process to change. Opaque and stored verbatim; keyup renders it "
            "as a warning beside those fields and refuses nothing over it. Omitting it means "
            "'no change'."
        ),
        examples=[{"recorder": {"restart_on": ["link", "event_log_archive"]}}],
    )


class TunerStatusAccepted(BaseModel):
    """What the status endpoint answers: which recorder it matched and how many tuners it stored."""

    ok: bool = True
    recorder_id: int
    tuners_upserted: int = 0
    tuners_skipped: int = Field(default=0, description="Malformed entries that were ignored")


class TunerOut(ORMModel):
    """One tuner as an operator sees it: what it reports, what keyup wants, and whether it lags."""

    id: int
    tuner_key: str
    reported: dict[str, Any] | None = None
    reported_at: datetime | None = None
    desired: dict[str, Any] | None = None
    desired_version: int = 0
    updated_by: str | None = None
    updated_at: datetime | None = None
    drift: bool = Field(
        default=False,
        description=(
            "True when the recorder has not applied the current desired_version: the tuner has "
            "been edited (desired_version > 0) and reports an applied_version that differs. A "
            "tuner never edited, or one whose recorder has not reported an applied_version yet, "
            "does not count as drifted."
        ),
    )
    rejected: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Desired fields this tuner's recorder **refused**, keyed by field name with its "
            "reason, lifted out of `reported` so a client need not dig for it. A refusal does "
            "*not* hold the tuner pending: the recorder acknowledges the version either way, "
            "so `drift` clears and this is what is left to say the setting did not take."
        ),
        examples=[{"lna_db": "not supported by this backend"}],
    )


class RecorderStreamOut(BaseModel):
    """Whether this recorder is holding the push channel open (``WS /recorder/stream``).

    **Per worker process.**  The registry behind it lives in memory, so with several uvicorn
    workers this reports the connections the worker that answered the request happens to
    hold — ``connected: false`` means "not to me", not "nowhere".
    """

    connected: bool = Field(
        default=False, description="True when at least one push socket for this recorder is open here"
    )
    since: datetime | None = Field(
        default=None, description="When the oldest of those sockets connected, or null"
    )
    client_count: int = Field(
        default=0, description="How many push sockets this worker holds for the recorder"
    )


class RecorderOut(ORMModel):
    """A recorder with its tuners (``GET /admin/recorders``)."""

    id: int
    instance_id: str
    label: str | None = None
    version: int = 0
    recorder_version: str | None = None
    last_seen_at: datetime | None = None
    #: when ``station``/``counters`` were last reported — not every contact carries them
    reported_at: datetime | None = None
    station: dict[str, Any] | None = Field(
        default=None, description="The operator's description of the site, as the recorder sent it"
    )
    counters: dict[str, Any] | None = Field(
        default=None, description="The recorder's instance-level lifetime counters"
    )
    priority: int = Field(
        default=100,
        description=(
            "Which recorder to prefer when several cover one site — **lower is preferred**. "
            "Only the arbiter reads it, as the tie-break between warm spares."
        ),
    )
    standby: RecorderStandbyName = Field(
        default="hot",
        description=(
            "`hot` records whenever it is healthy; `warm` is a spare kept registered with its "
            "tuners disabled, which the arbiter may enable when every hot recorder for a site "
            "has gone stale; `cold` is never touched automatically."
        ),
    )
    promoted_at: datetime | None = Field(
        default=None,
        description=(
            "When the arbiter enabled this recorder's tuners, and null whenever it did not — "
            "a warm recorder an operator enabled by hand is never auto-demoted."
        ),
    )
    created_at: datetime | None = None
    desired_spool_only: bool | None = Field(
        default=None,
        description=(
            "What keyup wants of this recorder's uploads: true hold, false upload, null no "
            "opinion (the recorder keeps its own config/panel value)."
        ),
    )
    desired_recorder_version: int = Field(
        default=0, description="The generation of `desired_spool_only`, bumped on every write"
    )
    applied_recorder_version: int | None = Field(
        default=None,
        description="The desired_recorder_version the recorder last echoed back, or null",
    )
    uplink: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the recorder reports about its upload path: `spool_only`, `source` "
            "(config|keyup|panel), `spooled_calls`, `spool_bytes`, `url`. Null until a "
            "recorder that knows about it reports."
        ),
    )
    spool_only_pending: bool = Field(
        default=False,
        description=(
            "True when keyup has an opinion on this recorder's uploads that the recorder has "
            "not yet echoed back — `desired_spool_only` is not null and the applied version "
            "differs from `desired_recorder_version`. The recorder-level twin of a tuner's "
            "`drift`."
        ),
    )
    desired_event_log_archive: bool | None = Field(
        default=None,
        description=(
            "What keyup wants of this recorder's event-log archive: true ship the daily log, "
            "false do not, null no opinion (the recorder keeps its own config value)."
        ),
    )
    desired_link: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The recorder-to-recorder link block keyup manages, **with the shared key "
            "removed**: `{enabled, bind, peers[], hello_interval_s, accept_remote_grants, "
            "publish_grants, defer_to_claims, key_set, key_fingerprint}`. Null means keyup "
            "does not manage the link and the recorder keeps its configured one. The key is "
            "never in a list or detail read — `key_fingerprint` (the first 8 hex of its "
            "sha256) is what shows two peers hold the same one, and "
            "`GET /admin/recorders/{id}/link/key` is the only route that returns it."
        ),
    )
    desired_broadcastify: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The Broadcastify Calls block this recorder is being sent, **with the API key "
            "removed**: `{enabled, endpoint_url, system_id, encoding, talkgroups, "
            "skew_limit_s, claim, claim_url, revision, api_key_set, api_key_fingerprint}`. "
            "Null — the usual answer — means keyup uploads this recorder's calls itself. "
            "Not stored on the recorder: it is derived from the export settings, so every "
            "recorder is sent the same block and it appears and disappears with "
            "`broadcastify_upload_at`. Read-only here; the key itself leaves only by the "
            "recorder's own desired-state poll."
        ),
    )
    desired_skip_talkgroups: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The talkgroup skip list this recorder is being sent, **as a count rather than "
            "the list**: `{count, revision}`. Null — the shipped answer — means every "
            "talkgroup is uploaded. Derived from the talkgroup table, so every recorder is "
            "sent the same one; the list itself is on the Talkgroups page, which is where "
            "it is changed."
        ),
        examples=[{"count": 12, "revision": "9f2c41a80b7e"}],
    )
    recorder_pending: bool = Field(
        default=False,
        description=(
            "True when keyup has *any* recorder-level opinion the recorder has not echoed "
            "back — the whole-block twin of `spool_only_pending`, which stays scoped to the "
            "upload hold. Set when at least one of `desired_spool_only`, "
            "`desired_event_log_archive` and `desired_link` is not null and "
            "`applied_recorder_version` differs from `desired_recorder_version`."
        ),
    )
    recorder_applied: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The whole block the recorder echoed back — `{version, spool_only, "
            "event_log_archive, link}` — so a client can show applied against pending per "
            "field. Null on a recorder that has never echoed one. Its `link` half carries "
            "`key_set`/`key_fingerprint`, never the key."
        ),
    )
    recorder_rejected: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Recorder-level desired fields this recorder refused, keyed by field name with "
            "its reason — the instance-level twin of a tuner's `rejected`, and the only trace "
            "a refusal leaves."
        ),
        examples=[{"link": "built without the link feature"}],
    )
    recorder_capabilities: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the recorder said it can change live, conventionally "
            "`{\"restart_on\": [...]}` — the recorder-level fields that cost a restart. "
            "Rendered as a warning; nothing is refused over it."
        ),
    )
    link: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What this recorder reports about the peers it is linked to, verbatim and opaque "
            "— conventionally `{\"peers\": [{\"instance\", \"alive\", \"last_seen\", "
            "\"claims\"}]}`. Null on a recorder that reports none."
        ),
    )
    event_log: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What this recorder reports about its own event-log archive — days pending, the "
            "last one shipped. Opaque; what actually arrived is `GET /admin/event-logs`."
        ),
    )
    stream: RecorderStreamOut = Field(
        default_factory=RecorderStreamOut,
        description=(
            "The recorder's push channel (`WS /api/recorder/stream`): `connected`, `since` and "
            "`client_count`. **Per worker process** — the registry is in memory, so a `false` "
            "here means the worker that answered is not holding a socket for this recorder, "
            "not that nothing anywhere is."
        ),
    )
    tuners: list[TunerOut] = Field(default_factory=list)


class RecorderPolicyIn(BaseModel):
    """``PUT /admin/recorders/{id}`` — the operator's policy for one recorder.

    Every field is optional and an omitted one is left alone, exactly as the tuner PUT
    merges.  This is deliberately *not* where a tuner's desired state is written: policy is
    what keyup should do about a recorder, ``desired`` is what the recorder should do about
    a tuner, and the arbiter is the only thing that turns the first into the second.

    The three **desired-state** fields — ``spool_only``, ``event_log_archive`` and ``link``
    — are the exception to "null means unset": there, null is a value in its own right —
    *keyup has no opinion, leave the recorder on its own setting* — so the three states are
    sent as a value / ``null`` / not sent at all, and "do not touch it" is the last one.
    Every write that actually changes one of them bumps ``desired_recorder_version``, which
    the recorder echoes back; a write of the value already stored bumps nothing, so a form
    that resubmits itself does not leave the recorder pending for ever.

    ``instance_id`` is the odd field out and is documented on itself: it is not policy but
    the recorder's *identity*, and renaming it rewrites the instance text on everything the
    recorder has already filed.
    """

    instance_id: str | None = Field(
        default=None,
        max_length=MAX_INSTANCE_ID_LEN,
        description=(
            "Rename the recorder's `instance_id` — the identity it names itself by, not the "
            "display `label`. Everything already filed under the old name (`radio_events`, "
            "`recorder_observations`, `event_log_archives`, a call's `source_instance`) is "
            "rewritten to the new one in the same transaction, so the archive follows the "
            "rename rather than splitting in two. A name another recorder already holds is a "
            "409. **The recorder itself must be reconfigured to match**: one that keeps "
            "posting the old name simply re-creates a row under it."
        ),
    )
    label: str | None = Field(
        default=None,
        max_length=MAX_DESCRIPTION_LEN,
        description="A human name for the recorder, when its instance_id is not one",
    )
    priority: int | None = Field(
        default=None,
        ge=0,
        le=10_000,
        description="Lower is preferred; the arbiter picks the lowest-numbered healthy warm spare",
    )
    standby: RecorderStandbyName | None = Field(
        default=None, description="hot | warm | cold — what this recorder is for"
    )
    spool_only: bool | None = Field(
        default=None,
        description=(
            "Hold this recorder's uploads in its local spool (`true`), push them (`false`), or "
            "say nothing and leave it on its own config/panel value (`null`). Unlike every "
            "other field here, an explicit `null` is a *write* — it clears keyup's opinion — "
            "while omitting the key leaves the current one alone. Every change bumps "
            "`desired_recorder_version`, which the recorder echoes back."
        ),
    )
    event_log_archive: bool | None = Field(
        default=None,
        description=(
            "Ship the daily raw event log to keyup (`true`), do not (`false`), or say nothing "
            "and leave the recorder on its own config value (`null`). The same three-way rule "
            "as `spool_only`: an explicit `null` is a write, omitting the key is not."
        ),
    )
    link: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The whole recorder-to-recorder link block to manage — `{enabled, bind, peers[], "
            "key, hello_interval_s, accept_remote_grants, publish_grants, defer_to_claims}` — "
            "or an explicit `null` to stop managing it, which leaves the recorder on its "
            "configured link. Written as a unit, never merged: a peer list saved without the "
            "key it authenticates with is not a link. `key` is the one field that may be "
            "**omitted** on an update, which keeps the key already stored — the edit dialog "
            "does not hold the secret unless the operator revealed it. Refused with a 422 "
            "naming the field for a `bind` or peer that is not `host:port` (a hostname, an "
            "IPv4 address, or an IPv6 one in brackets, with a port of 1…65535), a duplicated "
            "peer, an enabled link with no peers, a key under 16 characters, or a "
            "`hello_interval_s` outside 1…60."
        ),
        examples=[
            {
                "enabled": True,
                "bind": "0.0.0.0:9110",
                "peers": ["recorder-a:9110"],
                "hello_interval_s": 5,
                "accept_remote_grants": True,
                "publish_grants": True,
                "defer_to_claims": True,
            }
        ],
    )


class RecorderLinkKeyOut(BaseModel):
    """The shared link key in clear — the answer to the *one* route that returns it.

    Split out from :class:`RecorderOut` rather than made a field of it precisely so that it
    cannot be returned by accident: a list of recorders has no way to reach this model, and
    the only handler that builds one writes an activity row as it does.
    """

    key: str | None = Field(
        default=None, description="The shared secret in clear, or null when none is stored"
    )
    key_set: bool = Field(default=False, description="Whether a key is stored at all")
    key_fingerprint: str | None = Field(
        default=None, description="First 8 hex of its sha256 — what the list views show"
    )


class RecorderLinkKeyGenerated(BaseModel):
    """A freshly generated shared key, which nothing has stored yet.

    Generating is deliberately *not* saving: the operator gets a key to paste into both
    recorders' dialogs, and it becomes this recorder's key only when the form is saved. A
    route that stored it would break the link the moment someone clicked the button and
    then cancelled.
    """

    key: str = Field(description="32 random bytes as 64 hex characters")


class TunerDesiredIn(BaseModel):
    """``PUT /admin/recorders/{id}/tuners/{key}`` — the desired state to write for one tuner.

    Every field is optional and the write is a merge: a field **omitted** from the body is
    left as it was, a field sent with a **value** is set, and a field sent as **null** is
    *cleared* (removed from the tuner's desired config).  So "focus no particular site" is
    ``{"site_id": null}``, while leaving ``site_id`` out keeps the current focus.

    ``center_frequency`` is validated against the tuner's last *reported* span: a retune is
    limited to within half a span either side of the reported center (``center_hz ±
    span_hz/2``), because the recorder cannot tune outside the window it is currently on.

    ``bands`` follows the same three-way rule, and its three states are worth spelling out
    because two of them look alike: ``[]`` is *set to no restriction*, and ``null`` is
    *stop saying anything*, which leaves the recorder on its own configured bands.
    """

    role: TunerRoleName | None = None
    site_id: int | None = Field(default=None, description="A keyup site id to focus on")
    site: str | None = Field(default=None, description="A free-form site label, when there is no id")
    center_frequency: int | None = Field(default=None, description="Center frequency in Hz")
    enabled: bool | None = None
    spectrum: bool | None = Field(default=None, description="Whether to stream a spectrum for this tuner")
    bands: list[str] | None = Field(
        default=None,
        description=(
            "Which downlink ranges this tuner may record, as band labels "
            "(`vhf`|`uhf`|`700`|`800`|`900`, case-insensitive) or explicit `\"lo-hi\"` ranges in "
            "Hz such as `\"856000000-866000000\"`; at most 16 entries of at most 64 characters, "
            "deduplicated and stored lowercase. An **empty list** clears the restriction — the "
            "tuner records anything inside its capture span. An explicit **null** removes the "
            "key from the desired state entirely, exactly as it does for every other field "
            "here, which leaves the recorder on whatever its own configuration says. "
            "The recorder refuses (and stays pending on) a list that does not overlap the "
            "span it is capturing, so a list is worth checking against the tuner's reported "
            "center and span before sending it."
        ),
        examples=[["800", "856000000-866000000"]],
    )

    # ---- the front end itself ---------------------------------------------------------
    #
    # Everything below is a *radio* setting rather than a *what to record* one, and all of
    # it follows the same three-way merge rule as the fields above: omitted leaves it,
    # a value sets it, an explicit null hands the field back to the recorder's own config.
    #
    # The bounds that need the tuner's last **reported** state — which gain elements exist
    # and what their ranges are, which backend this is, where the span sits — are checked in
    # the route rather than here, because a schema cannot see the database.  See
    # :func:`app.api.recorders.validate_tuner_settings`; the caps here are only the ones
    # that hold whatever the tuner turns out to be.
    gain: float | None = Field(
        default=None,
        description=(
            "Overall gain in dB (Soapy devices). Refused when the tuner reports a `hackrf` "
            "backend, which has no single overall gain — set `lna_db`/`vga_db` instead."
        ),
    )
    gains: dict[str, float] | None = Field(
        default=None,
        description=(
            "Per-element gain in dB, keyed by the element name the tuner reports in "
            "`gain_elements` (`LNA`, `MIX`, `IF`…). Every key must be one the tuner named "
            "and every value must fall inside that element's reported `[min, max]`."
        ),
        examples=[{"LNA": 14.0, "IF": 8.0}],
    )
    agc: bool | None = Field(default=None, description="Hardware AGC on (Soapy devices)")
    ppm: float | None = Field(
        default=None, description=f"Crystal correction, ±{MAX_PPM:.0f} ppm"
    )
    auto_ppm: bool | None = Field(
        default=None,
        description=(
            "Let the recorder track the control channel's frequency error and correct itself. "
            "What it has actually applied comes back as reported `auto_ppm_applied`."
        ),
    )
    auto_ppm_limit: float | None = Field(
        default=None,
        description=f"How far auto-ppm may wander from 0, in ppm (0…{MAX_PPM:.0f})",
    )
    bandwidth_hz: int | None = Field(
        default=None, description="Analog front-end filter bandwidth in Hz; 0 lets the driver choose"
    )
    rx_port: str | None = Field(
        default=None,
        max_length=MAX_SETTING_KEY_LEN,
        description="Which antenna port to receive on (Soapy devices): `RX`, `LNAW`, `TX/RX`…",
    )
    settings: dict[str, str] | None = Field(
        default=None,
        description=(
            "Raw driver key/value settings passed straight to Soapy "
            f"(at most {MAX_TUNER_SETTINGS} entries). Soapy devices only."
        ),
        examples=[{"biastee": "true"}],
    )
    lna_db: int | None = Field(
        default=None,
        description=(
            f"HackRF front-end LNA gain: 0…{HACKRF_LNA_MAX_DB} dB in {HACKRF_LNA_STEP_DB} dB "
            "steps. HackRF only."
        ),
    )
    vga_db: int | None = Field(
        default=None,
        description=(
            f"HackRF baseband VGA gain: 0…{HACKRF_VGA_MAX_DB} dB in {HACKRF_VGA_STEP_DB} dB "
            "steps. HackRF only."
        ),
    )
    amp: bool | None = Field(default=None, description="HackRF front-end amplifier on. HackRF only.")
    data_follow: bool | None = Field(
        default=None,
        description=(
            "Follow SNDCP data grants as well as voice grants. Needed for the unknown-data "
            "analysis; it costs a recorder slot per data call."
        ),
    )
    remote_grants: bool | None = Field(
        default=None,
        description=(
            "Let this tuner act on grants a **linked peer** published, not only the ones it "
            "decoded itself. Needs the recorder's `link` to be up and accepting them; a tuner "
            "told this on a recorder with no link comes back `rejected`. Three-valued like "
            "every other field here: null hands it back to the recorder's own config."
        ),
    )
    control_frequencies: list[int] | None = Field(
        default=None,
        max_length=MAX_CONTROL_CANDIDATES,
        description=(
            "Control-channel candidates in Hz for this tuner to hunt, tried in order. Each "
            f"must be a multiple of {CHANNEL_STEP_HZ} Hz and, when the tuner has reported a "
            "span, fall inside it."
        ),
        examples=[[851050000, 852162500]],
    )
    sample_rate: int | None = Field(
        default=None,
        description=(
            f"Capture sample rate in Hz. `sample_rate / {CHANNEL_STEP_HZ}` must be an even "
            f"whole number of at least {MIN_SAMPLE_RATE_CHANNELS} channels — so "
            f"{CHANNEL_STEP_HZ * MIN_SAMPLE_RATE_CHANNELS} Hz is the floor and every step "
            f"above it is {CHANNEL_STEP_HZ * 2} Hz."
        ),
        examples=[2_400_000],
    )

    @field_validator("bands")
    @classmethod
    def _check_bands(cls, value: list[str] | None) -> list[str] | None:
        """Normalize the list, or 422 naming the entry that is wrong.

        Validating here rather than trusting the recorder is the whole point of the field:
        trunkd drops a tuner's *entire* desired entry over one malformed band, silently and
        on the next poll, where nobody is looking.  A 422 at the edit costs one retry.
        """
        return None if value is None else normalize_bands(value)

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        """Cap the count and the length of each key and value; the rest is the driver's."""
        if value is None:
            return None
        if len(value) > MAX_TUNER_SETTINGS:
            raise ValueError(f"at most {MAX_TUNER_SETTINGS} settings, got {len(value)}")
        for key, entry in value.items():
            if not key.strip():
                raise ValueError("a setting key may not be blank")
            if len(key) > MAX_SETTING_KEY_LEN:
                raise ValueError(f"settings[{key!r}]: the key is longer than {MAX_SETTING_KEY_LEN}")
            if len(entry) > MAX_SETTING_VALUE_LEN:
                raise ValueError(
                    f"settings[{key!r}]: the value is longer than {MAX_SETTING_VALUE_LEN}"
                )
        return value

    @field_validator("gains")
    @classmethod
    def _check_gains(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        """Cap the count and refuse a blank element name; the ranges need the reported state."""
        if value is None:
            return None
        if len(value) > MAX_GAIN_ELEMENTS:
            raise ValueError(f"at most {MAX_GAIN_ELEMENTS} gain elements, got {len(value)}")
        for key in value:
            if not key.strip():
                raise ValueError("a gain element name may not be blank")
        return value


# ---- live spectrum (ephemeral FFT frames; never persisted, never through NOTIFY) --------

#: The most FFT bins one frame may carry.  A frame over this is rejected rather than allowed
#: to grow the in-process cache or a client's queue without bound — the contract trunkd emits
#: against.  8/16/32-hundred-odd bins is a normal waterfall width; 2048 is a generous ceiling.
MAX_SPECTRUM_BINS = 2048

#: The most frames a single batched ``POST /recorder/spectrum`` may carry.
MAX_SPECTRUM_FRAMES = 32


class SpectrumFrameIn(BaseModel):
    """One FFT frame a recorder streams for a tuner (``POST /recorder/spectrum``).

    ``db`` is exactly ``bins`` signed bytes (−128..127), each a quantized dBFS value for one
    frequency bin across ``center_hz ± span_hz/2``.  The frame is ephemeral: keyup caches
    only the latest per tuner and fans it out to any live viewer, and never stores it.
    """

    instance_id: str = Field(
        description="The recorder's stable identity", max_length=MAX_INSTANCE_ID_LEN
    )
    tuner_key: str = Field(description="The recorder's key for the tuner", max_length=MAX_TUNER_KEY_LEN)
    ts_ms: int = Field(description="Recorder wall-clock of the frame, epoch milliseconds")
    center_hz: int = Field(description="Center frequency of the span, in Hz")
    span_hz: int = Field(description="Total width of the span, in Hz")
    bins: int = Field(ge=1, le=MAX_SPECTRUM_BINS, description="Number of FFT bins in `db`")
    db: list[int] = Field(description="`bins` signed bytes (−128..127), one quantized dBFS per bin")

    @model_validator(mode="after")
    def _check_db(self) -> SpectrumFrameIn:
        if len(self.db) != self.bins:
            raise ValueError("db length must equal bins")
        for value in self.db:
            if value < -128 or value > 127:
                raise ValueError("db values must be signed bytes in -128..127")
        return self


class SpectrumBatchIn(BaseModel):
    """A batch wrapper: ``{"frames": [frame, ...]}``.  A bare frame object is also accepted."""

    frames: list[SpectrumFrameIn] = Field(
        max_length=MAX_SPECTRUM_FRAMES, description=f"At most {MAX_SPECTRUM_FRAMES} frames per post"
    )


# ---- archived event logs (``POST /recorder/event-log``, docs/RECORDERS.md) ---------------

#: A hex sha256, and nothing else: the digest names the file on disk in the duplicate check,
#: so a value that is not one has no business reaching the filesystem.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EventLogAccepted(BaseModel):
    """What ``POST /recorder/event-log`` answers once the day is on disk and verified."""

    stored: bool = True
    bytes: int = Field(description="How many bytes were written, which is what was verified")


class EventLogArchiveOut(ORMModel):
    """One archived day of one recorder's event log, as the operator's list shows it.

    Deliberately without the on-disk ``path``: it is an absolute path inside the API
    container and says nothing to a browser, which fetches the file from
    ``GET /admin/event-logs/{archive_id}/download`` instead.
    """

    id: int
    instance: str
    date: date
    bytes: int = 0
    sha256: str
    received_at: datetime | None = None


class SpectrumAccepted(BaseModel):
    """What the ingest answers: how many frames it took and how many it dropped."""

    ok: bool = True
    accepted: int = Field(default=0, description="Frames cached and fanned out to viewers")
    skipped: int = Field(default=0, description="Frames for an unknown tuner, quietly ignored")


# ---- band occupancy (folded from recorder_observations, no new recorder data) ----------


class OccupancyBand(BaseModel):
    """One band-plan entry, as the strip lays channels out along it."""

    id: int | None = None
    base_hz: int | None = None
    spacing_hz: int | None = None
    bandwidth_hz: int | None = None


class OccupancyChannel(BaseModel):
    """One known channel, positioned by frequency and colored by recency and role."""

    frequency_hz: int
    channel: str | None = None
    role: Literal["control", "voice", "data"] = "voice"
    grants: int = 0
    calls: int = 0
    last_seen: datetime | None = None


class SiteOccupancy(BaseModel):
    """``GET /admin/occupancy`` — one site's band strip, folded from its latest observation."""

    site_id: int
    site_name: str | None = None
    system_id: int | None = None
    observed_at: datetime | None = None
    bands: list[OccupancyBand] = Field(default_factory=list)
    channels: list[OccupancyChannel] = Field(default_factory=list)



# --------------------------------------------------------------------------------------
# bulk edits (POST /{talkgroups,radios,units,personnel}/bulk)
# --------------------------------------------------------------------------------------

#: How many rows one bulk edit may touch.  The directory lists page at 200 and cap at 500,
#: so 500 is "everything you can have selected"; beyond that the request is a script, and a
#: script should be paging rather than holding one transaction open over the whole table.
BULK_MAX_IDS = 500

#: The reason bulk is a POST on a literal sub-path rather than a ``PATCH /talkgroups``:
#: ``PATCH /talkgroups/{id}`` already owns that shape, and ``/talkgroups/bulk`` would be
#: parsed as an id and 422 before any handler saw it.
BULK_IDS = Annotated[
    list[int],
    Field(
        min_length=1,
        max_length=BULK_MAX_IDS,
        description=f"The rows to change; 1–{BULK_MAX_IDS} of them",
        examples=[[12, 13, 14]],
    ),
]


def _reject_bulk_fields(changes: BaseModel, names: tuple[str, ...]) -> None:
    """Refuse per-row identity fields in a bulk body.

    A bulk edit writes *the same* value onto every selected row.  For a field that has to
    stay unique — or that only means anything about one particular row — that is never
    what the operator wanted, so it is a 422 with a sentence rather than a collision at
    commit time or a hundred radios all called the same thing.
    """
    present = [name for name in names if name in changes.model_fields_set]
    if present:
        raise ValueError(
            f"{', '.join(present)} is per-row and cannot be set in bulk; edit each row instead"
        )


class TalkgroupBulkPatch(BaseModel):
    """Body of ``POST /talkgroups/bulk``: which rows, and the one patch to write onto all."""

    ids: BULK_IDS
    changes: TalkgroupPatch = Field(
        description="Exactly as `PATCH /talkgroups/{id}`: only the keys present are written"
    )


class TalkgroupBulkResult(BaseModel):
    """``{updated, items}`` — how many rows changed, and each of them as it now stands."""

    updated: int
    items: list[TalkgroupOut] = Field(default_factory=list)


#: The per-talkgroup export checkboxes, and the only values the copy endpoint accepts.
TALKGROUP_EXPORT_FLAGS = ("export_broadcastify", "export_bcfy_calls", "export_openmhz")
TalkgroupExportFlag = Literal["export_broadcastify", "export_bcfy_calls", "export_openmhz"]


class TalkgroupExportFlagCopy(BaseModel):
    """Body of ``POST /admin/talkgroups/export-flags/copy``: one column onto another.

    It exists because splitting Broadcastify's shared checkbox into a Feed one and a Calls
    one left every operator with a curated selection on the old column and a default-true
    one on the new: the honest first move is "make Calls what Broadcastify already was",
    and doing that by hand over several hundred talkgroups is how a selection acquires
    mistakes.
    """

    source: TalkgroupExportFlag = Field(description="The column to read")
    destination: TalkgroupExportFlag = Field(description="The column to overwrite")
    ids: list[int] | None = Field(
        default=None,
        description=(
            "The talkgroups to copy across. Omit (or send `null`) together with "
            "`all: true` to copy over the whole directory."
        ),
    )
    all: bool = Field(
        default=False,
        description=(
            "Copy over **every** talkgroup rather than a selection. Deliberately explicit: "
            "it is the difference between an edit and a directory-wide overwrite."
        ),
    )

    @model_validator(mode="after")
    def _check(self):  # noqa: ANN202
        if self.source == self.destination:
            raise ValueError("source and destination must be different columns")
        if self.all and self.ids:
            raise ValueError("send either ids or all: true, not both")
        if not self.all and not self.ids:
            raise ValueError("send ids, or all: true to copy the whole directory")
        return self


class TalkgroupExportFlagCopyResult(BaseModel):
    """What the copy did — and, as loudly, what it refused to do."""

    source: str
    destination: str
    considered: int = Field(description="Talkgroups the request covered")
    updated: int = Field(description="Rows whose destination flag actually changed")
    unchanged: int = Field(description="Rows that already held the source value")
    skipped_not_trunked: int = Field(
        default=0,
        description=(
            "Rows left alone because the destination is the Broadcastify **Calls** flag "
            "and the talkgroup's system is not trunked. Calls publishes trunked systems "
            "only, so the flag is held false there rather than copied."
        ),
    )
    items: list[TalkgroupOut] = Field(default_factory=list)


class RadioBulkPatch(BaseModel):
    """Body of ``POST /radios/bulk``.  ``alias`` and ``unit_id`` are refused: see below."""

    ids: BULK_IDS
    changes: RadioPatch

    @model_validator(mode="after")
    def _no_identity(self):  # noqa: ANN202
        # A radio's alias and unit id *name that radio*.  Writing one of them onto a
        # selection would leave every radio in it wearing the same label, which is not a
        # bulk edit of a policy — it is the loss of the directory.
        _reject_bulk_fields(self.changes, ("alias", "unit_id"))
        return self


class RadioBulkResult(BaseModel):
    updated: int
    items: list[RadioOut] = Field(default_factory=list)


class UnitBulkPatch(BaseModel):
    """Body of ``POST /units/bulk``.  ``callsign`` is refused: it is the unique key."""

    ids: BULK_IDS
    changes: UnitPatch

    @model_validator(mode="after")
    def _no_callsign(self):  # noqa: ANN202
        # ``uq_units_agency_callsign``: the second row would lose the race and the whole
        # transaction with it.
        _reject_bulk_fields(self.changes, ("callsign",))
        return self


class UnitBulkResult(BaseModel):
    updated: int
    items: list[UnitOut] = Field(default_factory=list)


class PersonBulkPatch(BaseModel):
    """Body of ``POST /personnel/bulk``."""

    ids: BULK_IDS
    changes: PersonPatch


class PersonBulkResult(BaseModel):
    updated: int
    items: list[PersonOut] = Field(default_factory=list)


__all__ = [n for n in dir() if n[0].isupper()]
