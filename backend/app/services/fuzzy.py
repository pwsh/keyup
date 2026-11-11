"""Fuzzy search: spelling variants from the archive's own vocabulary.

Speech-to-text writes a street name the way it heard it — ``Milrose`` for Melrose,
``Kelguck`` for Kelbuk — and full-text search, which matches lexemes exactly, cannot find
one spelling from the other.  This module widens a query to the spellings the archive
actually contains, and nothing else changes: the widened query is still a ``tsquery`` over
``calls.search_tsv``, so ranking, the websearch syntax, highlighting and every filter
behave as they do for an exact search.

**The vocabulary.**  ``search_vocab`` holds one row per distinct lexeme in ``search_tsv``
(``ts_stat`` output: ``word``, ``ndoc``, ``nentry``) with a trigram GIN index on ``word``.
On production (2026-09-23) that is 181 k lexemes and about 20 MB with its indexes, built
by ``ts_stat`` over 5.2 M calls in 5.2 s.  The words are *lexemes*, stemmed the way
``search_tsv`` stems them (``melrose`` is ``melros``), because the query is compared in
that space too: ``websearch_to_tsquery`` stems what the reader typed.

A trigram index straight on ``calls.transcript_text`` was measured and not taken.  It is
smaller than feared (≈250 MB extrapolated from a 5 % sample) and ``word <% text`` is quick,
but it bypasses the FTS pipeline — no ranking, no websearch syntax, no highlight, no
stemming, no notes or alias — and every transcript write pays ~40 more GIN entries.  The
vocabulary costs 20 MB and leaves the write path alone.

**Expansion.**  Each positive (not negated) lexeme of four or more letters, and no digits —
a unit number one digit off is a different unit — is looked up with ``word % term``
(trigram similarity ≥ ``pg_trgm.similarity_threshold``, 0.3 by default) and the
candidates are kept only within a small edit distance: one edit for a term of up to five
letters, two above that.  Trigram similarity alone is too loose at the threshold real
misspellings need (``park`` → ``parkview``, 0.40, is as similar as ``melros`` →
``milros``); edit distance is what separates a misspelling from a different word.  At most
:data:`MAX_VARIANTS` per term: fewest edits first, then the most common — a spelling
Whisper keeps making (``milros``, 60 calls) before a one-off.  When the term is itself an
established word (:data:`ESTABLISHED`), a variant more common than it is dropped: a
misspelling is rarer than the word it misspells, so ``rochest`` → ``chest`` is two words.
And a term in :data:`COMMON` calls or more is not widened at all: it has no spelling
problem worth solving (``fire``, in 427 k calls, gains 31 from its eight variants), and
widening it is what made an all-time fuzzy search for it slow — every one of its calls is
ranked against a nine-lexeme query instead of one.

**Keeping it current.**  A worker job (:func:`refresh_vocabulary`, every five minutes) adds
the lexemes of the calls whose transcripts arrived since the last pass — a watermark on
``transcripts.id`` in the ``settings`` table, ~20 ms a pass on production's rate — and once a
day rebuilds the whole table (≈6 s), which is what picks up transcript corrections, notes,
retention's deletions and exact counts.  A per-row trigger was measured and rejected: it
made bulk writes to ``calls`` about three times slower (15 → 48 µs a row) and could
deadlock two multi-row transactions that meet the same new words in a different order.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.types import Text

from app.models import Setting, Transcript

log = logging.getLogger(__name__)

#: A term shorter than this is not widened: at three letters every trigram is shared with
#: half the dictionary, and a real misspelling of a three-letter word is rarely the reason
#: a search failed.
MIN_TERM_LENGTH = 4
#: Spelling variants kept per term, closest first.
MAX_VARIANTS = 8
#: A term in at least this many calls is a word in its own right, and a close word that is
#: *more* common than it is another word rather than a misspelling of it: ``rochester``
#: (≈1 000 calls) must not widen to ``chest`` (4 700).  A term below it is most likely the
#: misspelling itself, and the common spelling is exactly what it should widen to.
ESTABLISHED = 20
#: A term in at least this many calls is searched exactly even when ``fuzzy`` is on.
#: ``ts_rank_cd`` is computed for every matching call, and against a widened query it costs
#: more per call: for ``fire`` (427 k calls) the ranked page took 1.8 s instead of 1.3 s and
#: the Search page's request 1.9 s instead of 0.9 s (warm; 3.3 s cold), to add 31 calls
#: spelt ``firet``, ``firem``, ``fir`` …  A word that common is not the one a transcript
#: got wrong.  The words fuzzy search exists for sit well below it: ``melros`` 2 466 calls,
#: ``kelbuk`` 1 428, ``ashby`` 3 850; above it are ``medic``, ``highway``, ``park``,
#: ``street``, ``engin``, ``fire`` — 310 words of 181 k (production, 2026-09-23).  At the
#: cap the widened ranking costs ~10 ms, not half a second.  Exact search never reads it.
COMMON = 10_000
#: Trigram candidates fetched per term before the edit-distance cut.
CANDIDATES = 40
#: The ``settings`` row holding ``{"transcript_id": int, "full_at": iso}``.
STATE_KEY = "search_vocab_state"
#: How often the whole vocabulary is rebuilt rather than added to.
FULL_REFRESH_EVERY = timedelta(hours=24)

#: One token of a ``tsquery``'s text form: a quoted lexeme (quotes doubled inside), a
#: parenthesis, the NOT operator, or anything else (operators and spaces, copied as is).
_TOKEN = re.compile(r"'((?:[^']|'')*)'(?::[*A-D]+)?|(\()|(\))|(!)|([^'()!]+)")
_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class FuzzyTerm:
    """One query lexeme and the vocabulary words it was widened to."""

    term: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class FuzzyExpansion:
    """What a fuzzy search actually ran.

    ``tsquery`` is the widened query in ``tsquery`` text form, or ``None`` when nothing had
    a variant — the exact query is then the whole answer.  ``terms`` lists every term that
    was looked up, including the ones that found nothing, so the reader can be told.
    """

    tsquery: str | None
    terms: tuple[FuzzyTerm, ...]

    @property
    def widened(self) -> bool:
        return self.tsquery is not None


def max_edits(term: str) -> int:
    """Edits allowed between a term and a variant: one up to five letters, two above."""
    return 1 if len(term) <= 5 else 2  # noqa: PLR2004 - the rule is the docstring


def edit_distance(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance (a transposition is one edit), capped at ``limit + 1``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return min(previous[-1], limit + 1)


def _quote(lexeme: str) -> str:
    """A lexeme as ``tsquery`` text: single-quoted, with ``'`` and ``\\`` doubled."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def _unquote(body: str) -> str:
    return body.replace("''", "'").replace("\\\\", "\\")


def wants_variants(term: str) -> bool:
    """Whether a lexeme is worth widening at all (length, and no digits)."""
    return len(term) >= MIN_TERM_LENGTH and not _DIGIT.search(term)


def positive_terms(query_text: str) -> list[str]:
    """The lexemes of a ``tsquery`` text that are not under a NOT, in order, deduplicated."""
    seen: dict[str, None] = {}

    def collect(term: str) -> list[str]:
        seen[term] = None
        return []

    _rewrite(query_text, collect)
    return list(seen)


def _rewrite(query_text: str, variants_of: Callable[[str], list[str]]) -> str:
    """Replace each positive lexeme with ``( 'term' | 'variant' … )`` where it has variants.

    A lexeme under ``!`` — directly, or anywhere inside a negated group — is left exact:
    ``-drill`` means drill, and quietly excluding *drills and drils and dill* as well
    would be a surprise nobody asked for.
    """
    out: list[str] = []
    negated_groups = [False]
    pending_not = False
    for match in _TOKEN.finditer(query_text):
        lexeme, lparen, rparen, bang, other = match.groups()
        if bang:
            pending_not = True
            out.append("!")
        elif lparen:
            negated_groups.append(negated_groups[-1] or pending_not)
            pending_not = False
            out.append("(")
        elif rparen:
            if len(negated_groups) > 1:
                negated_groups.pop()
            out.append(")")
        elif lexeme is not None:
            negated = negated_groups[-1] or pending_not
            pending_not = False
            term = _unquote(lexeme)
            extra = [] if negated else variants_of(term)
            if extra:
                out.append("( " + " | ".join(_quote(w) for w in [term, *extra]) + " )")
            else:
                out.append(match.group(0))
        else:
            out.append(other)
    return "".join(out)


_CANDIDATES_SQL = text(
    """
    WITH t AS (
        SELECT u.term,
               coalesce((SELECT s.ndoc FROM search_vocab s WHERE s.word = u.term), 0) AS term_ndoc
        FROM unnest(:terms) AS u(term)
    )
    SELECT t.term, v.word, v.ndoc, similarity(v.word, t.term) AS sim, t.term_ndoc
    FROM t
    CROSS JOIN LATERAL (
        SELECT word, ndoc FROM search_vocab
        WHERE word % t.term AND word <> t.term AND word !~ '[0-9]'
        ORDER BY similarity(word, t.term) DESC, ndoc DESC
        LIMIT :candidates
    ) AS v
    WHERE t.term_ndoc < :common
    """
).bindparams(bindparam("terms", type_=ARRAY(Text)))


def variants_for(session: Session, terms: list[str]) -> dict[str, list[str]]:
    """Spelling variants for each term from ``search_vocab``: fewest edits, then most common.

    One round trip for every term: a lateral lookup per term against the trigram index.  A
    term in :data:`COMMON` calls or more gets none, and is not looked up.
    """
    wanted = [t for t in dict.fromkeys(terms) if wants_variants(t)]
    if not wanted:
        return {}
    ranked: dict[str, list[tuple[int, float, int, str]]] = {t: [] for t in wanted}
    rows = session.execute(
        _CANDIDATES_SQL, {"terms": wanted, "candidates": CANDIDATES, "common": COMMON}
    )
    for term, word, ndoc, sim, term_ndoc in rows:
        if term_ndoc >= ESTABLISHED and ndoc > term_ndoc:
            continue  # a different word that happens to be close, not a misspelling of it
        limit = max_edits(term)
        distance = edit_distance(term, word, limit)
        if distance <= limit:
            ranked[term].append((distance, -int(ndoc or 0), -float(sim), word))
    return {term: [w for *_, w in sorted(found)[:MAX_VARIANTS]] for term, found in ranked.items()}


def expand(session: Session, q: str) -> FuzzyExpansion:
    """Widen ``q`` (websearch syntax) to the spellings the archive contains.

    Parsed by PostgreSQL's own ``websearch_to_tsquery``, so what is widened is exactly what
    the exact search would have matched on; the result is checked by casting it back to
    ``tsquery`` before anything relies on it, and a query that will not round-trip falls
    back to exact rather than failing the search.
    """
    parsed = session.scalar(
        select(func.websearch_to_tsquery(literal_column("'english'"), q).cast(Text))
    ) or ""
    terms = positive_terms(parsed)
    found = variants_for(session, terms)
    looked_up = tuple(
        FuzzyTerm(term=t, variants=tuple(found.get(t, ()))) for t in terms if wants_variants(t)
    )
    if not any(term.variants for term in looked_up):
        return FuzzyExpansion(tsquery=None, terms=looked_up)
    widened = _rewrite(parsed, lambda term: found.get(term, []))
    try:
        with session.begin_nested():
            session.execute(text("SELECT CAST(:q AS tsquery)"), {"q": widened})
    except Exception:  # noqa: BLE001 - an odd lexeme must not fail the search
        log.warning("fuzzy: widened query did not parse, searching exactly: %r", widened)
        return FuzzyExpansion(tsquery=None, terms=looked_up)
    return FuzzyExpansion(tsquery=widened, terms=looked_up)


# --------------------------------------------------------------------------------------
# keeping the vocabulary current
# --------------------------------------------------------------------------------------

#: What the vocabulary is built from: every primary call's search document.
_DOCUMENTS = "SELECT search_tsv FROM calls WHERE duplicate_of IS NULL AND search_tsv IS NOT NULL"


def _state(session: Session) -> dict[str, Any]:
    row = session.get(Setting, STATE_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _save_state(session: Session, state: dict[str, Any]) -> None:
    row = session.get(Setting, STATE_KEY)
    if row is None:
        row = Setting(key=STATE_KEY, description="Fuzzy-search vocabulary watermark")
        session.add(row)
    row.value = state


def _due_for_full(state: dict[str, Any], now: datetime) -> bool:
    if "transcript_id" not in state or not state.get("full_at"):
        return True
    try:
        last = datetime.fromisoformat(str(state["full_at"]))
    except ValueError:
        return True
    return now - last >= FULL_REFRESH_EVERY


def refresh_vocabulary(
    session: Session, *, full: bool | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Bring ``search_vocab`` up to date; the caller commits.

    ``full=None`` decides by itself: a full rebuild when there is no watermark yet or the
    last one is a day old, an incremental pass otherwise.  The transcript watermark is
    read *before* the documents are, so a transcript that lands during the pass is picked
    up by the next one rather than lost.
    """
    now = now or datetime.now(UTC)
    state = _state(session)
    if full is None:
        full = _due_for_full(state, now)
    top = int(session.scalar(select(func.coalesce(func.max(Transcript.id), 0))) or 0)
    if full:
        session.execute(text("CREATE TEMP TABLE _search_vocab_new ON COMMIT DROP AS "
                             f"SELECT word, ndoc, nentry FROM ts_stat($${_DOCUMENTS}$$)"))
        removed = session.execute(
            text(
                "DELETE FROM search_vocab v WHERE NOT EXISTS "
                "(SELECT 1 FROM _search_vocab_new n WHERE n.word = v.word)"
            )
        ).rowcount
        written = session.execute(
            text(
                "INSERT INTO search_vocab (word, ndoc, nentry) "
                "SELECT word, ndoc, nentry FROM _search_vocab_new "
                "ON CONFLICT (word) DO UPDATE SET ndoc = EXCLUDED.ndoc, nentry = EXCLUDED.nentry "
                "WHERE (search_vocab.ndoc, search_vocab.nentry) "
                "IS DISTINCT FROM (EXCLUDED.ndoc, EXCLUDED.nentry)"
            )
        ).rowcount
        session.execute(text("DROP TABLE _search_vocab_new"))
        state = {"transcript_id": top, "full_at": now.isoformat()}
        summary = {"mode": "full", "written": written, "removed": removed}
    else:
        since = int(state.get("transcript_id") or 0)
        written = 0
        if top > since:
            # Integers formatted into the ts_stat text: it takes a query string, not
            # parameters, and these two came from ``max(id)`` and our own watermark.
            documents = (
                f"{_DOCUMENTS} AND id IN (SELECT call_id FROM transcripts "
                f"WHERE id > {since:d} AND id <= {top:d})"
            )
            # ``ndoc`` grows by the new documents' counts; a re-transcribed call counts twice
            # until the next full rebuild, which is fine for a tie-break and a hint.
            written = session.execute(
                text(
                    "INSERT INTO search_vocab (word, ndoc, nentry) "
                    f"SELECT word, ndoc, nentry FROM ts_stat($${documents}$$) "
                    "ON CONFLICT (word) DO UPDATE SET ndoc = search_vocab.ndoc + EXCLUDED.ndoc, "
                    "nentry = search_vocab.nentry + EXCLUDED.nentry"
                )
            ).rowcount
        state = {**state, "transcript_id": max(top, since)}
        summary = {"mode": "incremental", "since": since, "to": top, "written": written}
    _save_state(session, state)
    summary["words"] = int(session.scalar(text("SELECT count(*) FROM search_vocab")) or 0)
    return summary


__all__ = [
    "FuzzyExpansion",
    "FuzzyTerm",
    "edit_distance",
    "expand",
    "positive_terms",
    "refresh_vocabulary",
    "variants_for",
]
