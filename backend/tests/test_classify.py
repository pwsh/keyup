"""The rules-based talkgroup classifier (``app/classify.py``, ``keyup classify-talkgroups``).

The rules are the shipped example, ``examples/classification.example.csv``, seeded against
``examples/agencies.example.csv`` — so this file doubles as the check that the example
profile is coherent: every rule loads, every agency it names exists, and it maps the
aliases below the way its notes say.  If a rule is reordered or a pattern is loosened,
the expectations below are where it shows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import classify
from app.models import Agency, Radio, RadioAffiliation, System, Talkgroup
from app.seed import seed_agencies

from .conftest import EXAMPLE_AGENCIES, EXAMPLE_RULES, requires_db

#: alias → (category, service_tag, agency_code) — what the example rules say, verbatim.
EXPECTED: dict[str, tuple[str, str, str | None]] = {
    "EXAMPLE LAW 1": ("law", "Law Dispatch", None),
    "EXAMPLE LAW 2": ("law", "Law Dispatch", None),
    "EXAMPLE LAW 3": ("law", "Law Dispatch", None),
    "CCPD DISPATCH": ("law", "Law Dispatch", "EXAMPLEPD"),
    "CCPD CAR": ("law", "Law Tac", "EXAMPLEPD"),
    "EXAMPLE FIRE DISPATCH": ("fire", "Fire Dispatch", None),
    "CCFD Tactical": ("fire", "Fire-Tac", "EXAMPLEFD"),
    "EXAMPLE F-TAC 1": ("fire", "Fire-Tac", None),
    "EXAMPLE F-TAC 2": ("fire", "Fire-Tac", None),
    "EXAMPLE F TAC 3": ("fire", "Fire-Tac", None),
    "EXAMPLE EMS TAC": ("ems", "EMS-Tac", None),
    "EXAMPLE EVENT 1": ("multi", "Multi-Tac", None),
    "EXAMPLE EVENT 4": ("multi", "Multi-Tac", None),
    "Example Public Works": ("pw", "Public Works", None),
}

#: Aliases no rule may claim.  ``EXAMPLE EVENT 5`` and ``EXAMPLE LAW 7`` sit just outside
#: their rules' ranges and are the cheapest proof that those ranges are ranges and not
#: prefixes; ``XCCPD CAR`` is the proof that the word boundary holds.
UNMATCHED = ["EXAMPLE DATA 1", "EXAMPLE EVENT 5", "EXAMPLE LAW 7", "XCCPD CAR", "TOWN PW ROADS"]


# --------------------------------------------------------------------------------------
# the rules file itself — no database needed
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules() -> list[classify.Rule]:
    return classify.load_rules(EXAMPLE_RULES)


def test_there_is_no_default_rules_file() -> None:
    """The rules are a deployment's own; the loader has nothing to fall back on."""
    with pytest.raises(TypeError):
        classify.load_rules()  # type: ignore[call-arg]


def test_example_rules_load(rules) -> None:  # noqa: ANN001
    """Every category and service tag in the example file is one the app knows."""
    assert len(rules) >= 8
    assert all(rule.sets for rule in rules)
    # Every rule explains itself; the file is read by people, not only by the loader.
    assert all(rule.note for rule in rules)


@pytest.mark.parametrize(("alias", "expected"), sorted(EXPECTED.items()))
def test_alias_maps_as_the_operator_says(rules, alias, expected) -> None:  # noqa: ANN001
    rule = classify.match(rules, alias)
    assert rule is not None, f"{alias!r} matched no rule"
    category, service_tag, agency = expected
    assert rule.category == category
    assert rule.service_tag == service_tag
    assert rule.agency_code == agency


@pytest.mark.parametrize("alias", UNMATCHED)
def test_unlisted_aliases_are_left_alone(rules, alias) -> None:  # noqa: ANN001
    assert classify.match(rules, alias) is None


def test_the_specific_rule_is_read_first(rules) -> None:  # noqa: ANN001
    """``CCPD DISPATCH`` matches both CCPD rules; file order is what makes it dispatch."""
    assert classify.match(rules, "CCPD DISPATCH").service_tag == "Law Dispatch"
    assert classify.match(rules, "CCPD CAR").service_tag == "Law Tac"


def test_a_blank_alias_matches_nothing(rules) -> None:  # noqa: ANN001
    assert classify.match(rules, None) is None
    assert classify.match(rules, "   ") is None


def test_a_bad_category_is_refused(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "rules.csv"
    path.write_text("pattern,category,service_tag,agency_code,note\nFOO,nonsense,,,\n")
    with pytest.raises(classify.RulesError, match="not a talkgroup category"):
        classify.load_rules(path)


def test_a_bad_service_tag_is_refused(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "rules.csv"
    path.write_text("pattern,category,service_tag,agency_code,note\nFOO,law,Fire Brigade,,\n")
    with pytest.raises(classify.RulesError, match="RadioReference service tag"):
        classify.load_rules(path)


def test_a_bad_regex_names_its_line(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "rules.csv"
    path.write_text("pattern,category,service_tag,agency_code,note\n\"FOO(\",law,,,\n")
    with pytest.raises(classify.RulesError, match=r"rules\.csv:2"):
        classify.load_rules(path)


def test_a_rule_that_sets_nothing_is_refused(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "rules.csv"
    path.write_text("pattern,category,service_tag,agency_code,note\nFOO,,,,just a note\n")
    with pytest.raises(classify.RulesError, match="sets no columns"):
        classify.load_rules(path)


def test_comments_are_not_rules(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "rules.csv"
    path.write_text(
        "# a leading comment\npattern,category,service_tag,agency_code,note\n"
        "# and one in the middle\nFOO,law,,,\n"
    )
    loaded = classify.load_rules(path)
    assert [rule.source for rule in loaded] == ["FOO"]


# --------------------------------------------------------------------------------------
# planning and applying against the database
# --------------------------------------------------------------------------------------


def _seed(session, aliases: list[str]) -> System:  # noqa: ANN001
    seed_agencies(session, path=EXAMPLE_AGENCIES)
    system = System(name="Example System")
    session.add(system)
    session.flush()
    for index, alias in enumerate(aliases):
        session.add(Talkgroup(system_id=system.id, tgid=1100 + index, alias=alias))
    session.commit()
    return system


@requires_db
def test_plan_fills_the_operators_mapping(session, rules) -> None:  # noqa: ANN001
    _seed(session, [*EXPECTED, *UNMATCHED])
    plan = classify.plan_talkgroups(session, rules)

    assert plan["unknown_agencies"] == []
    assert plan["talkgroups_matched"] == len(EXPECTED)
    assert plan["talkgroups_unmatched"] == len(UNMATCHED)
    assert plan["talkgroups_changed"] == len(EXPECTED)

    classify.apply_changes(session, plan["changes"])
    session.commit()

    for alias, (category, service_tag, agency) in EXPECTED.items():
        tg = session.scalars(select(Talkgroup).where(Talkgroup.alias == alias)).one()
        assert (tg.category, tg.service_tag, tg.agency_code) == (category, service_tag, agency)

    for alias in UNMATCHED:
        tg = session.scalars(select(Talkgroup).where(Talkgroup.alias == alias)).one()
        assert (tg.category, tg.service_tag, tg.agency_code) == (None, None, None)


@requires_db
def test_a_second_run_changes_nothing(session, rules) -> None:  # noqa: ANN001
    _seed(session, list(EXPECTED))
    first = classify.plan_talkgroups(session, rules)
    classify.apply_changes(session, first["changes"])
    session.commit()

    again = classify.plan_talkgroups(session, rules)
    assert again["talkgroups_changed"] == 0
    assert again["columns_set"] == 0


@requires_db
def test_an_operators_value_is_kept_and_reported(session, rules) -> None:  # noqa: ANN001
    """Fill-only is the default, and the value that was not overwritten is named."""
    _seed(session, ["EXAMPLE LAW 1"])
    tg = session.scalars(select(Talkgroup).where(Talkgroup.alias == "EXAMPLE LAW 1")).one()
    tg.category = "interop"
    session.commit()

    plan = classify.plan_talkgroups(session, rules)
    classify.apply_changes(session, plan["changes"])
    session.commit()
    session.refresh(tg)

    assert tg.category == "interop"  # the operator's, untouched
    assert tg.service_tag == "Law Dispatch"  # the blank column still filled
    assert plan["changes"][0].kept == {"category": "interop"}
    assert plan["columns_kept"] == 1


@requires_db
def test_overwrite_replaces_it(session, rules) -> None:  # noqa: ANN001
    _seed(session, ["EXAMPLE LAW 1"])
    tg = session.scalars(select(Talkgroup).where(Talkgroup.alias == "EXAMPLE LAW 1")).one()
    tg.category = "interop"
    session.commit()

    plan = classify.plan_talkgroups(session, rules, overwrite=True)
    classify.apply_changes(session, plan["changes"])
    session.commit()
    session.refresh(tg)
    assert tg.category == "law"


@requires_db
def test_an_agency_code_that_does_not_exist_is_refused_before_any_write(session, tmp_path) -> None:  # noqa: ANN001
    """The FK would fail partway through a batch; the plan says which code is wrong instead."""
    _seed(session, ["EXAMPLE LAW 1"])
    path = tmp_path / "rules.csv"
    path.write_text("pattern,category,service_tag,agency_code,note\n\\bLAW\\b,law,,NOSUCHCODE,\n")
    plan = classify.plan_talkgroups(session, classify.load_rules(path))
    assert plan["unknown_agencies"] == ["NOSUCHCODE"]


@requires_db
def test_system_narrows_the_pass(session, rules) -> None:  # noqa: ANN001
    _seed(session, ["EXAMPLE LAW 1"])
    other = System(name="Neighbouring System")
    session.add(other)
    session.flush()
    session.add(Talkgroup(system_id=other.id, tgid=1101, alias="EXAMPLE LAW 2"))
    session.commit()

    plan = classify.plan_talkgroups(session, rules, system="Example System")
    assert [c.alias for c in plan["changes"]] == ["EXAMPLE LAW 1"]

    with pytest.raises(ValueError, match="no system named"):
        classify.plan_talkgroups(session, rules, system="Nowhere")


# --------------------------------------------------------------------------------------
# radios
# --------------------------------------------------------------------------------------


def _radio(session, system: System, radio_id: int) -> Radio:  # noqa: ANN001
    radio = Radio(system_id=system.id, radio_id=radio_id, wacn=0, home_system_id=0, kind="unknown")
    session.add(radio)
    session.flush()
    return radio


@requires_db
def test_a_radios_agency_comes_from_where_it_is_heard(session, rules) -> None:  # noqa: ANN001
    system = _seed(session, ["CCPD CAR", "CCFD TAC"])
    plan = classify.plan_talkgroups(session, rules)
    classify.apply_changes(session, plan["changes"])
    session.commit()

    police, fire = session.scalars(select(Talkgroup).order_by(Talkgroup.tgid)).all()
    radio = _radio(session, system, 9200001)
    session.add_all(
        [
            RadioAffiliation(radio_id_fk=radio.id, talkgroup_id_fk=police.id, call_count=40),
            RadioAffiliation(radio_id_fk=radio.id, talkgroup_id_fk=fire.id, call_count=3),
        ]
    )
    session.commit()

    radio_plan = classify.plan_radios(session)
    assert radio_plan["radios_changed"] == 1
    change = radio_plan["changes"][0]
    assert (change.agency_code, change.calls, change.total) == ("EXAMPLEPD", 40, 43)

    classify.apply_radio_changes(session, radio_plan["changes"])
    session.commit()
    session.refresh(radio)
    assert radio.agency_code == "EXAMPLEPD"


@requires_db
def test_a_tied_radio_is_left_alone(session, rules) -> None:  # noqa: ANN001
    """Two agencies, equal traffic: a real thing, and not one to guess at."""
    system = _seed(session, ["CCPD CAR", "CCFD TAC"])
    plan = classify.plan_talkgroups(session, rules)
    classify.apply_changes(session, plan["changes"])
    session.commit()

    police, fire = session.scalars(select(Talkgroup).order_by(Talkgroup.tgid)).all()
    radio = _radio(session, system, 9200002)
    session.add_all(
        [
            RadioAffiliation(radio_id_fk=radio.id, talkgroup_id_fk=police.id, call_count=9),
            RadioAffiliation(radio_id_fk=radio.id, talkgroup_id_fk=fire.id, call_count=9),
        ]
    )
    session.commit()

    radio_plan = classify.plan_radios(session)
    assert radio_plan["radios_changed"] == 0
    assert radio_plan["radios_tied"] == 1
    session.refresh(radio)
    assert radio.agency_code is None


@requires_db
def test_a_radio_with_an_agency_is_kept(session, rules) -> None:  # noqa: ANN001
    system = _seed(session, ["CCPD CAR"])
    plan = classify.plan_talkgroups(session, rules)
    classify.apply_changes(session, plan["changes"])
    session.commit()

    tg = session.scalars(select(Talkgroup)).one()
    radio = _radio(session, system, 9200003)
    radio.agency_code = "EXAMPLEFD"
    session.add(RadioAffiliation(radio_id_fk=radio.id, talkgroup_id_fk=tg.id, call_count=99))
    session.commit()

    assert classify.plan_radios(session)["radios_kept"] == 1
    assert classify.plan_radios(session, overwrite=True)["radios_changed"] == 1


@requires_db
def test_unclassified_channels_teach_nothing(session, rules) -> None:  # noqa: ANN001
    """A radio only ever heard on channels with no agency contributes no evidence."""
    system = _seed(session, ["EXAMPLE DATA 1"])
    tg = session.scalars(select(Talkgroup)).one()
    radio = _radio(session, system, 9200004)
    session.add(RadioAffiliation(radio_id_fk=radio.id, talkgroup_id_fk=tg.id, call_count=99))
    session.commit()

    assert classify.plan_radios(session)["radios_changed"] == 0
    assert session.get(Agency, "EXAMPLEPD") is not None  # the table is seeded; the evidence is not
