"""The spec and its consumers must not drift apart.

:data:`app.runtime_settings.SETTINGS_SPEC` is a whitelist of keys the Admin UI may write
and :func:`app.runtime_settings.effective_settings` may layer.  Nothing else connects a
key to the code that reads it: consumers reach for ``settings.<key>``, and the exporters
reach for it by name — ``f"export_{service}_enabled"``, ``f"export_{service}_systems"``.
A spec key with no matching field on :class:`app.config.Settings` is therefore a switch
that renders in the Admin page, saves without complaint, and does nothing at all; the
mistake is invisible until an operator flips it.

These tests are that mapping guarantee.  They need no database: the only thing
:func:`effective_settings` asks a session for is the ``settings`` table's rows, which a
two-line stub can supply.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.models import Setting
from app.runtime_settings import (
    SETTABLE_KEYS,
    SETTINGS_OPTIONS,
    SETTINGS_SPEC,
    WRITE_ONLY_KEYS,
    effective_settings,
    invalidate,
)

#: The kinds the Admin UI knows how to render and coerce.
KINDS = {"bool", "int", "float", "string", "secret", "text", "enum"}


class _StubSession:
    """Just enough of a :class:`~sqlalchemy.orm.Session` for ``read_overrides``."""

    def __init__(self, rows: list[Setting] | None = None) -> None:
        self._rows = rows or []

    def scalars(self, _statement: Any) -> list[Setting]:
        return self._rows


def _effective(rows: list[Setting] | None = None) -> Settings:
    """``effective_settings`` over a fixed set of rows, with the module cache cleared."""
    invalidate()
    try:
        return effective_settings(_StubSession(rows), base=Settings(_env_file=None))
    finally:
        invalidate()


def test_every_spec_key_is_a_field_of_the_effective_settings() -> None:
    """The guarantee: a key in the spec is a key a consumer can actually read."""
    merged = _effective()
    orphans = sorted(key for key, *_ in SETTINGS_SPEC if not hasattr(merged, key))
    assert orphans == []


def test_the_exporters_name_their_keys_by_service_and_all_of_them_resolve() -> None:
    """The f-string lookups in :mod:`app.exporters` are keys too, and are not searchable."""
    merged = _effective()
    for service in ("broadcastify", "openmhz"):
        for template in ("export_{}_enabled", "export_{}_systems", "export_{}_talkgroup_mode"):
            key = template.format(service)
            assert key in SETTABLE_KEYS, f"{key} is not settable"
            assert hasattr(merged, key), f"{key} has no field on Settings"


def test_an_override_reaches_the_attribute_it_names() -> None:
    """Not only present, but written through: the layering is what makes the spec real."""
    merged = _effective([Setting(key="export_max_attempts", value=9)])
    assert merged.export_max_attempts == 9


def test_the_export_loudness_is_settable_and_reaches_its_field() -> None:
    """The export level is a runtime knob, not a redeploy: spec key, field, and layering."""
    spec = {key: (kind, group, label) for key, kind, group, label, *_ in SETTINGS_SPEC}
    assert spec["export_target_lufs"] == ("float", "Export", "Export loudness")
    assert _effective().export_target_lufs == -11.0
    assert _effective([Setting(key="export_target_lufs", value=-9.0)]).export_target_lufs == -9.0


def test_the_spec_is_well_formed() -> None:
    """Duplicates, unknown kinds and an ``enum`` with no options all break the Admin form."""
    keys = [key for key, *_ in SETTINGS_SPEC]
    assert len(keys) == len(set(keys)), "a key is listed twice"
    for key, kind, group, label, description in SETTINGS_SPEC:
        assert kind in KINDS, f"{key}: unknown kind {kind}"
        assert group and label and description, f"{key}: an empty column"
        if kind == "enum":
            assert SETTINGS_OPTIONS.get(key), f"{key}: an enum with no options"
    assert set(SETTINGS_OPTIONS) <= set(keys)
    assert set(keys) >= WRITE_ONLY_KEYS


def test_every_group_has_a_home_in_the_ui() -> None:
    """The Settings tab renders every group except the three with pages of their own.

    ``DEDICATED_GROUPS`` in ``frontend/src/pages/Admin.tsx`` holds these back; the Exports
    tab, the Feed Stats page and the Audio tab render them instead.  A *new* group is
    rendered by the generic form and needs nothing here — this only pins the three, so
    renaming one on the backend cannot silently strand it in neither place.
    """
    groups = {group for _, _, group, *_ in SETTINGS_SPEC}
    assert {"Export", "Feed stats", "Audio"} <= groups


def test_the_broadcastify_upload_at_switch_is_an_enum_with_the_two_answers() -> None:
    """Who uploads to Broadcastify Calls is a two-valued choice, and `keyup` is the default.

    Spelled out rather than left to the generic well-formedness test because the *values*
    are a contract: trunkd reads them off the desired-state block, and a third answer
    appearing here would be a recorder that does not know what to do.
    """
    merged = _effective()
    assert merged.broadcastify_upload_at == "keyup"
    assert SETTINGS_OPTIONS["broadcastify_upload_at"] == ["keyup", "recorder"]
    (row,) = [entry for entry in SETTINGS_SPEC if entry[0] == "broadcastify_upload_at"]
    assert row[1] == "enum"
    assert row[2] == "Export"
    assert row[3] == "Broadcastify Calls uploaded by"
    assert _effective(
        [Setting(key="broadcastify_upload_at", value="recorder")]
    ).broadcastify_upload_at == "recorder"


def test_the_merge_over_the_process_settings_is_memoised_and_invalidated() -> None:
    """``base=get_settings()`` — ~35 call sites, hot on every upload and job — reuses one
    merged object instead of re-validating ~190 fields; a settings write drops it."""
    from app import runtime_settings
    from app.config import get_settings

    invalidate()
    try:
        rows = [Setting(key="broadcastify_upload_at", value="recorder")]
        first = effective_settings(_StubSession(rows), base=get_settings())
        again = effective_settings(_StubSession(rows), base=get_settings())
        assert again is first
        assert effective_settings(_StubSession(rows)) is first  # base=None is the same thing
        assert first.broadcastify_upload_at == "recorder"

        # What PUT /admin/settings does after its commit.
        invalidate()
        changed = effective_settings(
            _StubSession([Setting(key="broadcastify_upload_at", value="keyup")]), base=get_settings()
        )
        assert changed is not first
        assert changed.broadcastify_upload_at == "keyup"

        # A merge stored from a snapshot that has since been replaced is never handed out:
        # the memo is keyed on the override snapshot, not only on the base.
        with runtime_settings._lock:  # noqa: SLF001 - simulating the interleaving
            runtime_settings._merged = first  # noqa: SLF001
            runtime_settings._merged_values = {"broadcastify_upload_at": "recorder"}  # noqa: SLF001
        current = effective_settings(
            _StubSession([Setting(key="broadcastify_upload_at", value="keyup")]), base=get_settings()
        )
        assert current.broadcastify_upload_at == "keyup"
    finally:
        invalidate()
