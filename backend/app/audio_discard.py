"""The audio discard pass — deleting the recordings the **Record** toggle said not to keep.

A talkgroup with ``record`` off is not a talkgroup keyup ignores.  The call is ingested,
its metadata is kept for ever, it is transcribed when ``transcribe`` is on and it is
uploaded when an export service selects it.  What it does not get is a file on disk
afterwards, and this pass is what takes the file away.

Why a *pass* and not a delete at the end of ingest: at ingest time nothing that wants the
audio has had it yet.  The transcriber has not run, the exporters have not discovered the
call, and the audio-processing job has not written its processed copy.  Deleting there
would be deleting the input to the two jobs the operator did ask for.  So ingest only
writes down the decision (``calls.audio_policy = 'discard'``) and this pass collects the
rows once every reader is finished:

* **the grace** — ``ingested_at`` at least :data:`GRACE_SECONDS` old.  Export *discovery*
  runs on its own poll and writes the ``call_exports`` ledger rows; a call younger than
  the grace may simply not have been discovered yet, and "no pending export row" would be
  read as "nothing wants this" when it means "nobody has looked".  Two minutes is several
  discovery polls at any sane setting;
* **the transcript** — ``transcript_status`` has reached one of its terminal values.
  ``skipped`` is a terminal value, which is what makes the both-off case immediate: a
  talkgroup with ``record`` and ``transcribe`` both off never queues a job at all, so its
  calls are eligible the first time this pass runs after the grace;
* **the exports** — no ``call_exports`` row for the call is still ``pending``.  An upload
  that is queued, backing off or mid-flight needs the bytes; one that is ``uploaded``,
  ``skipped`` or ``failed`` is done with them;
* **the star** — a starred call keeps its audio whatever its talkgroup's Record toggle or
  its content says, as it does under retention.  The rows are read
  ``FOR UPDATE SKIP LOCKED``, so a star written while this pass runs either lands first and
  is seen, or waits for the pass and stars a call whose audio had already gone.

Then the file goes — the original *and* the processed copy beside it, through
:mod:`app.retention`'s own helpers, because "delete this call's audio" is one behaviour and
having two implementations of it is how an archive ends up with orphaned ``.proc.mp3``
files.  The row keeps everything else: ``duration_s``, the transcript, the metadata, its
place in every count.  ``audio_state`` becomes ``discarded`` — beside ``purged`` rather
than reusing it, because "expired after its retention window" and "was never meant to be
kept" are different answers to "where did the audio go".

One more thing has to be taken away with the file: a **queued audio-processing job** would
otherwise run afterwards and write a processed copy of a recording that no longer exists —
or, worse, of one that still does at the moment it is claimed.  So the pass cancels those
the same way reconciliation cancels the loser's (``app.ingest.reconcile._cancel_audio_jobs``):
``queued`` rows only, since a ``running`` job is mid-ffmpeg and the file it writes is
cleaned up by the next pass rather than pulled out from under it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import activity
from app.audio import prune_empty_dirs
from app.config import Settings
from app.models import AudioJob, Call, CallExport
from app.retention import RetentionReport, _drop_processed, _resolve_audio
from app.runtime_settings import effective_settings

log = logging.getLogger("app.audio_discard")

__all__ = ["GRACE_SECONDS", "BATCH_SIZE", "DiscardReport", "run_discard", "eligible_query"]

#: How long a call is left alone after ingest before it may be discarded.  It exists for
#: the exporters: discovery runs on its own poll and this pass must not read "no ledger row
#: yet" as "no service wants this".  Two minutes is several discovery cycles at any
#: interval an operator would set, and it costs an archive nothing — the file is deleted
#: two minutes later than it might have been.
GRACE_SECONDS = 120

#: Rows per pass.  The same batch the retention unlink uses: big enough that a backlog
#: drains in minutes at a pass a minute, small enough that one transaction never holds a
#: lock on a meaningful slice of ``calls``.
BATCH_SIZE = 500

#: The ``transcript_status`` values that mean the transcriber is finished with this call —
#: whether it produced words, produced none, was never going to (``skipped``: the
#: talkgroup's ``transcribe`` is off, or the call was too short) or gave up.  Anything else
#: (``pending``, ``queued``, ``running``, ``parked``) is a reader that still needs the file.
TERMINAL_TRANSCRIPT_STATUSES = ("done", "empty", "skipped", "failed")


@dataclass
class DiscardReport:
    """What one pass did.  ``discarded`` is what the activity line and the stats count."""

    examined: int = 0
    discarded: int = 0
    bytes_freed: int = 0
    audio_jobs_cancelled: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "examined": self.examined,
            "discarded": self.discarded,
            "bytes_freed": self.bytes_freed,
            "audio_jobs_cancelled": self.audio_jobs_cancelled,
            "missing": self.missing,
            "errors": self.errors[:10],
        }


def eligible_query(now: datetime, *, limit: int = BATCH_SIZE):  # noqa: ANN201 - a Select
    """The rows this pass may delete the audio of, oldest first.

    Written as one statement so that "which calls are eligible" is a single readable thing
    and the tests can assert against it without running the deletes.
    """
    pending_export = (
        select(CallExport.id)
        .where(CallExport.call_id == Call.id, CallExport.status == "pending")
        .exists()
    )
    return (
        select(Call)
        .where(
            Call.audio_policy == "discard",
            Call.audio_state == "present",
            Call.starred.is_(False),
            Call.ingested_at < now - timedelta(seconds=GRACE_SECONDS),
            Call.transcript_status.in_(TERMINAL_TRANSCRIPT_STATUSES),
            ~pending_export,
        )
        .order_by(Call.ingested_at)
        .limit(limit)
        .with_for_update(of=Call, skip_locked=True)
    )


def _cancel_audio_jobs(session: Session, call_ids: list[int]) -> int:
    """Cancel the queued audio-processing passes for the calls whose audio just went.

    ``queued`` only — the same rule :func:`app.ingest.reconcile._cancel_audio_jobs` uses.
    A ``running`` job is already inside ffmpeg with the file open; interrupting it buys
    nothing, and the processed copy it writes is caught by the next pass, which finds the
    row still ``discard``/``present`` only if the original survived too.
    """
    if not call_ids:
        return 0
    return int(
        session.execute(
            update(AudioJob)
            .where(AudioJob.call_id.in_(call_ids), AudioJob.status == "queued")
            .values(status="cancelled", finished_at=datetime.now(UTC))
        ).rowcount
        or 0
    )


def run_discard(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    limit: int = BATCH_SIZE,
) -> DiscardReport:
    """One pass.  Does **not** commit — the caller owns the transaction.

    Returns a :class:`DiscardReport` whether or not anything was found; a pass that finds
    nothing writes no activity row and touches nothing, which is what it does for ever on a
    deployment that has never turned Record off.
    """
    # Runtime settings, so an archive path set in Admin is an allowed root here too.
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)
    report = DiscardReport()

    calls = list(session.scalars(eligible_query(now, limit=limit)))
    report.examined = len(calls)
    if not calls:
        return report

    # The retention pass's own report object, so its two helpers can be reused verbatim
    # rather than reimplemented here; its errors are folded into ours below.
    unlink_report = RetentionReport()
    discarded: list[int] = []
    for call in calls:
        # The processed copy first: it is derived data and the one that is easy to orphan.
        report.bytes_freed += _drop_processed(call, settings, unlink_report)
        path = _resolve_audio(call, settings)
        if path is None:
            # A ``file_path`` that escaped AUDIO_ROOT is never unlinked, here or in
            # retention.  The row is left as it is so the next pass sees it again and
            # the operator sees the error rather than a silently forgotten call.
            report.errors.append(f"call {call.id}: file_path is outside the archive; not unlinked")
            continue
        try:
            if path.is_file():
                report.bytes_freed += path.stat().st_size
                path.unlink()
            else:
                # Nothing to delete — an already-moved or never-written file.  Still marked
                # ``discarded``: the fact being recorded is "this call's audio is not kept",
                # and leaving the row at ``present`` would have the API promise a recording
                # that is not there.
                report.missing += 1
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the pass
            report.errors.append(f"unlink {path}: {exc}")
            continue
        prune_empty_dirs(path.parent, settings)
        discarded.append(call.id)

    report.errors.extend(unlink_report.errors)
    if not discarded:
        return report

    report.audio_jobs_cancelled = _cancel_audio_jobs(session, discarded)
    session.execute(
        update(Call)
        .where(Call.id.in_(discarded), Call.starred.is_(False))
        .values(
            audio_state="discarded",
            audio_archived_at=now,
            # The derivative went with the original; the column must not outlive it.
            processed_path=None,
            # ``duration_s`` is deliberately untouched — every count, every statistic and
            # the call's own row still describe a transmission that happened.  The size is
            # nulled because it is a fact about a file that no longer exists.
            file_size=None,
        )
        .execution_options(synchronize_session=False)
    )
    report.discarded = len(discarded)

    # One line per batch, not per call: a channel with Record off produces a few thousand
    # of these a day and an activity log with one row each is a log nobody can read.
    activity.record(
        session,
        source="system",
        level="info",
        kind="audio.discarded",
        message=(
            f"discarded the audio of {report.discarded} call(s) on channels with Record off"
            f" ({report.bytes_freed / 1_048_576:.1f} MiB)"
        ),
        meta=report.as_dict(),
    )
    log.info("audio discard: %s", report.as_dict())
    return report
