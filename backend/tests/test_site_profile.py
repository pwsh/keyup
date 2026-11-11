"""The deployment's own site profile (``deploy/site/``), when this checkout has one.

``deploy/site/`` holds one installation's agency list, classification rules and shared
channel list; it is excluded from the public tree, so these tests skip there.  Where it is
present they check what a deploy would otherwise find out in production: that the agency
CSV seeds, that every rule loads and names an agency the CSV has, and that the shared tgid
list parses.  The format of each is the same as ``examples/``.
"""

from __future__ import annotations

import csv

import pytest

from app import classify
from app.models import AGENCY_CODE_PATTERN, AGENCY_KINDS

from .conftest import BACKEND_DIR, EXAMPLE_AGENCIES, EXAMPLE_RULES

SITE = BACKEND_DIR.parent / "deploy" / "site"

PROFILES = [pytest.param(EXAMPLE_AGENCIES, EXAMPLE_RULES, None, id="examples")]
if SITE.is_dir():
    PROFILES.append(
        pytest.param(
            SITE / "agencies.csv",
            SITE / "classification.csv",
            SITE / "shared-talkgroups.txt",
            id="deploy-site",
        )
    )


@pytest.mark.parametrize(("agencies", "rules", "shared"), PROFILES)
def test_the_profile_is_coherent(agencies, rules, shared) -> None:  # noqa: ANN001
    import re

    with agencies.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, agencies
    codes = {row["code"].strip() for row in rows}
    assert len(codes) == len(rows), "duplicate agency codes"
    for row in rows:
        assert re.fullmatch(AGENCY_CODE_PATTERN, row["code"].strip()), row
        assert row["kind"].strip() in AGENCY_KINDS, row
        assert row["name"].strip() and row["short_name"].strip(), row

    loaded = classify.load_rules(rules)
    assert loaded
    named = {rule.agency_code for rule in loaded if rule.agency_code}
    assert named <= codes, f"rules name agencies the CSV lacks: {sorted(named - codes)}"

    if shared is not None:
        from app.cli import _read_tgid_file

        tgids = _read_tgid_file(shared)
        assert tgids and len(set(tgids)) == len(tgids)
