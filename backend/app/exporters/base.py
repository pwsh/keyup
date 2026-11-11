"""What every exporter client is handed, and what it has to answer.

One small dataclass (:class:`CallFacts`) sits between the schema and the two service
clients.  Both services want the same handful of numbers — talkgroup, unit, frequency,
start time, duration, emergency flag, patch members — and both spell them differently;
resolving them once, from the session, keeps the per-service modules to nothing but the
request shape they are documenting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.models import Call, Patch, Radio, Talkgroup

__all__ = [
    "AmbiguousExportError",
    "CallFacts",
    "ExportClient",
    "ExportError",
    "PermanentExportError",
    "SkipCall",
    "UploadResult",
    "facts_for",
]


class SkipCall(Exception):  # noqa: N818 - it is a signal, not an error
    """The service does not want this call, and that is a fine outcome.

    Raised for the answers that are neither success nor failure: Broadcastify's
    ``SKIPPED---ALREADY-RECEIVED-THIS-CALL`` (another uploader got there first) and
    OpenMHz's "Talkgroup does not exist, skipping" on a system configured to ignore unknown
    talkgroups.  The worker records the ledger row as ``skipped`` and spends no retries.
    """

    def __init__(
        self,
        reason: str,
        *,
        response: str | None = None,
        response_code: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        #: the service's own line, already redacted and capped, ready for the ledger — see
        #: :class:`ExportError`.
        self.response = response
        self.response_code = response_code


class ExportError(RuntimeError):
    """The upload failed and is worth retrying (a timeout, a 5xx, a refused connection).

    ``response`` is what the service actually said, as one already-redacted, already-capped
    line ready to be stored in ``call_exports.response``, and ``response_code`` is the
    integer that led it.  Both are ``None`` for the failures that never reached an answer
    to parse — a timeout, a refused connection, a transcode error — and the ledger keeps
    that distinction: a null response means "we never heard", not "it said nothing".
    """

    def __init__(
        self,
        *args: object,
        response: str | None = None,
        response_code: int | None = None,
    ) -> None:
        super().__init__(*args)
        self.response = response
        self.response_code = response_code


class PermanentExportError(ExportError):
    """The service refused this call and will refuse it again — do not spend retries on it.

    A rejected API key, a talkgroup the system does not contain, a duplicate the service
    already has.  The worker parks the ledger row immediately rather than counting five
    attempts at something that cannot succeed.
    """


class AmbiguousExportError(ExportError):
    """The whole request went out and no answer came back: the service may have the call.

    A read timeout, a connection dropped while waiting for the status line, or a gateway
    that gave up on the origin (502/504) — every one of them after the body was sent.  For a
    service that does not deduplicate (OpenMHz), a blind retry of one of these can publish
    the call twice, so the worker never re-posts one without first asking the service
    whether it already has it (:meth:`find_uploaded`), and parks it as uncertain when it
    cannot ask (:data:`app.exporters.ledger.UNCERTAIN_REASON`).
    """


@dataclass(frozen=True, slots=True)
class UploadResult:
    """What one upload hands back: the service's id for the call, and its answer.

    Two fields rather than the bare identifier this used to be, because the ledger now
    records *what the service said* on every attempt, success included — and the identifier
    is not the answer.  For Broadcastify Calls the id is the one-time URL's last path
    segment and the answer is the ``0 <segment>`` line that carried it; OpenMHz returns no
    identifier at all and its answer is an HTTP status line.  Whatever a client puts in
    ``response`` is stored verbatim, so it must arrive already redacted and already short
    enough (500 characters).
    """

    remote_id: str | None = None
    response: str | None = None
    response_code: int | None = None


@dataclass(frozen=True, slots=True)
class CallFacts:
    """One call, flattened into what an export service asks for."""

    call_id: int
    #: the talkgroup *number* on the radio system (``talkgroups.tgid``), not our row id
    talkgroup: int
    #: the transmitting unit's radio id, or ``None`` when the recorder never resolved one
    radio_id: int | None
    started_at: datetime
    duration_s: float
    frequency_hz: int | None
    emergency: bool
    #: the other talkgroups a patched transmission was also heard on
    patched: list[int] = field(default_factory=list)
    #: the **patch's own id** — the temporary supergroup dispatch created, ``patches.patch_id``.
    #: It is emphatically not one of the members and it is not what ``talkgroup`` holds: a
    #: patched call is recorded under its *first member* (``app/ingest/parser.py``: "the
    #: primary talkgroup of a patch is its first member"), so the patch id has to be carried
    #: separately or it is lost.  Broadcastify's ``tg`` wants it **first**, ahead of the
    #: members (:func:`app.exporters.broadcastify.format_talkgroup`).  ``None`` on the
    #: overwhelming majority of calls, which are not patched at all.
    patch_id: int | None = None
    #: the transmitting **radio's** alias — the P25 over-the-air talker alias the recorder
    #: decoded, else the alias an operator gave that unit in the directory.  This is what
    #: both services' "source alias" fields mean; it is emphatically *not* the talkgroup
    #: name, and it is ``None`` far more often than not (only the push path carries a
    #: talker alias at all).  Callers omit the field entirely when this is ``None``.
    radio_alias: str | None = None

    @property
    def start_epoch(self) -> int:
        """Call start as whole Unix seconds — the form both services take."""
        moment = self.started_at if self.started_at.tzinfo else self.started_at.replace(tzinfo=UTC)
        return int(moment.timestamp())

    @property
    def stop_epoch(self) -> int:
        return self.start_epoch + int(round(self.duration_s))


def facts_for(session: Session, call: Call) -> CallFacts | None:
    """Resolve ``call`` into :class:`CallFacts`, or ``None`` when it has no talkgroup.

    ``None`` is the caller's cue to skip the row: neither service can place a call that is
    not on a talkgroup.
    """
    talkgroup = session.get(Talkgroup, call.talkgroup_id) if call.talkgroup_id else None
    if talkgroup is None:
        return None
    radio = session.get(Radio, call.radio_id) if call.radio_id else None
    patched: list[int] = []
    patch_id: int | None = None
    if call.patch_id:
        patch = session.get(Patch, call.patch_id)
        if patch is not None:
            # ``call.patch_id`` is the ``patches`` row's primary key; ``patch.patch_id`` is
            # the supergroup number dispatch announced, which is the one Broadcastify wants
            # leading ``tg``.  The two are easy to confuse and are never the same number.
            patch_id = int(patch.patch_id)
            patched = [int(t) for t in (patch.member_tgids or ()) if int(t) != int(talkgroup.tgid)]
    # The OTA talker alias the radio itself announced wins over the alias we gave the unit
    # in the directory: it is what the service's source-alias field is for, and it is the
    # radio's own claim about who is talking.  ``talkgroups.alias`` is deliberately NOT a
    # candidate — the talkgroup name is not the name of the unit transmitting on it.
    radio_alias = (call.talker_alias or "").strip() or (
        (radio.alias or "").strip() if radio is not None else ""
    )
    return CallFacts(
        call_id=int(call.id),
        talkgroup=int(talkgroup.tgid),
        radio_id=int(radio.radio_id) if radio is not None else None,
        started_at=call.started_at,
        duration_s=float(call.duration_s or 0),
        frequency_hz=int(call.frequency_hz) if call.frequency_hz else None,
        emergency=bool(call.emergency),
        patched=patched,
        patch_id=patch_id,
        radio_alias=radio_alias or None,
    )


class ExportClient(Protocol):
    """The two-method contract the worker drives.  Implementations live beside this file."""

    #: the ledger's ``service`` value, e.g. ``"openmhz"``
    service: str

    def configured(self) -> bool:
        """Are the credentials this client needs actually present?

        A client that answers ``False`` is skipped entirely — including its discovery pass
        — so turning a switch on without setting the environment variables does nothing at
        all rather than filling the ledger with failures.
        """
        ...

    def test_credentials(self) -> dict[str, object]:
        """Check as much of the configuration as can be checked *without publishing a call*.

        Returns ``{"ok": bool, "checked": str, "detail": str}``.  ``checked`` is prose and
        it matters: neither service offers a credential-validation endpoint, so an
        implementation says in that field exactly what it was able to prove — and, by
        implication, what a green answer does not prove.  An implementation must never
        upload anything from here.
        """
        ...

    def upload(self, facts: CallFacts, audio: bytes) -> UploadResult:
        """Send one call.  Returns the service's identifier for it and what it answered.

        Raises :class:`ExportError` for something worth retrying and
        :class:`PermanentExportError` for something that never will be; :class:`SkipCall`
        for an answer that is neither.  All three carry the same ``response`` /
        ``response_code`` a :class:`UploadResult` would, so the worker writes the service's
        own words to the ledger whichever way the attempt went.
        """
        ...
