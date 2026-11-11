"""Domain prompt / hotword assembly (ARCHITECTURE §5.2).

Whisper's ``initial_prompt`` is prior text, not an instruction: it biases the decoder toward the
vocabulary it contains.  Feeding it the talkgroup aliases, unit ids and place names that actually
occur on this system is what turns "Johnny, I'm 16" into a usable transcript.

The prompt is capped because whisper truncates ``initial_prompt`` to the last 224 tokens of its
context — anything beyond that is silently dropped, and an over-long prompt measurably increases
hallucination on short clips.  ``max_chars=800`` sits comfortably under that limit.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["DEFAULT_PLACES", "build_hotwords", "build_prompt"]

#: The places every deployment shares: none.  A site's own place names come from the
#: ``STT_PLACES`` setting (:attr:`app.config.Settings.stt_places`), which the worker appends.
DEFAULT_PLACES: tuple[str, ...] = ()

_LEAD = "public safety radio traffic"


def _clean(values: Iterable[str] | None) -> list[str]:
    """Strip, drop blanks, de-duplicate case-insensitively, preserve caller order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = " ".join(str(value).split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _fit(label: str, items: list[str], budget: int) -> str:
    """Render ``"<label>: a, b, c."`` using at most ``budget`` characters, dropping the tail.

    Items are dropped whole — a half-written unit id would bias the decoder toward nonsense.
    """
    if not items or budget <= 0:
        return ""
    prefix = f"{label}: "
    parts: list[str] = []
    used = len(prefix) + 1  # trailing '.'
    for item in items:
        extra = len(item) + (2 if parts else 0)
        if used + extra > budget:
            break
        parts.append(item)
        used += extra
    if not parts:
        return ""
    return prefix + ", ".join(parts) + "."


def build_prompt(
    *,
    system_name: str = "",
    talkgroup_aliases: Iterable[str] | None = None,
    unit_aliases: Iterable[str] | None = None,
    places: Iterable[str] | None = None,
    extra: str = "",
    max_chars: int = 800,
) -> str:
    """Assemble the domain prompt sent as ``prompt`` / ``initial_prompt``.

    Sections are added in order of value — the lead sentence and the talkgroups that name the
    agencies on air matter most, the place list least — and each is fitted to whatever budget
    is left, so the result never exceeds ``max_chars``.
    """
    if max_chars <= 0:
        return ""

    lead = f"{system_name.strip()} {_LEAD}." if system_name.strip() else f"{_LEAD.capitalize()}."
    if len(lead) > max_chars:
        return lead[:max_chars].rstrip()

    sections: list[str] = [lead]
    remaining = max_chars - len(lead)

    extra_text = " ".join(extra.split())
    ordered: list[tuple[str, list[str]]] = [
        ("Talkgroups", _clean(talkgroup_aliases)),
        ("Units", _clean(unit_aliases)),
        ("Places", _clean(places if places is not None else DEFAULT_PLACES)),
    ]

    # Reserve room for the user's free-text vocabulary so a huge alias list cannot crowd it out.
    reserved = min(len(extra_text) + 1, remaining // 3) if extra_text else 0

    for label, items in ordered:
        budget = remaining - reserved - 1  # -1 for the joining space
        rendered = _fit(label, items, budget)
        if rendered:
            sections.append(rendered)
            remaining -= len(rendered) + 1

    if extra_text:
        if not extra_text.endswith((".", "!", "?")):
            extra_text += "."
        if len(extra_text) + 1 <= remaining:
            sections.append(extra_text)

    return " ".join(sections)


def build_hotwords(
    *,
    talkgroup_aliases: Iterable[str] | None = None,
    unit_aliases: Iterable[str] | None = None,
    places: Iterable[str] | None = None,
    extra_terms: Iterable[str] | None = None,
    max_chars: int = 400,
) -> str | None:
    """Build the ``hotwords`` bias string, or ``None`` when there is nothing to bias toward.

    Hotwords are a stronger, narrower lever than the prompt, so this keeps only proper nouns and
    identifiers: the units on air, the places they are dispatched to and the operator's own
    vocabulary list.  Returning ``None`` (not ``""``) matters — the backend omits the field
    entirely, because an empty hotwords string is a real, empty bias list to the server.
    """
    terms = _clean(
        [
            *(unit_aliases or ()),
            *(talkgroup_aliases or ()),
            *(places if places is not None else DEFAULT_PLACES),
            *(extra_terms or ()),
        ]
    )
    if not terms:
        return None
    out: list[str] = []
    used = 0
    for term in terms:
        extra = len(term) + (1 if out else 0)
        if used + extra > max_chars:
            break
        out.append(term)
        used += extra
    return " ".join(out) or None
