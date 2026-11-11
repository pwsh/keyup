"""The vocabulary descriptions, and the docs page that renders them.

No database: this is a pure module and a Markdown file, and the whole reason both exist is
that the *lists* in ``app.models`` and the *sentences* in ``app.vocabulary`` must not drift
apart from each other or from ``docs/TALKGROUPS.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models import (
    AGENCY_KINDS,
    AGENCY_SOURCES,
    RR_SERVICE_TAGS,
    SERVICE_TAG_TO_CATEGORY,
    TALKGROUP_CATEGORIES,
)
from app.vocabulary import (
    AGENCY_KIND_DESCRIPTIONS,
    AGENCY_SOURCE_DESCRIPTIONS,
    CATEGORY_DESCRIPTIONS,
    DOC_BLOCKS,
    SERVICE_TAG_DESCRIPTIONS,
    categories,
    fenced_block,
    service_tags,
    vocabulary,
)

DOCS = Path(__file__).resolve().parents[2] / "docs" / "TALKGROUPS.md"


def _sentences(mapping: dict[str, str]) -> list[tuple[str, str]]:
    return sorted(mapping.items())


class TestCoverage:
    """Every value in every list has a sentence, and no sentence is orphaned."""

    def test_every_category_is_described(self) -> None:
        assert sorted(CATEGORY_DESCRIPTIONS) == sorted(TALKGROUP_CATEGORIES)

    def test_every_service_tag_is_described(self) -> None:
        assert sorted(SERVICE_TAG_DESCRIPTIONS) == sorted(RR_SERVICE_TAGS)

    def test_every_agency_kind_is_described(self) -> None:
        assert sorted(AGENCY_KIND_DESCRIPTIONS) == sorted(AGENCY_KINDS)

    def test_every_agency_source_is_described(self) -> None:
        assert sorted(AGENCY_SOURCE_DESCRIPTIONS) == sorted(AGENCY_SOURCES)

    @pytest.mark.parametrize(
        "key,text",
        _sentences(CATEGORY_DESCRIPTIONS)
        + _sentences(SERVICE_TAG_DESCRIPTIONS)
        + _sentences(AGENCY_KIND_DESCRIPTIONS)
        + _sentences(AGENCY_SOURCE_DESCRIPTIONS),
    )
    def test_each_description_is_a_real_sentence(self, key: str, text: str) -> None:
        assert text == text.strip(), key
        assert len(text) > 25, key
        assert text[0].isupper(), key
        assert text.endswith((".", "?")), key
        assert not re.match(r"^(TODO|TBD|FIXME)", text, re.I), key


class TestMapping:
    """The two tables are two views of one mapping, so they have to agree."""

    def test_every_tag_maps_to_a_real_category(self) -> None:
        assert set(SERVICE_TAG_TO_CATEGORY.values()) <= set(TALKGROUP_CATEGORIES)

    def test_the_category_table_is_the_reverse_of_the_tag_table(self) -> None:
        reversed_ = {c["key"]: c["service_tags"] for c in categories()}
        for tag in RR_SERVICE_TAGS:
            assert tag in reversed_[SERVICE_TAG_TO_CATEGORY[tag]]
        # …and nothing extra crept in.
        assert sorted(t for tags in reversed_.values() for t in tags) == sorted(RR_SERVICE_TAGS)

    def test_the_payload_is_ordered_like_the_source_lists(self) -> None:
        payload = vocabulary()
        assert [c["key"] for c in payload["categories"]] == list(TALKGROUP_CATEGORIES)
        assert [t["tag"] for t in payload["service_tags"]] == list(RR_SERVICE_TAGS)
        assert [k["key"] for k in payload["agency_kinds"]] == list(AGENCY_KINDS)

    def test_every_tag_carries_its_category_label(self) -> None:
        by_key = {c["key"]: c["label"] for c in categories()}
        for tag in service_tags():
            assert tag["category_label"] == by_key[tag["category"]]


class TestDocs:
    """``docs/TALKGROUPS.md`` renders the code, and is checked here rather than trusted."""

    def test_the_page_exists_and_names_the_three_fields(self) -> None:
        text = DOCS.read_text()
        for heading in ("## Agencies", "## Categories", "## Service tags"):
            assert heading in text

    @pytest.mark.parametrize("name", DOC_BLOCKS)
    def test_each_generated_table_matches_the_code(self, name: str) -> None:
        text = DOCS.read_text()
        match = re.search(
            rf"<!-- generated:{re.escape(name)} -->.*?<!-- /generated:{re.escape(name)} -->",
            text,
            re.DOTALL,
        )
        assert match is not None, f"docs/TALKGROUPS.md has no generated:{name} block"
        assert match.group(0) == fenced_block(name), (
            f"the {name} table in docs/TALKGROUPS.md is out of date — run "
            "`cd backend && .venv/bin/python tools/render_vocabulary_docs.py`"
        )
