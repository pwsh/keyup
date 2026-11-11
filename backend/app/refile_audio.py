"""Move already-stored recordings into the partition their own date names.

The sibling of :mod:`app.repair_timestamps`, and the second half of the same repair.  That
one put 1,041,258 backlog calls back on the day they were *recorded*; this one puts their
files there.  Until it runs, the archive's directory layout still records the day the
backlog was *pushed*: 972,347 files in ``uploads/2026/09/01`` and 82,174 in
``uploads/2026/08/31``, because :func:`app.api.call_upload.upload_path` used to partition
on a date taken from the form before anything had parsed the recording
(docs/ARCHITECTURE.md, "Storage layout").

ext4's ``dir_index`` keeps a million-entry directory *functionally* fine — a lookup by name
is a hash probe.  Everything that does not look up by name is not fine: ``readdir`` returns
hash order, so ``rsync``, ``find``, a backup, an integrity sweep and every directory walk
pay for the whole directory and then seek all over the disk.  Partitioned by call date the
same archive spreads over about 1,100 real days at a few thousand files each.

How it works
------------
It walks ``calls``, **not the filesystem** — that is the whole point; enumerating the
directory this exists to break up would be the slowest possible way to fix it.  Each row
whose ``file_path`` is under ``uploads/`` and whose partition disagrees with its
``started_at`` gets its file ``os.rename``\\ d into the right one and its ``file_path``
rewritten in the same transaction, committed a batch at a time.  A rename within one
filesystem is a directory-entry edit: the inode does not move and the bytes are not read,
which is why a million of them take minutes rather than the hours a copy would.

Safety, in the order it matters:

* **Nothing is ever deleted.**  The only ways a file leaves its old path are ``rename`` and,
  across a device boundary, copy-verify-then-unlink.
* **Every move is verified.**  ``st_ino`` and ``st_size`` are compared before and after —
  for a rename the inode is *unchanged*, so an equal inode at the new path is proof the
  entry names the same bytes, and it costs one ``stat``.  A cross-device fallback cannot
  preserve the inode, so it verifies the size and, when the row has one, the sha256, before
  it unlinks the source.
* **It is resumable and re-runnable.**  A moved row's partition now agrees with its
  ``started_at``, so a second pass counts it ``unchanged``; a run interrupted between the
  rename and the commit is recognised on the next pass — the source is gone and the target
  is there with the right size — and the row is repaired without touching the disk.
* **A row whose file is missing is reported, not fatal.**  So is one whose ``file_path``
  points outside the archive, which :func:`app.audio.audio_path` refuses to resolve.

``--dry-run`` is the default everywhere: it reads, computes every target, and writes
neither a row nor a directory entry.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audio import UPLOADS_DIR, audio_path, processed_path, prune_empty_dirs, upload_partition
from app.config import Settings
from app.models import Call

log = logging.getLogger("app.refile")

#: rows per committed transaction.  Large, because the work per row is one ``rename`` and
#: one column update and the transaction holds no lock anything live contends for.
DEFAULT_BATCH_SIZE = 5000

#: how many distinct failures the report keeps; the log has them all
MAX_ERRORS = 50

__all__ = ["DEFAULT_BATCH_SIZE", "RefileReport", "refile_audio", "target_for"]


@dataclass
class RefileReport:
    """What one pass did, or — the default — what it would have done."""

    dry_run: bool = True
    #: rows considered (``file_path`` under the source prefix)
    scanned: int = 0
    #: rows already in the partition their ``started_at`` names
    unchanged: int = 0
    #: files moved (or, in a dry run, that would be)
    moved: int = 0
    #: rows whose file was already at the target — an interrupted earlier run, path fixed
    repaired: int = 0
    #: rows whose file is not at either path.  Reported; the row is left exactly as it is
    missing: int = 0
    #: moves that crossed a device boundary and fell back to copy-verify-unlink
    copied: int = 0
    #: rows that could not be moved; each one is in ``errors``
    failed: int = 0
    #: emptied source directories removed
    dirs_pruned: int = 0
    #: the id the walk reached, so an interrupted run can say where it stopped
    last_id: int = 0
    errors: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.failed += 1
        log.warning("refile: %s", message)
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "unchanged": self.unchanged,
            "moved": self.moved,
            "repaired": self.repaired,
            "missing": self.missing,
            "copied": self.copied,
            "failed": self.failed,
            "dirs_pruned": self.dirs_pruned,
            "last_id": self.last_id,
            "errors": self.errors,
        }


def target_for(call: Call, settings: Settings) -> str | None:
    """The ``calls.file_path`` this row's own ``started_at`` names, or ``None``.

    ``None`` when the row is not one of ours to move: no path, a path that is not under
    ``uploads/`` (a watched-tree recording keeps the layout its recorder chose, and an
    archived row's path is absolute and belongs to retention), or no ``started_at``.
    """
    raw = (call.file_path or "").strip()
    if not raw or not raw.startswith(f"{UPLOADS_DIR}/") or call.started_at is None:
        return None
    name = Path(raw).name
    return f"{upload_partition(call.started_at, settings, sha256=call.sha256, name=name)}/{name}"


def _free_target(
    target: str,
    root: Path,
    session: Session,
    taken: set[str],
    *,
    check_db: bool,
) -> str:
    """``target``, or the first ``_1``…``_999`` variant free on disk, in ``calls`` and here.

    Three collision domains, because all three can bite: ``calls.file_path`` is UNIQUE, a
    file already at the name would be overwritten by ``rename``, and two rows in the *same
    batch* can both want it before either has been written.
    """
    stem, suffix = Path(target).stem, Path(target).suffix
    parent = str(Path(target).parent)

    def free(candidate: str) -> bool:
        if candidate in taken or (root / candidate).exists():
            return False
        if not check_db:
            return True
        return (
            session.execute(select(Call.id).where(Call.file_path == candidate).limit(1)).first()
            is None
        )

    for candidate in (target, *(f"{parent}/{stem}_{n}{suffix}" for n in range(1, 1000))):
        if free(candidate):
            return candidate
    return f"{parent}/{stem}_{os.urandom(4).hex()}{suffix}"  # pragma: no cover - 1000 collisions


def _claimed_by_other(session: Session, target: str, call_id: int) -> bool:
    """True when some *other* row already names ``target``."""
    return (
        session.execute(
            select(Call.id).where(Call.file_path == target, Call.id != call_id).limit(1)
        ).first()
        is not None
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_across_devices(source: Path, destination: Path, expect_sha: str | None) -> None:
    """The ``EXDEV`` fallback: copy, flush to disk, verify, and only then unlink.

    ``AUDIO_ROOT`` is one ext4 filesystem in this deployment and this should never run;
    it exists because a deployment that bind-mounts a second volume under the root would
    otherwise lose the recording to a bare ``rename`` failure.
    """
    shutil.copyfile(source, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    if destination.stat().st_size != source.stat().st_size:
        destination.unlink(missing_ok=True)
        raise OSError(f"copy of {source} came out a different size")
    if expect_sha and _sha256(destination) != expect_sha:
        destination.unlink(missing_ok=True)
        raise OSError(f"copy of {source} does not hash to {expect_sha}")
    source.unlink()


def _move(source: Path, destination: Path, expect_sha: str | None) -> bool:
    """Move one recording and prove it arrived.  True when it crossed a device boundary."""
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_across_devices(source, destination, expect_sha)
        return True
    after = destination.stat()
    if (after.st_ino, after.st_size) != (before.st_ino, before.st_size):
        # A rename does not touch the inode, so this cannot happen without something else
        # having written the target between the check and here.  Say so rather than record
        # a path for bytes we did not verify.
        raise OSError(
            f"{destination} is not the file that was at {source}: "
            f"inode {before.st_ino}->{after.st_ino}, size {before.st_size}->{after.st_size}"
        )
    return False


def refile_audio(  # noqa: PLR0912, PLR0915 - one loop, read top to bottom
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    source_prefix: str | None = None,
    on_batch: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Re-file every pushed recording whose partition disagrees with its ``started_at``.

    ``source_prefix`` narrows the walk to one ``file_path`` prefix — ``uploads/2026/09/01``
    is the flat directory the backlog landed in — which is worth using on a re-run, when
    scanning the whole table would otherwise re-read a million rows to find nothing.

    Returns the report as a dict.  ``on_batch`` is called with the running totals after
    each committed batch, for a CLI that wants to show progress on a run this long.
    """
    root = Path(settings.audio_root)
    report = RefileReport(dry_run=dry_run)
    prefix = (source_prefix or UPLOADS_DIR).strip().strip("/")
    last_id = 0

    while True:
        rows = (
            session.execute(
                select(Call)
                .where(
                    Call.id > last_id,
                    Call.file_path.like(f"{prefix}/%"),
                    # Only rows that claim to have bytes on disk.  A purged row names where
                    # its recording *would* have been and an archived one lives under the
                    # retention archive with an absolute path; moving either would rewrite
                    # history rather than tidy it.
                    Call.audio_state == "present",
                )
                .order_by(Call.id)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].id
        report.last_id = last_id

        taken: set[str] = set()
        pruneable: set[Path] = set()
        moved_back: list[tuple[Path, Path]] = []

        for call in rows:
            report.scanned += 1
            target = target_for(call, settings)
            if target is None or target == call.file_path:
                report.unchanged += 1
                continue

            source = audio_path(call, settings)
            if source is None:
                report.note(f"call {call.id}: {call.file_path!r} is outside the archive")
                continue

            if not source.is_file():
                # Either the file really is gone, or an earlier run renamed it and died
                # before the commit.  The second is recoverable: the recording is sitting
                # at the target this row would have been given, with the size the row
                # remembers, and nothing else has claimed that path.  This has to be asked
                # *before* the free-name walk, which would otherwise see the row's own
                # rescued file in the way and hand out ``_1``.
                landed = root / target
                if (
                    landed.is_file()
                    and not _claimed_by_other(session, target, call.id)
                    and (not call.file_size or landed.stat().st_size == call.file_size)
                ):
                    if not dry_run:
                        _rewrite(session, call, target, settings)
                    taken.add(target)
                    report.repaired += 1
                    continue
                report.missing += 1
                log.info("refile: call %s has no file at %s", call.id, source)
                continue

            target = _free_target(target, root, session, taken, check_db=True)
            taken.add(target)
            destination = root / target

            if dry_run:
                report.moved += 1
                continue

            try:
                crossed = _move(source, destination, call.sha256)
            except OSError as exc:
                report.note(f"call {call.id}: {source} -> {destination}: {exc}")
                continue
            moved_back.append((destination, source))
            report.copied += int(crossed)
            report.moved += 1
            pruneable.add(source.parent)
            _rewrite(session, call, target, settings)

        if dry_run:
            session.rollback()
        else:
            try:
                session.commit()
            except Exception:
                # The rows did not land, so the files must not stay moved: a file at a path
                # no row names is invisible to the API, and the next pass would count it
                # missing.  Put every one of them back before re-raising.
                session.rollback()
                for destination, source in moved_back:
                    with contextlib.suppress(OSError):
                        os.rename(destination, source)
                raise
            for directory in pruneable:
                report.dirs_pruned += prune_empty_dirs(directory, settings)

        if on_batch is not None:
            on_batch(report.as_dict())

    log.info("refile: %s", report.as_dict())
    return report.as_dict()


def _rewrite(session: Session, call: Call, target: str, settings: Settings | None = None) -> None:
    """Point the row — and the ingest ledger — at where the bytes now are.

    The processed copy (§5.5) is *dropped* rather than moved. It is derived data that the
    backfill can rebuild in a second, and carrying a second file through the rename — with
    its own cross-device fallback and its own entry in the rollback list — would double the
    ways this pass can leave the archive half-moved for something nobody needs preserved.
    Clearing the column is what makes the call fall back to its original until then.
    """
    old = call.file_path
    if settings is not None and call.processed_path:
        stale = processed_path(call, settings)
        if stale is not None:
            with contextlib.suppress(OSError):
                stale.unlink()
        call.processed_path = None
    call.file_path = target
    call.file_name = Path(target).name
    # The ledger is keyed on the path, and a stale entry would let a scan of ``uploads/``
    # (which is pruned by default, but an operator can ask for one) treat the moved file as
    # a new recording.  ``NOT EXISTS`` because the ledger's key is unique and a duplicate
    # entry for the target is not worth failing the batch over.
    session.execute(
        text(
            "UPDATE ingest_files SET file_path = :new WHERE file_path = :old "
            "AND NOT EXISTS (SELECT 1 FROM ingest_files WHERE file_path = :new)"
        ),
        {"new": target, "old": old},
    )
