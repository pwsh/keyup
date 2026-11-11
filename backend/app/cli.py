"""``keyup`` command line interface (typer).

Every command opens its own session and prints a compact JSON summary, so the same
commands are usable by hand, from ``docker compose exec`` and from a script.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import func, select

from app.backfill_priority import DEFAULT_BATCH_SIZE as PRIORITY_BATCH_SIZE
from app.config import get_settings
from app.flag_hallucinations import DEFAULT_BATCH_SIZE as HALLUCINATION_BATCH_SIZE
from app.models import Agency, Call, System, Talkgroup
from app.refile_audio import DEFAULT_BATCH_SIZE as REFILE_BATCH_SIZE
from app.repair import DEFAULT_BATCH_SIZE, PHANTOM_SYSTEM_NAME, merge_phantom_system
from app.repair_fragment_duplicates import DEFAULT_BATCH_SIZE as FRAGMENT_BATCH_SIZE
from app.repair_fragment_duplicates import DEFAULT_REQUEUE_PRIORITY as FRAGMENT_REQUEUE_PRIORITY
from app.repair_hash_duplicates import DEFAULT_MAX_SKEW_SECONDS as FARHASH_MAX_SKEW_S
from app.repair_self_duplicates import DEFAULT_BATCH_SIZE as SELFDUP_BATCH_SIZE
from app.repair_timestamps import DEFAULT_BATCH_SIZE as TIMESTAMP_BATCH_SIZE
from app.repair_timestamps import DEFAULT_REGROUP_BATCH_SIZE, DEFAULT_TOLERANCE_SECONDS

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Keyup sdrtrunk call archive tooling.",
)
db_app = typer.Typer(help="Database migrations.")
ingest_app = typer.Typer(help="Scan, watch and repair the recording archive.")
playlist_app = typer.Typer(help="Import an sdrtrunk playlist or a talkgroup CSV.")
gaps_app = typer.Typer(help="Recorder gap detection.")
retention_app = typer.Typer(help="Retention: expire audio, calls, radio events and the activity log.")
user_app = typer.Typer(help="API users (sign-in accounts).")
token_app = typer.Typer(help="API tokens for machine access.")
activity_app = typer.Typer(help="The operational activity log of the pipeline.")
transcripts_app = typer.Typer(help="Stored transcripts: quality repairs and backfills.")
bcfy_app = typer.Typer(help="Broadcastify feed-owner statistics (docs/BCFY_STATS.md).")
sites_app = typer.Typer(help="Sites: the control-channel message registry.")
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")
app.add_typer(playlist_app, name="playlist")
app.add_typer(gaps_app, name="gaps")
app.add_typer(retention_app, name="retention")
app.add_typer(user_app, name="user")
app.add_typer(token_app, name="token")
app.add_typer(activity_app, name="activity")
app.add_typer(transcripts_app, name="transcripts")
app.add_typer(bcfy_app, name="bcfy-stats")
app.add_typer(sites_app, name="sites")

log = logging.getLogger("app.cli")
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _session():  # noqa: ANN202
    from app.db import SessionLocal

    return SessionLocal()


def _echo(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


@app.callback()
def _main() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
    )


# --------------------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="target revision")] = "head",
) -> None:
    """Run ``alembic upgrade``.  This is what the ``api`` container runs at start."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND_DIR,
        check=False,
    )
    raise typer.Exit(result.returncode)


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument(help="target revision")]) -> None:
    """Run ``alembic downgrade``."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "downgrade", revision],
        cwd=BACKEND_DIR,
        check=False,
    )
    raise typer.Exit(result.returncode)


# --------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------


@ingest_app.command("scan")
def ingest_scan(
    path: Annotated[Path | None, typer.Argument(help="tree to scan (default: AUDIO_ROOT)")] = None,
    workers: Annotated[int, typer.Option("--workers", "-w", help="worker threads")] = 0,
) -> None:
    """Walk a tree and ingest everything the ledger has not already settled."""
    from app.db import SessionLocal
    from app.ingest.service import ingest_tree

    settings = get_settings()
    root = Path(path) if path is not None else Path(settings.audio_root)
    summary = ingest_tree(
        SessionLocal,
        root,
        workers=workers or settings.ingest_workers,
        settings=settings,
        audio_root=settings.audio_root,
    )
    _echo(
        {
            "run_id": summary.run_id,
            "root": summary.root,
            "files_seen": summary.files_seen,
            "files_new": summary.files_new,
            "files_duplicate": summary.files_duplicate,
            "files_skipped": summary.files_skipped,
            "files_failed": summary.files_failed,
            "elapsed_s": round(summary.elapsed_s, 2),
            "errors": summary.errors[:10],
        }
    )


@ingest_app.command("watch")
def ingest_watch(
    path: Annotated[Path | None, typer.Argument(help="tree to watch (default: AUDIO_ROOT)")] = None,
) -> None:
    """Watch AUDIO_ROOT with inotify and rescan periodically.  Blocks until stopped."""
    from app.ingest.watcher import run_watcher

    run_watcher(get_settings(), root=Path(path) if path is not None else None)


@ingest_app.command("status")
def ingest_status_cmd() -> None:
    """Counts: calls, directory, ledger by status, transcription queue, last run.

    ``force=True``: the API caches these for a minute, but a person who typed the command
    asked for the number now.
    """
    from app.ingest.service import ingest_status

    session = _session()
    try:
        _echo(ingest_status(session, force=True))
    finally:
        session.close()


@ingest_app.command("retry-failed")
def ingest_retry_failed(
    limit: Annotated[int, typer.Option("--limit", help="most files to retry")] = 1000,
) -> None:
    """Re-ingest every path the ledger marks ``failed``."""
    from app.db import SessionLocal
    from app.ingest.service import retry_failed

    summary = retry_failed(SessionLocal, settings=get_settings(), limit=limit)
    _echo(
        {
            "files_seen": summary.files_seen,
            "files_new": summary.files_new,
            "files_duplicate": summary.files_duplicate,
            "files_skipped": summary.files_skipped,
            "files_failed": summary.files_failed,
            "elapsed_s": round(summary.elapsed_s, 2),
            "errors": summary.errors[:10],
        }
    )


# --------------------------------------------------------------------------------------
# playlist
# --------------------------------------------------------------------------------------


@playlist_app.command("import")
def playlist_import_cmd(
    file: Annotated[Path, typer.Argument(help="sdrtrunk default.xml or a talkgroup CSV")],
    system: Annotated[
        str | None, typer.Option("--system", help="system name (default: the only one)")
    ] = None,
) -> None:
    """Import aliases, groups and colors into ``talkgroups`` and ``radios``."""
    from app.ingest.playlist_import import import_file

    session = _session()
    try:
        summary = import_file(session, file, system=system)
        session.commit()
        _echo(summary.__dict__)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# gaps
# --------------------------------------------------------------------------------------


@gaps_app.command("detect")
def gaps_detect(
    minutes: Annotated[
        int | None, typer.Option("--minutes", help="gap threshold (default: GAP_MINUTES)")
    ] = None,
    days: Annotated[int, typer.Option("--days", help="only look this far back; 0 = all")] = 0,
) -> None:
    """Find stretches with no calls on a system that normally has traffic."""
    from app.ingest.gaps import detect_gaps

    since = datetime.now(UTC) - timedelta(days=days) if days else None
    session = _session()
    try:
        summary = detect_gaps(session, settings=get_settings(), since=since, gap_minutes=minutes)
        session.commit()
        _echo(summary.__dict__)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------------------


@retention_app.command("run")
def retention_run(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="count what would go; delete nothing")
    ] = False,
) -> None:
    """Run one retention pass now — the same one the nightly CRON_RETENTION job runs.

    Exists for the pass that is not like the others: turning on a window for the first time.
    The nightly job is the right place for a day's worth of expiry, but an operator who has
    just set ``activity_retention_days`` on a table holding millions of rows past the cutoff
    wants to watch that first run rather than discover it at 03:20, and wants ``--dry-run``
    first to see the count before anything is deleted.
    """
    from app.retention import apply_retention

    session = _session()
    try:
        report = apply_retention(session, settings=get_settings(), dry_run=dry_run)
        _echo(report.as_dict())
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------------------


reconcile_app = typer.Typer(
    help="Cross-recorder reconciliation: the batch pass, and its repairs.",
    invoke_without_command=True,
)
app.add_typer(reconcile_app, name="reconcile")


@reconcile_app.callback(invoke_without_command=True)
def reconcile_cmd(
    ctx: typer.Context,
    since: Annotated[
        str | None,
        typer.Option("--since", help="only calls ingested at or after this ISO timestamp"),
    ] = None,
    system: Annotated[
        str | None, typer.Option("--system", help="restrict to one system by name")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="report the pairs and change nothing")
    ] = False,
    commit_every: Annotated[
        int | None,
        typer.Option("--commit-every", help="candidates per committed transaction"),
    ] = None,
) -> None:
    """Match the two recorders' copies of the same transmissions and keep the better one.

    The batch counterpart of what ingest does inline (docs/RECONCILIATION.md): it is how a
    backlog bulk-uploaded days after the live feed finds its twin.  Idempotent — a second
    run over the same window changes nothing — so it is safe to schedule and safe to repeat
    by hand, and that is also what lets it commit a chunk at a time
    (``RECONCILE_BATCH_COMMIT_EVERY``, or ``--commit-every``) instead of holding the whole
    window's rows and locks in one transaction while an upload waits behind it.

    ``keyup reconcile repair-fragments`` re-judges the duplicates the older, looser rules hid.
    """
    if ctx.invoked_subcommand is not None:
        return
    from app.ingest.reconcile import reconcile_batch

    when = None
    if since:
        when = datetime.fromisoformat(since)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
    session = _session()
    try:
        report = reconcile_batch(
            session,
            get_settings(),
            since=when,
            system=system,
            dry_run=dry_run,
            commit_every=commit_every,
        )
        if dry_run:
            session.rollback()
        else:
            session.commit()
        _echo(report.as_dict())
    finally:
        session.close()


def _cli_moment(value: str | None, option: str) -> datetime | None:
    """``YYYY-MM-DD`` or an ISO timestamp; a naive one is UTC."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        typer.echo(f"{option} is not a date: {value}", err=True)
        raise typer.Exit(code=2) from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@reconcile_app.command("repair-fragments")
def reconcile_repair_fragments_cmd(
    since: Annotated[
        str | None,
        typer.Option("--since", help="only duplicates started at or after this date (YYYY-MM-DD or ISO)"),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="only duplicates started before this date (YYYY-MM-DD or ISO)"),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="only read and count; write nothing")
    ] = False,
    batch: Annotated[
        int, typer.Option("--batch", help="duplicates examined per committed transaction")
    ] = FRAGMENT_BATCH_SIZE,
    requeue_priority: Annotated[
        str,
        typer.Option(
            "--requeue-priority",
            help="band for re-queued transcriptions: live, backlog, bulk, urgent or 1-999",
        ),
    ] = FRAGMENT_REQUEUE_PRIORITY,
) -> None:
    """Free the calls the older, looser duplicate rules hid as duplicates of a transmission they were not.

    ``fragment inside`` counted a short key-up that started after a longer call ended as
    inside it; a shared traffic frequency joined calls on two different talkgroups; and an
    identical ``sha256`` joined voiceless recordings across talkgroups.  Every duplicate
    ruled on one of those is judged again with the fixed rules, and the ones they refuse
    are made primaries again: re-offered to reconciliation against their other neighbours,
    put back in their conversation, their transcription queued at ``--requeue-priority``
    (the backlog band by default) and, while audio processing is on (the Admin page's
    switch, falling back to ``AUDIO_PROCESS_ENABLED``), an audio pass.  The
    export ledger is released only for calls inside ``EXPORT_MAX_AGE_MINUTES``; older calls
    are never published because of this repair.

    Fields ``merge_metadata`` filled on a primary from a now-separated call are counted in
    ``merge_side_effects``, not undone.  Committed every ``--batch`` rows, safe to interrupt
    and to run again — a second run finds nothing to repair.
    """
    from app.ingest.flow import BadPriority, parse_priority
    from app.repair_fragment_duplicates import repair_fragment_duplicates

    try:
        parse_priority(requeue_priority)
    except BadPriority as exc:
        typer.echo(f"--requeue-priority: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    def progress(event: dict[str, Any]) -> None:
        typer.echo(
            "  through call {cursor}: examined {examined}, refused {refused}, repaired "
            "{repaired}, re-matched {rematched}, queued {queued}".format(**event),
            err=True,
        )

    session = _session()
    try:
        report = repair_fragment_duplicates(
            session,
            get_settings(),
            since=_cli_moment(since, "--since"),
            until=_cli_moment(until, "--until"),
            dry_run=dry_run,
            batch=batch,
            requeue_priority=requeue_priority,
            progress=progress,
        )
        _echo(report.as_dict())
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# repairs
# --------------------------------------------------------------------------------------


@app.command("fix-upload-system")
def fix_upload_system(
    name: Annotated[
        str, typer.Option("--name", help="the phantom system's name")
    ] = PHANTOM_SYSTEM_NAME,
    apply: Annotated[
        bool, typer.Option("--apply", help="actually write it; without this nothing is kept")
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="rows repointed per committed transaction")
    ] = DEFAULT_BATCH_SIZE,
) -> None:
    """Fold a system invented from a fallback label back into the real one.

    The push path used to name a system ``upload`` whenever a recorder sent no
    ``systemLabel``, which grew a second system holding duplicate ``Central`` and
    ``Eastside`` sites beside the real one.  This folds the duplicates into their
    counterparts, re-parents the sites that have none (a real neighbor site under the
    wrong parent is still a real site), moves every call, radio event, gap and observation
    across, and deletes the empty phantom.

    Without ``--apply`` it only reads: it prints which rows have twins, which would be
    re-parented, and how many rows each referring table would have repointed.  With
    ``--apply`` it repoints in batches of ``--batch-size``, committing each one and
    printing progress to stderr, so the live feed keeps flowing beside it.  Safe to
    interrupt with Ctrl-C and safe to run again — a half-done repair resumes and a
    finished one finds no phantom to fold.
    """

    def progress(event: dict[str, Any]) -> None:
        typer.echo(
            f"  {event['table']}.{event['column']}: "
            f"{event['rows_done']}/{event['rows_total']} (batch {event['batch']})",
            err=True,
        )

    session = _session()
    try:
        report = merge_phantom_system(
            session,
            name=name,
            dry_run=not apply,
            batch_size=batch_size,
            on_batch=progress if apply else None,
        )
        _echo(report)
    finally:
        session.close()


@app.command("refile-calls")
def refile_calls_cmd(
    match: Annotated[
        str,
        typer.Option(
            "--match",
            help="SQL LIKE pattern matched against calls.file_name — %% is the wildcard",
        ),
    ],
    to_system: Annotated[
        str, typer.Option("--to-system", help="name of the system the calls belong to (must exist)")
    ],
    to_site: Annotated[
        str, typer.Option("--to-site", help="name of the site under it (must exist)")
    ],
    apply: Annotated[
        bool, typer.Option("--apply", help="actually write it; without this nothing is kept")
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="calls repointed per committed transaction")
    ] = REFILE_BATCH_SIZE,
    drop_empty_source_site: Annotated[
        bool,
        typer.Option(
            "--drop-empty-source-site",
            help="also delete a source site the move itself emptied, in the same run",
        ),
    ] = False,
) -> None:
    """Re-file calls a recorder archived under the wrong system, matched by filename.

    A recorder labeled a school district's own P25 traffic ``Example System``, so 3,803
    calls whose filenames all say ``EXAMPLE_SCHOOL_DISTRICT`` were
    filed under the county network's ``Eastside`` site — and, because a talkgroup is keyed on
    ``(system_id, tgid)``, merged into the county's talkgroups and radios that happen to
    carry the same numbers.  The filename is the only place the truth survived, which is
    why the slice is chosen by ``LIKE`` on it and by nothing else.

    Every matching call is moved onto ``--to-system`` / ``--to-site``, splitting its
    talkgroup and radio off onto twins under the target system (created when they are
    missing, seeded from the source row; an existing twin is used untouched).  Derived
    per-pair rows — affiliations, radio events, conversations — move only when they are
    exclusively about the moved calls, and ``call_count`` is recomputed on both sides.
    **No call is ever deleted.**

    Without ``--apply`` it only reads and prints the plan.  With ``--apply`` it moves the
    calls in batches of ``--batch-size``, committing each one and printing progress to
    stderr, so the live feed keeps flowing beside it.  Safe to interrupt with Ctrl-C and
    safe to run again: work outstanding is "matches and is not yet under the target", so a
    half-done refile resumes and a finished one finds nothing to do.

    If the move empties the source site the summary says so and leaves it in place — the
    Sites page has a delete button for exactly that.  ``--drop-empty-source-site`` does it
    here instead, and still refuses when anything but calls references the site.
    """
    from app.refile import RefileTargetMissing, refile_calls

    def progress(event: dict[str, Any]) -> None:
        typer.echo(
            f"  calls: {event['rows_done']}/{event['rows_total']} (batch {event['batch']})", err=True
        )

    session = _session()
    try:
        report = refile_calls(
            session,
            match=match,
            to_system=to_system,
            to_site=to_site,
            dry_run=not apply,
            batch_size=batch_size,
            drop_empty_source_site=drop_empty_source_site,
            on_batch=progress if apply else None,
        )
        _echo(report)
    except RefileTargetMissing as error:
        _echo(
            {
                "match": match,
                "to_system": to_system,
                "to_site": to_site,
                "dry_run": not apply,
                "changed": False,
                "detail": str(error),
            }
        )
        raise typer.Exit(2) from error
    finally:
        session.close()


@app.command("fix-upload-timestamps")
def fix_upload_timestamps_cmd(
    apply: Annotated[
        bool, typer.Option("--apply", help="actually write it; without this nothing is kept")
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="rows rewritten per committed transaction")
    ] = TIMESTAMP_BATCH_SIZE,
    regroup_batch_size: Annotated[
        int,
        typer.Option(
            "--regroup-batch-size",
            help="calls regrouped per committed transaction; smaller, because each one "
            "holds its talkgroup's conversation lock until the batch commits",
        ),
    ] = DEFAULT_REGROUP_BATCH_SIZE,
    tolerance: Annotated[
        float,
        typer.Option(
            "--tolerance",
            help="seconds the stored time may differ from the filename's before it is damage",
        ),
    ] = DEFAULT_TOLERANCE_SECONDS,
    reconcile: Annotated[
        bool,
        typer.Option("--reconcile/--no-reconcile", help="re-run reconciliation over the repaired rows"),
    ] = True,
    regroup: Annotated[
        bool,
        typer.Option("--regroup", help="also rebuild the repaired calls' conversations"),
    ] = False,
) -> None:
    """Put pushed calls back on the day they were recorded, from their own filenames.

    The backlog migration pushes an sdrtrunk recording without a ``dateTime`` and lets the
    server read the stamp out of ``audioName``.  Pre-2025 sdrtrunk names have no underscore
    after the time, the parser rejected them, and the push path fell through to *now minus
    the duration* — so a million calls were filed on their upload date, then deduped and
    threaded against each other on those invented times (docs/MIGRATION.md).

    Three phases, each batched and committed per batch so the live feed keeps flowing
    (``--regroup``'s batch is smaller by default: it is the only one holding a lock a live
    call waits on):
    the timestamps are recomputed from the filename exactly as the file parser does it and
    every ruling made on the old ones is cleared; reconciliation runs again over the
    repaired rows so genuine duplicates re-link on the true times; and with ``--regroup``
    the conversations built over the fiction are rebuilt in chronological order.

    Without ``--apply`` it only reads: it reports what it would rewrite, what already
    agrees with its filename, and which names it still cannot parse (those are left
    exactly as they are).  Safe to interrupt with Ctrl-C and safe to run again — each phase
    recognizes its own leftovers and resumes.  Deploy the parser fix first; a repair run
    against the old parser finds nothing to do.

    **Pause the scheduled reconcile pass while this runs.**  ``RECONCILE_ENABLED`` /
    ``RECONCILE_MINUTES`` drive a worker job that reconciles a recent window in one
    transaction, and it takes the same per-talkgroup conversation locks this command takes
    — over a different set of rows, in a different order.  Both sides now sort the locks
    they can predict and retry the batches they lose, so leaving it on costs retries rather
    than the run; turning it off for the duration costs nothing at all, and the repair
    reconciles the repaired rows itself.
    """
    from app.repair_timestamps import fix_upload_timestamps

    def progress(event: dict[str, Any]) -> None:
        detail = " ".join(f"{k}={v}" for k, v in event.items() if k not in {"phase", "batch"})
        typer.echo(f"  {event['phase']} batch {event['batch']}: {detail}", err=True)

    session = _session()
    settings = get_settings()
    if apply and settings.reconcile_enabled:
        typer.echo(
            f"  note: the scheduled reconcile pass is on (every {settings.reconcile_minutes} "
            "min) and competes with this repair for the same per-talkgroup locks. "
            "Set RECONCILE_ENABLED=false and restart the worker for the duration of "
            "--apply; batches that do collide are retried, so this is a recommendation, "
            "not a requirement.",
            err=True,
        )
    try:
        report = fix_upload_timestamps(
            session,
            settings,
            dry_run=not apply,
            batch_size=batch_size,
            regroup_batch_size=regroup_batch_size,
            tolerance_s=tolerance,
            reconcile=reconcile,
            regroup=regroup,
            on_batch=progress if apply else None,
        )
        _echo(report)
    finally:
        session.close()


@app.command("repair-self-duplicates")
def repair_self_duplicates_cmd(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="only read and count; write nothing"),
    ] = False,
    batch: Annotated[
        int, typer.Option("--batch", help="rows restored per committed transaction")
    ] = SELFDUP_BATCH_SIZE,
) -> None:
    """Free the calls reconciliation ruled duplicates of *themselves*.

    The exact-hash rung applied its exclusion list to the row it found and not to the
    primary that row resolves to, so a call whose bytes were stored twice matched its own
    copy, followed it back to itself, tied with itself, and was written
    ``duplicate_of = <its own id>`` — 18,708 rows on production, every one hidden from
    every listing, taken out of its conversation, and (when its transcript was still
    pending) marked ``skipped`` with its jobs cancelled.

    This clears the self-pointer, records the repair in ``reconcile_reason`` with the bogus
    ruling kept under ``previous``, offers the row to conversation grouping again, and
    requeues a wrongly-skipped transcription in the band the call was ingested in — the
    backlog behind the live feed, not in front of it.  A row with a transcript, or one
    that is ``done`` or ``empty``, is left alone.

    Committed every ``--batch`` rows so the live feed keeps flowing beside it.  Safe to
    interrupt with Ctrl-C and safe to run again: a repaired row no longer points at itself,
    so a second run finds nothing.  Deploy the ``_find`` fix first, or the pass that runs
    next will write the same rows again.
    """
    from app.repair_self_duplicates import repair_self_duplicates

    session = _session()
    try:
        report = repair_self_duplicates(
            session, get_settings(), dry_run=dry_run, batch=batch
        )
        _echo(report.as_dict())
    finally:
        session.close()


@app.command("repair-far-hash-duplicates")
def repair_far_hash_duplicates_cmd(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="only read and count; write nothing"),
    ] = False,
    batch: Annotated[
        int, typer.Option("--batch", help="rows restored per committed transaction")
    ] = SELFDUP_BATCH_SIZE,
    max_skew_s: Annotated[
        float,
        typer.Option(
            "--max-skew-s",
            help="how far apart two copies' starts may be and still be one transmission",
        ),
    ] = FARHASH_MAX_SKEW_S,
) -> None:
    """Free the calls ruled duplicates on an identical hash from another day.

    The exact-hash rung read an identical ``sha256`` as proof of the same transmission
    whatever the time.  Identical bytes only prove the same *recording*, and a short
    recording with no voice in it — a squelch tail, 0.14-2 s — encodes to byte-identical
    MP3 bytes every time, so one hash spreads across hundreds of unrelated calls.  Call
    2250773, a 1.7 s unit call on 2026-09-08, was ruled a duplicate of a 1.7 s group call
    on talkgroup 2 from 2026-08-31 — 927 rows on production, one hash covering 155 of them,
    every one hidden from every listing.

    This clears ``duplicate_of`` on the rows whose primary started more than ``--max-skew-s``
    away, records the repair in ``reconcile_reason`` with the bogus ruling kept under
    ``previous``, offers the row to conversation grouping again, and requeues a
    wrongly-skipped transcription in the band the call was ingested in.

    Committed every ``--batch`` rows, safe to interrupt and safe to run again.  Deploy the
    ``_find`` fix first — otherwise the next pass writes the same rows again.  With the fix
    deployed, the next batch reconciliation *will* re-examine every row freed here and may
    rule some of them duplicates on the interval rung, which is the intent.
    """
    from app.repair_hash_duplicates import repair_far_hash_duplicates

    session = _session()
    try:
        report = repair_far_hash_duplicates(
            session, get_settings(), dry_run=dry_run, batch=batch, max_skew_s=max_skew_s
        )
        _echo(report.as_dict())
    finally:
        session.close()


def _like_substring(text: str) -> str:
    """A plain substring as a SQL ``LIKE`` pattern that means only itself.

    ``%`` and ``_`` in a filename are literal characters, not wildcards, and PostgreSQL's
    default ``LIKE`` escape is the backslash — so escaping those three is the whole job.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@app.command("refile-system")
def refile_system_cmd(
    match: Annotated[
        str,
        typer.Option(
            "--match",
            help="substring of calls.file_name that identifies the slice (literal, not a pattern)",
        ),
    ],
    to_system: Annotated[
        str, typer.Option("--to-system", help="name of the system the calls belong to (must exist)")
    ],
    to_site: Annotated[
        str, typer.Option("--to-site", help="name of the site under it (must exist)")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="report the plan and write nothing")
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="calls repointed per committed transaction")
    ] = REFILE_BATCH_SIZE,
    drop_empty_source_site: Annotated[
        bool,
        typer.Option(
            "--drop-empty-source-site",
            help="also delete a source site the move itself emptied, in the same run",
        ),
    ] = False,
) -> None:
    """Move a mislabeled slice of calls onto the system and site they belong to.

    The same operation as ``refile-calls`` (:mod:`app.refile`) with the interface the
    repair is actually run with: ``--match`` is the **substring** of ``calls.file_name``
    that identifies the slice, taken literally, and the move happens unless ``--dry-run``
    says otherwise.  The one this exists for::

        keyup refile-system \\
          --match EXAMPLE_SCHOOL_DISTRICT_Eastside_District_Schools \\
          --to-system 'EXAMPLE SCHOOL DISTRICT' --to-site Eastside

    3,803 calls a recorder labeled ``Example System``, filed under the county's ``Eastside``
    site and — because a talkgroup is keyed on ``(system_id, tgid)`` — merged into the
    county's talkgroups and radios that happen to carry the same numbers.  Every matching
    call moves; its talkgroup and radio are split off onto twins under the target system,
    created with the same ``tgid`` and, when the move empties the source row (so it was
    only ever the school's channel), its label and tags too — a source row the county keeps
    using gives the twin nothing but the number.  Affiliations, radio events and
    conversations move only when they are exclusively about the moved calls; ``call_count``
    is recomputed on both sides and the Sites page's cached counts are dropped.  **No call
    is ever deleted.**

    Committed in batches, so the live feed keeps flowing beside it; safe to interrupt and
    safe to run again — outstanding work is "matches and is not yet under the target".
    """
    from app.refile import RefileTargetMissing, refile_calls

    pattern = _like_substring(match)

    def progress(event: dict[str, Any]) -> None:
        typer.echo(
            f"  calls: {event['rows_done']}/{event['rows_total']} (batch {event['batch']})", err=True
        )

    session = _session()
    try:
        report = refile_calls(
            session,
            match=pattern,
            to_system=to_system,
            to_site=to_site,
            dry_run=dry_run,
            batch_size=batch_size,
            drop_empty_source_site=drop_empty_source_site,
            on_batch=None if dry_run else progress,
        )
        report["match_substring"] = match
        _echo(report)
    except RefileTargetMissing as error:
        _echo(
            {
                "match": pattern,
                "match_substring": match,
                "to_system": to_system,
                "to_site": to_site,
                "dry_run": dry_run,
                "changed": False,
                "detail": str(error),
            }
        )
        raise typer.Exit(2) from error
    finally:
        session.close()


@app.command("refile-audio")
def refile_audio_cmd(
    apply: Annotated[
        bool, typer.Option("--apply", help="actually move it; without this nothing is touched")
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="rows moved per committed transaction")
    ] = REFILE_BATCH_SIZE,
    source_prefix: Annotated[
        str | None,
        typer.Option(
            "--source-prefix",
            help="only rows whose file_path starts here, e.g. uploads/2026/09/01",
        ),
    ] = None,
) -> None:
    """Move pushed recordings into the partition their own ``started_at`` names.

    The archive files a push under ``uploads/YYYY/MM/DD/`` — and until this branch, that
    date came from the form *before* the recording was parsed, so a backlog migration piled
    972,347 files into ``uploads/2026/09/01`` and 82,174 into ``uploads/2026/08/31``.  Run
    this once after ``fix-upload-timestamps``, which is what put the true 2022-2024 dates on
    those rows in the first place; the layout is the only thing still holding the upload
    date.

    It walks ``calls``, never the directory — enumerating the million-entry directory would
    be the slowest possible way to fix it — and each move is an ``os.rename`` within one
    filesystem plus a column update in the same batch transaction.  The inode is compared
    before and after, so a move is *verified* rather than assumed; a file that is not where
    its row says is reported and left alone; nothing is ever deleted.

    Safe to interrupt and safe to re-run: a moved row now agrees with its own date and is
    counted ``unchanged`` on the next pass, and a row whose file made it across before the
    commit did is recognised and repaired.  Without ``--apply`` it only reads.
    """
    from app.refile_audio import refile_audio

    def progress(report: dict[str, Any]) -> None:
        typer.echo(
            f"  scanned={report['scanned']} moved={report['moved']} "
            f"unchanged={report['unchanged']} missing={report['missing']} "
            f"failed={report['failed']} (through call {report['last_id']})",
            err=True,
        )

    session = _session()
    try:
        _echo(
            refile_audio(
                session,
                get_settings(),
                dry_run=not apply,
                batch_size=batch_size,
                source_prefix=source_prefix,
                on_batch=progress if apply else None,
            )
        )
    finally:
        session.close()


@app.command("backfill-ingest-priority")
def backfill_ingest_priority_cmd(
    apply: Annotated[
        bool, typer.Option("--apply", help="actually write it; without this nothing is kept")
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="rows stamped per committed transaction")
    ] = PRIORITY_BATCH_SIZE,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="stop after examining this many rows (0 = the whole table); a bounded dry run",
        ),
    ] = 0,
) -> None:
    """Give the calls that pre-date ``ingest_priority`` the class they arrived under.

    The column was added with the push path's admission control and is written on every
    recorder push since; the 1.08 million rows before it are NULL.  NULL is not neutral —
    it fails the exporters' live-band guard closed, matches no ``?priority=`` class, and
    looks identical whether the row is live traffic or a decade of sdrtrunk pushed last
    Tuesday — so the one question the column exists to answer is unanswerable for most of
    the archive.

    Two facts still on the rows recover it.  ``ingest_source='trunkd'`` is the live
    recorder and is always ``live``.  ``ingest_source='upload'`` is the generic push,
    shared by live clients and by the archive migration, and the two are told apart by
    lag: ingested more than a day after it was recorded is ``backlog``, under a day is a
    live push that spooled through a network blip.  ``file`` and ``trunk_recorder`` rows
    came off disk with no such evidence and are deliberately left NULL.

    A CLI rather than a migration, and for one reason: an ``UPDATE`` over a million rows
    inside ``alembic upgrade`` holds a lock the live feed waits on for the whole rewrite,
    and a deploy that must finish it before serving a request is a deploy that can fail
    with the API down.  Here it is keyset-paged by id and committed per batch — the live
    feed flows between batches, Ctrl-C costs at most one batch, and re-running resumes,
    because "outstanding" is a NULL a finished row no longer has.

    Without ``--apply`` nothing is written; the counts are what it *would* stamp.
    """
    from app.backfill_priority import backfill_ingest_priority, remaining

    def progress(event: dict[str, Any]) -> None:
        typer.echo(
            f"  batch {event['batch']}: scanned={event['scanned']} live={event['live']} "
            f"backlog={event['backlog']} (through call {event['last_id']})",
            err=True,
        )

    session = _session()
    try:
        before = remaining(session)
        report = backfill_ingest_priority(
            session,
            dry_run=not apply,
            batch_size=batch_size,
            limit=limit,
            on_batch=progress if apply else None,
        )
        report["outstanding_before"] = before
        report["outstanding_after"] = remaining(session)
        _echo(report)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------------------


@transcripts_app.command("flag-hallucinations")
def transcripts_flag_hallucinations_cmd(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--apply", help="count without writing (the default)"),
    ] = True,
    batch: Annotated[
        int, typer.Option("--batch", help="rows examined and committed per transaction")
    ] = HALLUCINATION_BATCH_SIZE,
    since: Annotated[
        str | None,
        typer.Option("--since", help="only calls started on or after this date (YYYY-MM-DD)"),
    ] = None,
) -> None:
    """Relabel stored transcripts that today's hallucination check would have rejected.

    The phrase heuristics used to be an exact-match list, so every caption-credit variant
    whisper invented — "closed captioning provided by WKNO-Memphis.", "captioning provided
    by Aeverine Zinn Digital Media Group" — sailed through as ``good`` and was denormalized
    onto the call.  This applies the current check (the exact list, the credit and sign-off
    pattern families, the broadcast-credit frames, and whatever ``stt_hallucination_patterns``
    holds) to the text already in the database.

    A hit is left exactly as the worker would leave it today: the transcript row keeps its
    text and is relabeled ``noise`` with the reason appended to ``quality_reasons``, and the
    call loses its denormalized ``transcript_text`` and moves to ``transcript_status`` =
    ``empty``.  Nothing is deleted, and re-running is a no-op because an ``empty`` call is
    no longer in scope.

    ``--dry-run`` is the default; pass ``--apply`` to write.  Counts are reported by family
    (``phrase``, ``credit``, ``credit_frame``, ``signoff``, ``custom``) and by individual reason.
    """
    from app.flag_hallucinations import flag_hallucinations
    from app.runtime_settings import effective_settings

    cutoff: datetime | None = None
    if since:
        try:
            cutoff = datetime.fromisoformat(since).replace(tzinfo=UTC)
        except ValueError as exc:
            typer.echo(f"--since is not a date: {since}", err=True)
            raise typer.Exit(code=1) from exc

    def progress(event: dict[str, Any]) -> None:
        typer.echo(
            f"  batch {event['batch']}: scanned={event['scanned']} flagged={event['flagged']}"
            f" (through transcript {event['last_id']})",
            err=True,
        )

    session = _session()
    try:
        settings = effective_settings(session)
        report = flag_hallucinations(
            session,
            dry_run=dry_run,
            batch_size=batch,
            since=cutoff,
            patterns=getattr(settings, "stt_hallucination_patterns", "") or "",
            on_batch=progress,
        )
        _echo(report)
    finally:
        session.close()



# --------------------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------------------


@app.command("seed-agencies")
def seed_agencies_cmd(
    csv_path: Annotated[
        Path | None,
        typer.Option(
            "--csv",
            help="agency CSV (code,name,short_name,kind); default: KEYUP_SEED_AGENCIES",
        ),
    ] = None,
    if_configured: Annotated[
        bool,
        typer.Option(
            "--if-configured",
            help="do nothing (exit 0) when no CSV is named; for the compose migrate step",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="also overwrite seeded rows an operator has renamed here (never touches local ones)",
        ),
    ] = False,
) -> None:
    """Load an agency list into the ``agencies`` reference table.

    The CSV is the deployment's own (``examples/agencies.example.csv`` shows the format),
    named by ``--csv`` or ``KEYUP_SEED_AGENCIES``.  With neither, this is an error — unless
    ``--if-configured`` is passed, which is how the compose ``migrate`` step runs it on
    every deploy without requiring a seed.

    Agencies added in Keyup (``source = 'local'``) are never touched, and neither is a
    seeded row somebody has renamed through the admin API — ``--force`` is the explicit
    "the seeded list wins" button for the second case, which is also how an upstream
    rename lands.
    """
    from app.seed import seed_agencies

    configured = get_settings().seed_agencies_csv.strip()
    path = csv_path or (Path(configured) if configured else None)
    if path is None:
        if if_configured:
            _echo({"agencies": 0, "skipped": "no agency CSV configured (KEYUP_SEED_AGENCIES)"})
            return
        typer.echo("name an agency CSV with --csv or KEYUP_SEED_AGENCIES", err=True)
        raise typer.Exit(code=2)
    if not path.is_file():
        typer.echo(f"no agency CSV at {path}", err=True)
        raise typer.Exit(code=2)

    session = _session()
    try:
        written = seed_agencies(session, path=path, force=force)
        session.commit()
        _echo({"agencies": written, "csv": str(path)})
    finally:
        session.close()


def _read_tgid_file(path: Path) -> list[int]:
    """Tgids from a text file: one or more per line, ``#`` starts a comment."""
    tgids: list[int] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        text = line.split("#", 1)[0].replace(",", " ").strip()
        for word in text.split():
            try:
                tgids.append(int(word))
            except ValueError as exc:
                typer.echo(f"{path}:{number}: not a tgid: {word}", err=True)
                raise typer.Exit(code=2) from exc
    return tgids


@app.command("mark-shared")
def mark_shared_cmd(
    tgids: Annotated[
        list[int] | None, typer.Argument(help="on-air tgids to flag as shared")
    ] = None,
    tgid_file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="read tgids from a text file (one or more per line, # comments)",
        ),
    ] = None,
    system: Annotated[
        str | None, typer.Option("--system", help="system name (default: every system)")
    ] = None,
    agency: Annotated[
        str | None,
        typer.Option(
            "--agency",
            help="agency code to fill a blank agency_code with (default: the shared_agency_code setting)",
        ),
    ] = None,
) -> None:
    """Flag shared dispatch channels ``shared`` and map the blank ones to an agency.

    The shared dispatch and interop channels belong to no department, which used to leave
    them with no ``agency_code`` at all — and therefore invisible to ``?agency=``, which is
    how a consumer asks "what was this agency on?" about the very channel a call went out
    on.  This marks them, so the set is readable from the API instead of hardcoded by every
    consumer.  The agency defaults to the ``shared_agency_code`` runtime setting
    (``SHARED_AGENCY_CODE``); when that is blank too, the channels are only flagged.

    Idempotent, and deliberately timid: a channel that does not exist is reported and
    skipped rather than created, and an ``agency_code`` an operator already set is left
    alone unless it is already the shared code.  Re-running changes nothing.

    ``python -m app.cli mark-shared --system "Example System" 1011 1012``
    ``python -m app.cli mark-shared --file deploy/site/shared-talkgroups.txt``
    """
    from app.runtime_settings import effective_settings

    wanted = list(tgids or [])
    if tgid_file is not None:
        if not tgid_file.is_file():
            typer.echo(f"no tgid file at {tgid_file}", err=True)
            raise typer.Exit(code=2)
        wanted += _read_tgid_file(tgid_file)
    if not wanted:
        typer.echo("name the tgids to mark, as arguments or with --file", err=True)
        raise typer.Exit(code=2)

    session = _session()
    try:
        if agency is None:
            agency = effective_settings(session).shared_agency_code
        agency_code = agency.strip()
        stmt = select(Talkgroup).where(Talkgroup.tgid.in_(sorted(set(wanted))))
        if system:
            stmt = stmt.join(System).where(func.lower(System.name) == system.strip().lower())
        rows = list(session.scalars(stmt))
        agency_exists = bool(agency_code) and session.get(Agency, agency_code) is not None
        flagged, mapped, kept = [], [], []
        for row in rows:
            if not row.shared:
                row.shared = True
                flagged.append(row.tgid)
            if row.agency_code is None and agency_exists:
                row.agency_code = agency_code
                mapped.append(row.tgid)
            elif row.agency_code not in (None, agency_code):
                kept.append({"tgid": row.tgid, "agency_code": row.agency_code})
        session.commit()
        found = {row.tgid for row in rows}
        _echo(
            {
                "requested": sorted(set(wanted)),
                "matched": sorted(found),
                "missing": sorted(set(wanted) - found),
                "newly_shared": sorted(flagged),
                "agency_filled": sorted(mapped),
                "agency_kept": kept,
                "agency_code": agency_code if agency_exists else None,
                "agency_missing": agency_code if agency_code and not agency_exists else None,
                "system": system,
            }
        )
    finally:
        session.close()


@app.command("classify-talkgroups")
def classify_talkgroups_cmd(
    rules: Annotated[
        Path,
        typer.Option(
            "--rules", help="rules CSV (pattern,category,service_tag,agency_code,note); see examples/"
        ),
    ],
    apply: Annotated[
        bool, typer.Option("--apply", help="actually write it; without this nothing is kept")
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="replace values somebody already set, instead of only filling blanks",
        ),
    ] = False,
    system: Annotated[
        str | None, typer.Option("--system", help="restrict to one system by name")
    ] = None,
    radios: Annotated[
        bool,
        typer.Option(
            "--radios",
            help="also set radios.agency_code from the agency each radio is most affiliated with",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="print the plan as JSON instead of a table")
    ] = False,
) -> None:
    """Classify talkgroups from an ordered ``alias regex → category/tag/agency`` file.

    A big system has thousands of talkgroups, few of them categorized and none carrying
    an agency, which leaves ``?agency=`` matching nothing and an outside system matching
    incidents to radio traffic with only time to go on.  This replays the operator's own
    mapping (``--rules``; the format is ``examples/classification.example.csv``) over the
    channels that matter, and it is a file rather than dozens of hand edits so that it can
    be reviewed, repeated after a playlist re-import, and corrected in one place.

    Rules are tried in file order and **the first match wins**.  A talkgroup no rule
    matches is left completely alone — not filed under ``other``, because "nobody has
    classified this" is a different fact from "somebody said it is miscellaneous".

    **Fill-only.**  A column that already has a value is kept and reported, so the RR
    sync's tags and an operator's corrections survive every run.  ``--overwrite`` is the
    deliberate opposite.

    Without ``--apply`` nothing is written: the plan is printed as a table (tgid, alias,
    and the category/service tag/agency each row would gain) and that same plan is what
    ``--apply`` then writes, so what was read is what lands.

    ``--radios`` adds a second pass with no aliases to match on: a radio's agency is
    inferred from ``radio_affiliations`` — the agency of the talkgroups it has actually
    been heard on, by call count — and a radio split evenly between two agencies is left
    alone rather than assigned to the first one alphabetically.  Run it *after* the
    talkgroup pass; it reads the agencies that pass just wrote.
    """
    from app import classify

    session = _session()
    try:
        rule_set = classify.load_rules(rules)
        plan = classify.plan_talkgroups(
            session, rule_set, system=system, overwrite=overwrite
        )
        radio_plan = (
            classify.plan_radios(session, system=system, overwrite=overwrite) if radios else None
        )

        if plan["unknown_agencies"]:
            typer.echo(
                "  refusing to write: the rules name agency codes the agencies table does "
                f"not have ({', '.join(plan['unknown_agencies'])}). Run `keyup seed-agencies`, "
                "or fix the rules file.",
                err=True,
            )
            apply = False

        report: dict[str, Any] = {
            k: v for k, v in plan.items() if k not in {"changes", "unmatched"}
        }
        report["dry_run"] = not apply
        report["rules_file"] = str(rules)
        if radio_plan is not None:
            report.update({k: v for k, v in radio_plan.items() if k != "changes"})

        if as_json:
            report["changes"] = [c.as_dict() for c in plan["changes"]]
            if radio_plan is not None:
                report["radio_changes"] = [c.as_dict() for c in radio_plan["changes"]]
        else:
            _classify_table(plan, radio_plan)

        if apply:
            written = classify.apply_changes(session, plan["changes"])
            report["talkgroups_written"] = written
            if radio_plan is not None:
                report["radios_written"] = classify.apply_radio_changes(
                    session, radio_plan["changes"]
                )
            session.commit()
        else:
            session.rollback()
        _echo(report)
    except (classify.RulesError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    finally:
        session.close()


def _classify_table(plan: dict[str, Any], radio_plan: dict[str, Any] | None) -> None:
    """The dry-run table: one line per talkgroup that would change, then the radios."""
    def cell(change: Any, column: str) -> str:
        """The new value, or the existing one in parentheses when it was kept."""
        if column in change.sets:
            return str(change.sets[column])
        if column in change.kept:
            return f"({change.kept[column]})"
        return ""

    changes = plan["changes"]
    if changes:
        typer.echo(f"{'tgid':>7}  {'alias':<28}  {'category':<9}  {'service tag':<15}  agency")
        for change in changes:
            typer.echo(
                f"{change.tgid:>7}  {(change.alias or '')[:28]:<28}  "
                f"{cell(change, 'category'):<9}  {cell(change, 'service_tag'):<15}  "
                f"{cell(change, 'agency_code')}"
            )
        typer.echo("  (parenthesized values already exist and were kept — use --overwrite to replace)")
    if radio_plan and radio_plan["changes"]:
        typer.echo("")
        typer.echo(f"{'radio':>9}  {'label':<16}  agency  calls/total")
        for change in radio_plan["changes"]:
            typer.echo(
                f"{change.radio_id:>9}  {(change.label or ''):<16}  "
                f"{change.agency_code}  {change.calls}/{change.total}"
            )


@app.command("stats")
def stats() -> None:
    """A quick look at what is in the archive."""
    session = _session()
    try:
        top = session.execute(
            select(Talkgroup.tgid, Talkgroup.alias, Talkgroup.call_count)
            .order_by(Talkgroup.call_count.desc())
            .limit(10)
        ).all()
        span = session.execute(
            select(func.min(Call.started_at), func.max(Call.started_at), func.sum(Call.duration_s))
        ).one()
        _echo(
            {
                "calls": session.execute(select(func.count()).select_from(Call)).scalar_one(),
                "first_call": span[0],
                "last_call": span[1],
                "audio_hours": round(float(span[2] or 0) / 3600, 2),
                "top_talkgroups": [
                    {"tgid": t, "alias": a, "calls": c} for t, a, c in top
                ],
            }
        )
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# audio processing (docs/ARCHITECTURE.md 5.5)
# --------------------------------------------------------------------------------------


@app.command("process-audio")
def process_audio_cmd(
    since: Annotated[
        str | None, typer.Option("--since", help="only calls on or after this date (YYYY-MM-DD)")
    ] = None,
    talkgroup: Annotated[
        int | None, typer.Option("--talkgroup", help="only calls involving this tgid")
    ] = None,
    ids: Annotated[
        str | None, typer.Option("--ids", help="comma-separated call ids")
    ] = None,
    only_tone_flagged: Annotated[
        bool,
        typer.Option(
            "--only-tone-flagged", help="only calls the stored pass heard a tone in"
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="requeue calls that already have a current copy")
    ] = False,
    priority: Annotated[
        int, typer.Option("--priority", help="queue priority; 600 is the backlog band")
    ] = 600,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="requeue the parked failed jobs and queue nothing else"),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="most calls to queue in one run")] = 100_000,
) -> None:
    """Queue calls for the audio-processing pass (§5.5).

    Enqueues at the backlog band by default, so a backfill over the whole archive never
    delays the processing of a call that is arriving right now.  Without ``--force`` only
    calls that have no processed copy or an out-of-date one are queued, which makes the
    command safe to re-run and makes "bump the reprocess version, then run this" the whole
    procedure for applying new settings to the archive.

    ``--retry-failed`` is the other shape: it queues no new work at all and instead puts
    the jobs that parked at ``max_attempts`` back on the queue with a clean attempt count —
    the one-shot for a fault fixed in the code since they parked, such as the empty
    recordings trunkd used to send, which now finish ``done`` with the call marked empty.
    """
    from app.audioproc.jobs import (  # noqa: PLC0415 - numpy
        enqueue_many,
        requeue_failed,
        select_for_reprocess,
    )
    from app.runtime_settings import effective_settings  # noqa: PLC0415

    # Both shapes put rows in ``queued``, and with the stage switched off the worker cancels
    # every queued job on its next idle cycle — so queueing then is only table bloat.
    session = _session()
    try:
        enabled = bool(
            getattr(effective_settings(session, base=get_settings()), "audio_process_enabled", True)
        )
    finally:
        session.close()
    if not enabled:
        typer.echo(
            "audio processing is switched off (Admin > Audio, or AUDIO_PROCESS_ENABLED); "
            "nothing queued",
            err=True,
        )
        raise typer.Exit(code=1)

    if retry_failed:
        session = _session()
        try:
            requeued = requeue_failed(session, limit=limit)
            session.commit()
            _echo({"retry_failed": True, "requeued": requeued})
        finally:
            session.close()
        return

    moment = None
    if since:
        try:
            moment = datetime.fromisoformat(since)
        except ValueError as exc:
            raise typer.BadParameter(f"{since!r} is not an ISO date") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    wanted = [int(part) for part in (ids or "").replace(" ", "").split(",") if part]

    session = _session()
    try:
        settings = effective_settings(session, base=get_settings())
        call_ids = select_for_reprocess(
            session,
            settings,
            since=moment,
            talkgroup=talkgroup,
            ids=wanted or None,
            only_tone_flagged=only_tone_flagged,
            force=force,
            limit=limit,
        )


        def progress(done: int, offered: int, total: int) -> None:
            typer.echo(f"  queued {done} of {offered}/{total} calls", err=True)

        # Chunked and committed as it goes: one statement per call set blew past
        # PostgreSQL's 65,535 bind parameters at 16,384 calls and lost the whole run, and
        # the insert is idempotent, so a chunk that has landed can stay landed.
        queued = enqueue_many(
            session,
            call_ids,
            priority=priority,
            max_attempts=int(getattr(settings, "audio_process_max_attempts", 3) or 3),
            commit=True,
            on_chunk=progress,
        )
        session.commit()
        _echo(
            {
                "matched": len(call_ids),
                "queued": queued,
                "already_open": len(call_ids) - queued,
                "priority": priority,
                "force": force,
            }
        )
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# users and tokens (API auth, docs/ARCHITECTURE.md 4.7)
# --------------------------------------------------------------------------------------


@user_app.command("create")
def user_create(
    username: Annotated[str, typer.Argument(help="login name")],
    password: Annotated[
        str | None,
        typer.Option("--password", help="password (prompted when omitted)", prompt=False),
    ] = None,
    admin: Annotated[bool, typer.Option("--admin", help="grant the admin scope")] = False,
    display_name: Annotated[str | None, typer.Option("--display-name")] = None,
    email: Annotated[str | None, typer.Option("--email")] = None,
) -> None:
    """Create a sign-in account.  Reads are open; an account is needed to *change* things."""
    from sqlalchemy import func, select

    from app.auth import hash_password, normalize_scopes
    from app.models import User

    secret = password or typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    session = _session()
    try:
        existing = session.scalar(select(User).where(func.lower(User.username) == username.lower()))
        if existing is not None:
            typer.echo(f"user {username!r} already exists", err=True)
            raise typer.Exit(code=1)
        scopes = normalize_scopes(["read", "write", "admin"] if admin else ["read", "write"])
        user = User(
            username=username,
            display_name=display_name,
            email=email,
            password_hash=hash_password(secret),
            scopes=scopes,
        )
        session.add(user)
        session.commit()
        _echo({"id": user.id, "username": user.username, "scopes": scopes})
    finally:
        session.close()


@user_app.command("list")
def user_list() -> None:
    """Every account and its scopes."""
    from sqlalchemy import select

    from app.models import User

    session = _session()
    try:
        _echo(
            [
                {
                    "id": u.id,
                    "username": u.username,
                    "scopes": list(u.scopes or ()),
                    "is_active": u.is_active,
                    "last_login_at": u.last_login_at,
                }
                for u in session.scalars(select(User).order_by(User.username))
            ]
        )
    finally:
        session.close()


@user_app.command("passwd")
def user_passwd(
    username: Annotated[str, typer.Argument(help="login name")],
    password: Annotated[str | None, typer.Option("--password")] = None,
) -> None:
    """Set a new password.  Signs the user out everywhere: every session they hold ends."""
    from sqlalchemy import func, select

    from app.auth import hash_password, revoke_user_sessions
    from app.models import User

    secret = password or typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    session = _session()
    try:
        user = session.scalar(select(User).where(func.lower(User.username) == username.lower()))
        if user is None:
            typer.echo(f"no user {username!r}", err=True)
            raise typer.Exit(code=1)
        user.password_hash = hash_password(secret)
        # A session stolen before the reset must not survive it.
        ended = revoke_user_sessions(session, user.id)
        session.commit()
        _echo(
            {"id": user.id, "username": user.username, "password": "changed", "sessions_ended": ended}
        )
    finally:
        session.close()


@token_app.command("create")
def token_create(
    label: Annotated[str, typer.Argument(help="what this token is for")],
    scopes: Annotated[
        str, typer.Option("--scopes", help="comma separated: read,write,admin,stream,upload")
    ] = "read",
) -> None:
    """Mint an API token.  The secret is printed once and only its hash is stored."""
    from app.auth import create_api_token

    session = _session()
    try:
        row, raw = create_api_token(
            session, label, [s.strip() for s in scopes.split(",") if s.strip()], created_by="cli"
        )
        _echo({"id": row.id, "label": row.label, "scopes": list(row.scopes), "token": raw})
    finally:
        session.close()


@token_app.command("list")
def token_list() -> None:
    """Existing tokens (never the secrets)."""
    from sqlalchemy import select

    from app.models import ApiToken

    session = _session()
    try:
        _echo(
            [
                {
                    "id": t.id,
                    "label": t.label,
                    "scopes": list(t.scopes or ()),
                    "prefix": t.token_prefix,
                    "created_at": t.created_at,
                    "last_used_at": t.last_used_at,
                    "revoked_at": t.revoked_at,
                }
                for t in session.scalars(select(ApiToken).order_by(ApiToken.created_at.desc()))
            ]
        )
    finally:
        session.close()


@token_app.command("revoke")
def token_revoke(token_id: Annotated[int, typer.Argument(help="token id from `token list`")]) -> None:
    """Revoke a token; the row is kept so the audit trail survives."""
    from datetime import UTC as _UTC

    from app.models import ApiToken

    session = _session()
    try:
        row = session.get(ApiToken, token_id)
        if row is None:
            typer.echo(f"no token {token_id}", err=True)
            raise typer.Exit(code=1)
        row.revoked_at = datetime.now(_UTC)
        session.commit()
        _echo({"id": row.id, "label": row.label, "revoked_at": row.revoked_at})
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# activity
# --------------------------------------------------------------------------------------


def _activity_line(row: Any) -> str:
    """One compact human line: ``2026-08-31T03:20:01Z  info  ingest  call.accepted  …``."""
    at = row.at.isoformat() if row.at is not None else "?"
    call = f" call={row.call_id}" if row.call_id is not None else ""
    return f"{at}  {row.level:<5}  {row.source:<10}  {row.kind:<18}  {row.message}{call}"


@activity_app.command("list")
def activity_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="rows to show")] = 50,
    source: Annotated[str | None, typer.Option("--source", "-s", help="ingest|upload|transcribe|…")] = None,
    level: Annotated[str | None, typer.Option("--level", "-l", help="info|warn|error")] = None,
    since: Annotated[str | None, typer.Option("--since", help="ISO timestamp lower bound")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="print JSON instead of a table")] = False,
) -> None:
    """The most recent activity rows, newest first."""
    from app import activity

    since_dt = datetime.fromisoformat(since) if since else None
    session = _session()
    try:
        rows = activity.recent(session, limit=limit, source=source, level=level, since=since_dt)
        if as_json:
            _echo([activity.to_dict(r) for r in rows])
        else:
            for row in reversed(rows):  # oldest first reads naturally in a terminal
                typer.echo(_activity_line(row))
    finally:
        session.close()


@activity_app.command("follow")
def activity_follow(
    source: Annotated[str | None, typer.Option("--source", "-s", help="ingest|upload|transcribe|…")] = None,
    level: Annotated[str | None, typer.Option("--level", "-l", help="info|warn|error")] = None,
    interval: Annotated[float, typer.Option("--interval", help="poll seconds")] = 1.5,
) -> None:
    """Tail the activity log, polling the database (Ctrl-C to stop)."""
    from app import activity

    # Seed from the last few rows so `follow` shows recent context, then only newer ids.
    session = _session()
    try:
        seed = activity.recent(session, limit=10, source=source, level=level)
    finally:
        session.close()
    for row in reversed(seed):
        typer.echo(_activity_line(row))
    last_id = seed[0].id if seed else 0

    try:
        while True:
            time.sleep(interval)
            session = _session()
            try:
                fresh = [
                    r
                    for r in activity.recent(session, limit=500, source=source, level=level)
                    if r.id > last_id
                ]
            finally:
                session.close()
            for row in reversed(fresh):
                typer.echo(_activity_line(row))
            if fresh:
                last_id = fresh[0].id
    except KeyboardInterrupt:  # pragma: no cover - interactive
        raise typer.Exit(code=0) from None


@activity_app.command("prune")
def activity_prune(
    keep_days: Annotated[int, typer.Option("--keep-days", help="delete rows older than this")] = 30,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="count what would go; delete nothing")
    ] = False,
) -> None:
    """Delete activity rows older than ``--keep-days`` (0 = keep for ever).

    Goes through :func:`app.retention.purge_activity_events` — the same batched pass the
    nightly retention run uses — rather than ``activity.prune``'s single DELETE.  That
    matters precisely when somebody reaches for this command: by hand, on a table that has
    grown past whatever window they are about to impose, which is the one case where "all
    of them in one statement" is millions of rows.
    """
    from app.retention import RetentionReport, purge_activity_events

    settings = get_settings().model_copy(update={"activity_retention_days": keep_days})
    session = _session()
    try:
        report = RetentionReport(dry_run=dry_run)
        removed = purge_activity_events(
            session, settings=settings, now=datetime.now(UTC), dry_run=dry_run, report=report
        )
        _echo({"removed": removed, "keep_days": keep_days, "dry_run": dry_run})
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# Broadcastify feed-owner statistics (docs/BCFY_STATS.md)
# --------------------------------------------------------------------------------------


def _bcfy_settings():  # noqa: ANN202
    """The effective settings, so the CLI sees the operator's runtime overrides."""
    from app import runtime_settings

    session = _session()
    try:
        return runtime_settings.effective_settings(session, base=get_settings())
    finally:
        session.close()


@bcfy_app.command("probe")
def bcfy_probe(
    raw_only: Annotated[
        bool, typer.Option("--raw-only", help="print only the response, with no interpretation")
    ] = False,
) -> None:
    """Fetch ``a=feed`` once and print exactly what came back.

    The owner API's response fields are undocumented, so this is the command that turns the
    guesses in ``app.bcfy_stats.client.FIELD_ALIASES`` into observations: run it, read
    ``unmapped_fields``, and add the real names to the mapping.  Nothing is stored, and the
    password appears nowhere in the output.
    """
    from app.api.bcfy_stats import unmapped
    from app.bcfy_stats.client import BcfyOwnerError, extract
    from app.bcfy_stats.poller import make_client

    settings = _bcfy_settings()
    client = make_client(settings)
    if client is None:
        _echo(
            {
                "ok": False,
                "error": (
                    "incomplete credentials: set bcfy_owner_username, bcfy_owner_password and "
                    "bcfy_feed_id (Admin -> Settings -> Feed stats)"
                ),
            }
        )
        raise typer.Exit(code=1)
    try:
        document = client.feed()
    except BcfyOwnerError as exc:
        _echo({"ok": False, "feed_id": client.feed_id, "error": str(exc)})
        raise typer.Exit(code=1) from None
    if raw_only:
        _echo(document)
        return
    extracted = extract(document)
    _echo(
        {
            "ok": True,
            "feed_id": client.feed_id,
            "raw": document,
            "extracted": extracted,
            "unmapped_fields": unmapped(document, extracted),
        }
    )


@bcfy_app.command("poll")
def bcfy_poll() -> None:
    """Poll once and **store** the sample, exactly as the worker's job would."""
    from app.bcfy_stats import store
    from app.bcfy_stats.client import BcfyOwnerError
    from app.bcfy_stats.poller import make_client

    settings = _bcfy_settings()
    client = make_client(settings)
    if client is None:
        _echo({"ok": False, "error": "incomplete credentials"})
        raise typer.Exit(code=1)
    session = _session()
    try:
        sample = store.record_sample(session, feed_id=client.feed_id, document=client.feed())
        session.commit()
        _echo(
            {
                "ok": True,
                "feed_id": client.feed_id,
                "listeners": sample.listeners,
                "online": sample.online,
                "extracted": sample.extracted,
            }
        )
    except BcfyOwnerError as exc:
        session.rollback()
        _echo({"ok": False, "feed_id": client.feed_id, "error": str(exc)})
        raise typer.Exit(code=1) from None
    finally:
        session.close()


@bcfy_app.command("page")
def bcfy_page() -> None:
    """Fetch the **public** feed page once and backfill the hours nobody polled.

    The page at ``broadcastify.com/listen/feed/<id>`` needs no login and server-renders a
    24-hour hourly listener sparkline, which is the only listener history Broadcastify
    publishes anywhere — the owner API carries none.  Each bar fills an hour that has no
    poll of its own; an hour this deployment *did* poll is never overwritten.

    Needs only ``bcfy_feed_id``, no credentials, and ignores both the poller switch and the
    fetch interval.  ``bars`` comes back empty when their markup has changed, and nothing
    is written.
    """
    from app.bcfy_stats.poller import sync_public_page

    settings = _bcfy_settings()
    session = _session()
    try:
        report = sync_public_page(session, settings, force=True)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    _echo({"ok": "error" not in report and "skipped" not in report, **report})
    if report.get("error") or report.get("skipped"):
        raise typer.Exit(code=1)


@bcfy_app.command("archives")
def bcfy_archives(
    days: Annotated[int, typer.Option("--days", help="how many recent days of listings to refresh")] = 7,
    full: Annotated[
        bool,
        typer.Option("--full", help="catch the whole archive backlog up, not just this pass's budget"),
    ] = False,
) -> None:
    """Sync the archive listings now, ignoring the switch and the interval.

    Every run refreshes the last ``--days`` days, whose download links expire, and then
    backfills days Broadcastify lists but this database has never stored — oldest first, up
    to ``bcfy_archives_backfill_days_per_sync`` of them.  ``--full`` raises that budget to a
    year, which is the one-time catch-up for a feed whose archives predate this deployment;
    run it twice if ``backfill_remaining`` is still non-zero.
    """
    from app.bcfy_stats.poller import sync_archives

    session = _session()
    try:
        report = sync_archives(session, _bcfy_settings(), force=True, full=full, days=days)
        session.commit()
        _echo(report)
    finally:
        session.close()


@bcfy_app.command("import")
def bcfy_import(
    path: Annotated[Path, typer.Argument(help="one of the dashboard's two CSV exports")],
    feed_id: Annotated[
        str | None, typer.Option("--feed-id", help="override the configured bcfy_feed_id")
    ] = None,
) -> None:
    """Import a feed dashboard CSV export — the only backfill that reaches back months.

    Broadcastify -> Manage -> your feed -> Statistics offers two exports, and the format is
    read from the file's header rather than given here:

    * ``Hour,Online Checks,Online Reports,Max Listeners`` — about a week of hours, which go
      into the hourly rollup beside the polled ones;
    * ``Day,Uptime %,Max Listeners`` — about 180 days, which go into ``bcfy_feed_daily``.

    Their timestamps are **UTC** (docs/BCFY_STATS.md).  Precedence is polled > import >
    page: an imported hour replaces one reconstructed from the public feed page, refreshes
    an earlier import, and never touches an hour this deployment polled.  Running it twice
    on the same file therefore changes nothing, which is what makes it safe to re-run after
    a fresh export.
    """
    from app.bcfy_stats.importer import ImportFormatError, import_csv

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    resolved = str(feed_id or getattr(_bcfy_settings(), "bcfy_feed_id", "") or "").strip()
    if not resolved:
        typer.echo("no feed configured: set bcfy_feed_id, or pass --feed-id", err=True)
        raise typer.Exit(code=1)
    session = _session()
    try:
        report = import_csv(session, resolved, text)
        session.commit()
    except ImportFormatError as exc:
        session.rollback()
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    _echo({"feed_id": resolved, **report})


@bcfy_app.command("prune")
def bcfy_prune(
    keep_days: Annotated[
        int, typer.Option("--keep-days", help="delete samples older than this; 0 = keep")
    ] = 400,
) -> None:
    """Delete raw samples past their retention.  The hourly rollup keeps the history."""
    from app.bcfy_stats.store import prune_samples

    session = _session()
    try:
        removed = prune_samples(session, keep_days=keep_days)
        session.commit()
        _echo({"removed": removed, "keep_days": keep_days})
    finally:
        session.close()


@sites_app.command("seed-message-kinds")
def sites_seed_message_kinds(
    site_id: Annotated[
        int | None, typer.Option("--site", help="only this site (default: every site)")
    ] = None,
) -> None:
    """Replay the stored observation windows into ``site_message_kinds``, silently.

    The baseline for the phase-5 discovery alert (docs/UNKNOWN-DATA-PLAN.md): without it
    the first window after the migration would report every kind the site has always been
    transmitting as new.  Nothing reaches the activity log and no alert is delivered — run
    it once after upgrading, and again only if the table is ever rebuilt.
    """
    from app.rr.message_kinds import seed_message_kinds

    session = _session()
    try:
        result = seed_message_kinds(session, site_id=site_id)
    finally:
        session.close()
    _echo(result.as_dict())


@sites_app.command("name-message-kinds")
def sites_name_message_kinds(
    toml_path: Annotated[
        Path | None,
        typer.Option(
            "--toml",
            help="a trunkd identifiers.toml whose [tsbk] table names more than the built-in table",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    site_id: Annotated[
        int | None, typer.Option("--site", help="only this site (default: every site)")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="count what would be named and write nothing")
    ] = False,
) -> None:
    """Name the stored control-channel message kinds from the built-in P25 table.

    Rows written before there was a table to name them from are all ``known = false``,
    which makes the unknown count say nothing: most of them are standard opcodes with a
    name in TIA-102.AABC.  This sets ``known``/``label`` on every row the built-in table
    (and, with ``--toml``, trunkd's curated registry) names; a kind an operator named by
    hand is left alone, and a registry entry marked *noise* gets its label but stays
    unknown, because a CRC false pass is explained rather than known.

    Idempotent — re-run it after every registry update.
    """
    from app.rr.message_kinds import name_message_kinds

    session = _session()
    try:
        result = name_message_kinds(
            session,
            toml_path=str(toml_path) if toml_path is not None else None,
            site_id=site_id,
            dry_run=dry_run,
        )
    finally:
        session.close()
    _echo(result.as_dict())



@transcripts_app.command("search-vocab")
def transcripts_search_vocab_cmd(
    full: Annotated[
        bool,
        typer.Option(
            "--full/--incremental",
            help="rebuild the whole vocabulary (~6 s) rather than add new transcripts' words",
        ),
    ] = True,
) -> None:
    """Refresh the fuzzy-search vocabulary (``search_vocab``) now.

    The worker does this every five minutes and rebuilds it daily; this is for a restore,
    a bulk re-transcription, or checking the numbers by hand.
    """
    from app.services.fuzzy import refresh_vocabulary  # noqa: PLC0415

    session = _session()
    try:
        summary = refresh_vocabulary(session, full=full)
        session.commit()
    finally:
        session.close()
    _echo(summary)


if __name__ == "__main__":  # pragma: no cover
    app()
