"""The RadioReference service-tag vocabulary and the category it maps to.

``service_tag`` is RR's classification of a channel, kept in RR's own words; ``category``
is this archive's coarse bucket, the one the UI colors and the calls filter groups by.
The two are joined by :data:`SERVICE_TAG_TO_CATEGORY`, and these tests are what stops that
join going stale: a tag added to the vocabulary without a mapping, or a mapping that
points at a category the UI has never heard of, would both leave a synced talkgroup in a
bucket nothing draws.
"""

from __future__ import annotations

from app.models import (
    RR_SERVICE_TAGS,
    SERVICE_TAG_TO_CATEGORY,
    TALKGROUP_CATEGORIES,
    canonical_service_tag,
)


def test_every_service_tag_maps_to_a_category_the_ui_knows() -> None:
    assert set(SERVICE_TAG_TO_CATEGORY) == set(RR_SERVICE_TAGS)
    assert set(SERVICE_TAG_TO_CATEGORY.values()) <= set(TALKGROUP_CATEGORIES)


def test_the_categories_still_contain_the_original_eight() -> None:
    """The list was widened, not replaced — which is why no row needed rewriting."""
    original = {"law", "fire", "ems", "pw", "transit", "interop", "schools", "other"}
    assert original <= set(TALKGROUP_CATEGORIES)
    assert len(set(TALKGROUP_CATEGORIES)) == len(TALKGROUP_CATEGORIES)


def test_a_service_tag_is_recognized_however_it_is_spelled() -> None:
    assert canonical_service_tag("fire dispatch") == "Fire Dispatch"
    assert canonical_service_tag("  EMS-TAC ") == "EMS-Tac"
    assert canonical_service_tag("Public Works") == "Public Works"


def test_nothing_and_nonsense_both_come_back_as_none() -> None:
    """Blank and NULL both mean "unclassified", so they must not become each other."""
    assert canonical_service_tag(None) is None
    assert canonical_service_tag("") is None
    assert canonical_service_tag("   ") is None
    assert canonical_service_tag("Weather Net") is None
