"""Domain prompt / hotword assembly, including the truncation budget."""

from __future__ import annotations

from app.stt.prompt import DEFAULT_PLACES, build_hotwords, build_prompt

TALKGROUPS = ["EC FIRE DISPATCH", "EC EMS OPS", "EC V-LAW 31+", "TRANSIT", "EC EVENT 1"]
UNITS = ["Engine 1", "Medic 21", "EFD 2", "Ladder 3"]


def test_full_prompt_reads_like_prior_radio_traffic():
    prompt = build_prompt(
        system_name="Example System Example County",
        talkgroup_aliases=TALKGROUPS,
        unit_aliases=UNITS,
        places=["Lakeside", "Riverton", "North Fork"],
        extra="ten-four, signal 30, MABAS box alarm",
    )
    assert prompt.startswith("Example System Example County public safety radio traffic.")
    assert "Talkgroups: EC FIRE DISPATCH, EC EMS OPS" in prompt
    assert "Units: Engine 1, Medic 21, EFD 2, Ladder 3." in prompt
    assert "Places: Lakeside, Riverton, North Fork." in prompt
    assert prompt.endswith("ten-four, signal 30, MABAS box alarm.")


def test_places_default_to_none():
    """No site's towns are built in; a deployment names its own with ``STT_PLACES``."""
    assert DEFAULT_PLACES == ()
    prompt = build_prompt(system_name="Example System")
    assert prompt == "Example System public safety radio traffic."
    assert "Places" not in prompt


def test_empty_inputs_still_give_a_usable_lead():
    assert build_prompt(places=[]) == "Public safety radio traffic."


def test_sections_are_omitted_when_empty():
    prompt = build_prompt(system_name="Example System", talkgroup_aliases=[], unit_aliases=[], places=[])
    assert prompt == "Example System public safety radio traffic."


def test_truncation_respects_max_chars_and_never_splits_an_item():
    aliases = [f"TALKGROUP ALIAS NUMBER {i:03d}" for i in range(200)]
    prompt = build_prompt(
        system_name="Example System", talkgroup_aliases=aliases, unit_aliases=UNITS, max_chars=200
    )
    assert len(prompt) <= 200
    # every rendered alias is complete
    listed = prompt.split("Talkgroups: ")[1].split(".")[0]
    for item in listed.split(", "):
        assert item in aliases


def test_extra_vocabulary_survives_a_huge_alias_list():
    aliases = [f"ALIAS {i:03d}" for i in range(500)]
    prompt = build_prompt(
        system_name="Example System", talkgroup_aliases=aliases, extra="signal 30", max_chars=300
    )
    assert len(prompt) <= 300
    assert prompt.endswith("signal 30.")


def test_default_max_chars_is_under_the_whisper_prompt_window():
    prompt = build_prompt(
        system_name="Example System",
        talkgroup_aliases=[f"TALKGROUP {i}" for i in range(400)],
        unit_aliases=[f"UNIT {i}" for i in range(400)],
    )
    assert len(prompt) <= 800


def test_zero_budget_returns_empty():
    assert build_prompt(system_name="Example System", max_chars=0) == ""


def test_tiny_budget_truncates_the_lead():
    prompt = build_prompt(system_name="Example System", max_chars=12)
    assert len(prompt) <= 12


def test_duplicates_and_whitespace_are_cleaned():
    prompt = build_prompt(
        talkgroup_aliases=["  EC FIRE  DISPATCH ", "52 fire dispatch", "", "  "],
        unit_aliases=None,
        places=[],
    )
    assert prompt.count("FIRE") == 1
    assert "EC FIRE DISPATCH" in prompt


# --------------------------------------------------------------------------- hotwords


def test_hotwords_prefers_units_then_talkgroups_then_places():
    hotwords = build_hotwords(unit_aliases=UNITS, talkgroup_aliases=TALKGROUPS, places=["Lakeside"])
    assert hotwords is not None
    assert hotwords.startswith("Engine 1 Medic 21")
    assert hotwords.endswith("Lakeside")


def test_hotwords_returns_none_when_nothing_to_bias():
    assert build_hotwords(places=[]) is None


def test_hotwords_respects_max_chars():
    hotwords = build_hotwords(unit_aliases=[f"UNIT {i:03d}" for i in range(200)], places=[], max_chars=50)
    assert hotwords is not None and len(hotwords) <= 50


def test_hotwords_dedupes():
    hotwords = build_hotwords(unit_aliases=["Engine 1", "engine 1"], places=[])
    assert hotwords == "Engine 1"
