"""Retro-flag caption-credit hallucinations already stored as good transcripts (§5.2).

The phrase heuristics in :mod:`app.stt.postprocess` only ever ran on new transcripts, and
until the pattern families landed they only caught an utterance that matched the exact
phrase list character for character.  What that left behind in this archive is measurable:
1,005 transcripts containing "captioning provided by", 913 of them still labeled ``good``
and denormalized onto their call, plus thousands of sign-off artifacts ("thank you for
watching", "see you next time") on calls averaging two seconds of audio.  They pollute
search, they pollute the exporters, and no amount of fixing the live path removes them.

This pass applies the *current* check to the *stored* text and, where it fires, leaves the
call exactly as the worker would have left it today: the transcript row keeps its text but
is relabeled ``noise`` with the reason appended, and the call loses its denormalized text
and goes to ``transcript_status='empty'``.  Nothing is deleted — an operator can still read
what the engine said on the transcript row.

Two properties make it safe to run against a live archive:

*idempotent*
    a row it has already flagged is ``transcript_status='empty'``, out of scope, so a second
    run finds it a second time only if something requeued and re-transcribed the call.

*bounded*
    keyset-paged by transcript id and committed per batch, so the live feed flows between
    batches and Ctrl-C costs at most one batch.

The SQL prefilter is what keeps it out of a 900k-row Python scan: an ``ILIKE`` over the
literal fragments every pattern family must contain — or, for the exact phrase list, whose
short entries ("you", "so", "ok") have no fragment worth an ``ILIKE``, a length bound —
applied in the database, followed by the real check in Python on the rows that survive.

**Human text is never judged.**  The pass reads the *engine's* transcript
(``transcripts.text``, its segments collapsed exactly as the worker collapses them) and
skips every call whose primary transcript a person has edited: a correction is the best
text the call has, and nulling it because it reads "Okay." would destroy the one thing
nobody can regenerate.

The check is run over :func:`~app.stt.postprocess.normalize_text` of the stored text and with
the call's own duration, because the live path judges both — stored text pre-dates the
rewrites, and the sign-off family is gated on how long the call was.  A dry run that skipped
either proposed discarding 627 real transmissions whisper had written as domains (a callsign
as ``XCOM.com``, repaired by ``STT_TEXT_REWRITES``) and a handful of genuine 5-10 s sign-offs.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, exists, func, or_, select, update
from sqlalchemy import true as sa_true
from sqlalchemy.orm import Session

from app import activity
from app.models import Call, Transcript
from app.stt.postprocess import (
    collapse_repeats,
    compile_patterns,
    hallucination_phrase,
    normalize_text,
)

log = logging.getLogger("app.flag_hallucinations")

DEFAULT_BATCH_SIZE = 1000

#: The call states whose text is on screen.  ``done`` is the normal one.  ``skipped`` should
#: carry no text, but 356 calls in this archive do — transcribed 2026-09-01..03, moved to
#: ``skipped`` during that week's duplicate repairs with their text and label left behind —
#: and the call list renders ``transcript_text`` whatever the status, so their hallucinated
#: "Thank you for listening." was shown and searchable while this pass, reading ``done``
#: only, never saw it.  A flagged row of either kind ends ``empty``, which is out of scope.
SCANNED_STATUSES: tuple[str, ...] = ("done", "skipped")

#: Literal substrings, one of which every builtin pattern family requires.  The prefilter is
#: deliberately *wider* than the patterns — it may hand Python a row the real check clears
#: ("dispatch, the end of Alder Street") but must never hide one the check would flag.
PREFILTER_FRAGMENTS: tuple[str, ...] = (
    "caption",
    "subtit",
    "presentation",
    "transcri",
    "amara",
    "www.",
    ".com",
    "copyright",
    "rights reserved",
    "brandhagen",
    "wkno",
    "watching",
    "listening",
    "attention",
    "your time",
    "joining",
    "next time",
    # "see you on the next one" — the one sign-off with no fragment above.  A sign-off's
    # closings ("take care", "have a great day") need none: they never condemn alone, and
    # the conversational ones are short enough for the length bound below.  Production
    # cost (2026-10-10): 734 rows the rest of the prefilter did not already select.
    "in the next",
    "on the next",
    "subscribe",
    "the end",
    "bye",
    "goodbye",
    # the broadcast-credit frames: one of these for every frame's noun or verb ("thank" below
    # covers the thank-you frames, "presentation" above the presentation one).  Phrases, not
    # bare words — "department" and "possible" alone hand Python 10k rows of fire departments
    # and possible strokes.
    "production of",
    "project of",
    "program of",
    "programme of",
    "brought to you",
    "made possible",
    "produced",
    "presented",
    "funded",
    "sponsored",
    "supported",
    "department of",
    # the exact phrase list and the conversational words: every entry that has a fragment
    # of its own is covered here, at any length ...
    "thank",
    "okay",
    "music",
    "applause",
    "silence",
    "beep",
    "castingwords",
    "mooji",
)

#: ... and the rest ("you", "so", "ok" — too common for an ``ILIKE``) by length: a stored
#: text no longer than this is always handed to the real check.  The longest exact-list
#: entry without a fragment above is "ok"/"you"/"so" with punctuation; 48 characters also
#: covers such a word repeated several times over, which the segment collapse reduces to
#: the bare word.  A longer run of repeats is missed here — it is a decoder loop, which
#: the worker's loop rule is for, not a caption artifact.
SHORT_TEXT_PREFILTER_CHARS = 48


#: Regex metacharacters: everything between them in a pattern is literal text the match must
#: contain, which is what makes an operator pattern prefilterable.
_META_RE = re.compile(r"[\\^$.|?*+()\[\]{}]")

#: Shorter literal runs than this are too common to narrow anything down.
MIN_FRAGMENT_CHARS = 3


def _literal_fragment(pattern: re.Pattern[str]) -> str | None:
    """The longest run of literal text an operator pattern must contain, or ``None``.

    ``brought to you by`` is entirely literal and prefilters perfectly; ``w.?k.?n.?o`` has no
    run worth an ``ILIKE`` and returns ``None``, which drops the prefilter for the whole run
    rather than silently missing rows.
    """
    runs = [run.strip() for run in _META_RE.split(pattern.pattern)]
    best = max(runs, key=len, default="")
    return best.lower() if len(best) >= MIN_FRAGMENT_CHARS else None


def _engine_text(text: str | None, segments: Any) -> str:
    """The text the worker judged: segment texts with consecutive repeats collapsed.

    ``transcripts.text`` is the engine's raw output, and ``"Thank you. Thank you."`` is
    two segments the live path collapsed to one before judging — so the backfill does the
    same, and falls back to the raw text for a row stored without segments.
    """
    texts = [
        str(item["text"])
        for item in (segments if isinstance(segments, list) else [])
        if isinstance(item, dict) and item.get("text")
    ]
    if texts:
        kept, _ = collapse_repeats(texts)
        return normalize_text(" ".join(t.strip() for t in kept))
    return normalize_text(text)


def _family(reason: str) -> str:
    """``credit`` from ``credit:closed caption`` — the counting bucket for the report."""
    return reason.split(":", 1)[0]


def flag_hallucinations(
    session: Session,
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    since: datetime | None = None,
    patterns: str | None = None,
    on_batch: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Walk the prefiltered primary transcripts and flag the ones that are hallucinations.

    ``dry_run`` (the default) counts without writing.  ``since`` restricts to calls started
    at or after that instant.  ``patterns`` is the raw ``stt_hallucination_patterns`` text,
    merged into the check exactly as the worker merges it.
    """
    extra = compile_patterns(patterns)
    # The engine's own text, never the call's denormalized copy: that one may be a
    # person's correction.
    text_col = Transcript.text_
    fragments = list(PREFILTER_FRAGMENTS)
    prefilter = True
    for pattern in extra:
        fragment = _literal_fragment(pattern)
        if fragment is None:
            # Nothing to ILIKE on: scan everything rather than miss rows the pattern covers.
            log.warning(
                "flag-hallucinations: pattern %r has no literal fragment; scanning without a"
                " prefilter (this reads every transcribed call)",
                pattern.pattern,
            )
            prefilter = False
            break
        fragments.append(fragment)
    likes = [text_col.ilike(f"%{fragment}%") for fragment in fragments]
    likes.append(func.char_length(func.btrim(text_col)) <= SHORT_TEXT_PREFILTER_CHARS)
    # A call whose primary transcript a person corrected — however the correction got there.
    edited = exists().where(
        Transcript.call_id == Call.id,
        Transcript.is_primary.is_(True),
        Transcript.edited_by_user.is_(True),
    )

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "scanned": 0,
        "flagged": 0,
        "batches": 0,
        "families": {},
        "reasons": {},
    }
    families: Counter[str] = Counter()
    reasons: Counter[str] = Counter()

    last_id = 0
    while True:
        stmt = (
            select(
                Transcript.id,
                Transcript.call_id,
                Transcript.quality_reasons,
                text_col,
                Transcript.segments,
                Transcript.no_speech_prob,
                Call.duration_s,
            )
            .join(Call, Call.id == Transcript.call_id)
            .where(
                Transcript.id > last_id,
                Transcript.is_primary.is_(True),
                Transcript.edited_by_user.is_(False),
                Call.transcript_status.in_(SCANNED_STATUSES),
                # a skipped call only while it still shows text (a duplicate's copy, which
                # carries none, is not this pass's business)
                or_(Call.transcript_status == "done", Call.transcript_text.is_not(None)),
                text_col.is_not(None),
                or_(*likes) if prefilter else sa_true(),
                or_(
                    Transcript.quality.is_(None),
                    cast(Transcript.quality, String) != "noise",
                ),
            )
            .order_by(Transcript.id)
            .limit(batch_size)
        )
        if since is not None:
            stmt = stmt.where(Call.started_at >= since)
        rows = session.execute(stmt).all()
        if not rows:
            break

        report["batches"] += 1
        report["scanned"] += len(rows)
        last_id = rows[-1][0]
        batch_families: Counter[str] = Counter()
        hits: list[tuple[int, int, list[str], str]] = []
        for transcript_id, call_id, quality_reasons, text, segments, no_speech, duration in rows:
            reason = hallucination_phrase(
                _engine_text(text, segments),
                extra,
                float(duration) if duration is not None else None,
                no_speech_prob=float(no_speech) if no_speech is not None else None,
            )
            if reason is None:
                continue
            hits.append((transcript_id, call_id, list(quality_reasons or []), reason))
            batch_families[_family(reason)] += 1
            reasons[reason] += 1

        families.update(batch_families)
        report["flagged"] += len(hits)

        if hits and not dry_run:
            for transcript_id, _call_id, quality_reasons, reason in hits:
                stored = f"hallucination_phrase:{reason}"
                if stored not in quality_reasons:
                    quality_reasons = [*quality_reasons, stored]
                session.execute(
                    update(Transcript)
                    .where(Transcript.id == transcript_id, Transcript.edited_by_user.is_(False))
                    .values(quality="noise", quality_reasons=quality_reasons)
                    .execution_options(synchronize_session=False)
                )
            session.execute(
                update(Call)
                # Re-checked at write time: a correction saved since the read above wins.
                .where(Call.id.in_([call_id for _t, call_id, _r, _reason in hits]), ~edited)
                .values(
                    transcript_text=None,
                    transcript_quality="noise",
                    transcript_status="empty",
                )
                .execution_options(synchronize_session=False)
            )
            activity.record(
                session,
                source="stt",
                level="warn",
                kind="stt.hallucination_backfill",
                message=(
                    f"flagged {len(hits)} stored transcript(s) as hallucinations"
                    f" ({', '.join(f'{k}={v}' for k, v in sorted(batch_families.items()))})"
                ),
                meta={
                    "batch": report["batches"],
                    "scanned": len(rows),
                    "flagged": len(hits),
                    "families": dict(batch_families),
                    "last_transcript_id": last_id,
                },
            )
            session.commit()
        elif hits:
            log.info("flag-hallucinations: batch %d would flag %d row(s)", report["batches"], len(hits))

        if on_batch is not None:
            on_batch(
                {
                    "batch": report["batches"],
                    "scanned": len(rows),
                    "flagged": len(hits),
                    "families": dict(batch_families),
                    "last_id": last_id,
                }
            )

    report["families"] = dict(sorted(families.items()))
    report["reasons"] = dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
    return report
