"""The audio-profile whitelist, validator and renderer (docs/AUDIO.md).

Nothing here touches a database or ffmpeg.  :mod:`app.audioproc.profiles` is the boundary
between operator-typed data and an ffmpeg command line, so the tests it needs are the ones
that pin *what cannot get through it*: a kind that is not on the list, a number outside its
range, a chain longer than the cap, a value that is not a number at all.

The three shipped profiles are pinned by their exact rendered string.  That is deliberately
brittle: "Default" has to render the same stages the global chain renders, or assigning it
to a talkgroup silently rewrites that channel's whole archive, and "Aircraft cabin" is a
documented fix whose numbers appear in docs/AUDIO.md.  If one of these strings changes, the
change is the point and the test is where it gets noticed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.audioproc.chain import AudioConfig, Measurement, filter_chain
from app.audioproc.profiles import (
    BUILTIN_PROFILES,
    MAX_STEPS,
    STEP_KINDS,
    ProfileError,
    normalize_chain,
    render_chain,
    render_steps,
    step_catalog,
)

#: The tail every rendered chain ends with, and which a profile cannot remove.
TAIL = "loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50"

BUILTIN_RENDERS = {
    "Default": (
        "highpass=f=300,lowpass=f=3800,afftdn=nf=-35:nr=18,"
        f"agate=threshold=0.003162:ratio=4:attack=5:release=250,{TAIL}"
    ),
    "Aircraft cabin": f"highpass=f=250,equalizer=f=1950:w=500:g=-40:t=h,afftdn=nf=-28:nr=12,{TAIL}",
    "Speech band-pass": f"highpass=f=300,lowpass=f=3000,afftdn=nf=-25,{TAIL}",
}


def builtin(name: str) -> list[dict]:
    return next(p["chain"] for p in BUILTIN_PROFILES if p["name"] == name)


# --------------------------------------------------------------------------------------
# the builtins
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BUILTIN_RENDERS))
def test_builtin_renders_exactly(name: str) -> None:
    assert render_chain(builtin(name)) == BUILTIN_RENDERS[name]


def test_default_profile_renders_the_global_chains_own_stages() -> None:
    """The point of "Default": assigning it must not change a single byte of audio.

    The global chain's denoiser and gate are built from ``AudioConfig``'s own defaults; the
    profile is those two stages written as data.  Comparing the two renderings is what
    keeps the claim honest as either side moves.
    """
    # A call that arrived at -20 dBFS: the global gate follows the clip's own level, and at
    # -20 the answer it lands on is the -50 dBFS the profile writes down literally.  Any
    # other level compares the relative gate against a fixed one and measures that instead.
    measurement = Measurement(duration_s=2.0, mean_db=-20.0, peak_db=-10.0)
    config = AudioConfig()
    global_chain = filter_chain(measurement, (), config)
    with_profile = filter_chain(measurement, (), config, profile=render_steps(builtin("Default")))
    assert with_profile == global_chain


def test_every_builtin_normalizes_to_itself() -> None:
    """A stored builtin is already in normal form, so a round trip through the DB is a no-op."""
    for profile in BUILTIN_PROFILES:
        assert normalize_chain(profile["chain"]) == normalize_chain(normalize_chain(profile["chain"]))


# --------------------------------------------------------------------------------------
# what the renderer refuses
# --------------------------------------------------------------------------------------


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ProfileError, match="unknown filter"):
        render_chain([{"kind": "aresample", "params": {}}])


def test_a_shell_flavored_kind_is_refused_like_any_other() -> None:
    """The whitelist, not an escaping pass, is what keeps punctuation out of the graph."""
    with pytest.raises(ProfileError, match="unknown filter"):
        render_chain([{"kind": "highpass;amovie=/etc/passwd", "params": {}}])


def test_unknown_parameter_is_refused() -> None:
    with pytest.raises(ProfileError, match="takes no parameter"):
        render_chain([{"kind": "highpass", "params": {"f": 300, "shell": 1}}])


@pytest.mark.parametrize(
    ("step", "params"),
    [
        ("highpass", {"f": 0.0}),
        ("highpass", {"f": 96_000.0}),
        ("equalizer", {"f": 1950, "width": 500, "gain_db": -400}),
        ("equalizer", {"f": 1950, "width": 0.5, "gain_db": -40}),
        ("notch", {"f": 1000, "q": 0.0}),
        ("notch", {"f": 1000, "q": 10, "gain_db": 6}),
        ("afftdn", {"nf": -200.0}),
        ("afftdn", {"nf": -25, "nr": 500.0}),
        ("agate", {"threshold": 12.0}),
        ("anlmdn", {"s": 1e9}),
    ],
)
def test_out_of_range_numbers_are_refused(step: str, params: dict) -> None:
    with pytest.raises(ProfileError, match="must be between"):
        render_chain([{"kind": step, "params": params}])


@pytest.mark.parametrize("value", ["300", None, [300], {"f": 300}, True, float("nan"), float("inf")])
def test_a_parameter_that_is_not_a_finite_number_is_refused(value: object) -> None:
    """``None`` is the exception: it means "not given", and the default fills in."""
    step = [{"kind": "highpass", "params": {"f": value}}]
    if value is None:
        assert render_chain(step) == f"highpass=f=300,{TAIL}"
        return
    with pytest.raises(ProfileError):
        render_chain(step)


def test_too_many_steps_is_refused() -> None:
    long = [{"kind": "highpass", "params": {"f": 300}}] * (MAX_STEPS + 1)
    with pytest.raises(ProfileError, match=f"at most {MAX_STEPS} steps"):
        render_chain(long)
    # exactly the cap is fine
    assert render_chain(long[:MAX_STEPS]).count("highpass") == MAX_STEPS


@pytest.mark.parametrize("bad", ["highpass", {"kind": "highpass"}, 3])
def test_a_chain_that_is_not_a_list_of_steps_is_refused(bad: object) -> None:
    with pytest.raises(ProfileError):
        normalize_chain(bad)


def test_a_step_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ProfileError, match="must be an object"):
        render_chain(["highpass=f=300"])


def test_params_that_are_not_an_object_are_refused() -> None:
    with pytest.raises(ProfileError, match="'params' must be an object"):
        render_chain([{"kind": "highpass", "params": [300]}])


# --------------------------------------------------------------------------------------
# what it renders
# --------------------------------------------------------------------------------------


def test_an_empty_chain_is_the_tail_alone() -> None:
    """"No denoiser, no gate" is a legitimate profile, not a missing one."""
    assert render_chain([]) == TAIL
    assert render_steps([]) == []


def test_defaults_are_filled_and_optional_parameters_stay_off() -> None:
    """``afftdn.nr`` has no default: absent means "let ffmpeg choose", not "pick for it"."""
    assert render_steps([{"kind": "afftdn"}]) == ["afftdn=nf=-25"]
    assert render_steps([{"kind": "anlmdn"}]) == ["anlmdn"]
    assert render_steps([{"kind": "anlmdn", "params": {"s": 0.5}}]) == ["anlmdn=s=0.5"]


def test_normalize_fills_the_defaults_it_stores() -> None:
    assert normalize_chain([{"kind": "bandreject", "params": {"f": 1950}}]) == [
        {"kind": "bandreject", "params": {"f": 1950.0, "width": 500.0}}
    ]


def test_bandreject_and_notch_render_the_forms_ffmpeg_documents() -> None:
    assert render_steps([{"kind": "bandreject", "params": {"f": 1950, "width": 500}}]) == [
        "bandreject=f=1950:w=500:t=h"
    ]
    assert render_steps([{"kind": "notch", "params": {"f": 1200, "q": 20, "gain_db": -35}}]) == [
        "equalizer=f=1200:w=20:g=-35:t=q"
    ]


def test_the_gate_threshold_is_stored_in_db_and_rendered_linear() -> None:
    """An operator types the dBFS off the meter; ffmpeg wants the amplitude."""
    assert render_steps([{"kind": "agate", "params": {"threshold": -60.0}}]) == [
        "agate=threshold=0.001:ratio=4:attack=5:release=250"
    ]


def test_the_loudness_tail_follows_the_settings_it_is_given() -> None:
    assert render_chain([], target_lufs=-23.0, true_peak_db=-2.0).startswith(
        "loudnorm=I=-23:TP=-2:LRA=11"
    )


def test_a_profile_replaces_only_the_denoiser_and_the_gate() -> None:
    """The trim and the loudness tail are the call's, not the profile's."""
    measurement = Measurement(
        duration_s=6.0, mean_db=-30.0, peak_db=-10.0, head_trim_s=0.4, tail_trim_s=0.2
    )
    chain = filter_chain(
        measurement, (), AudioConfig(), profile=render_steps(builtin("Aircraft cabin"))
    )
    assert chain.startswith("atrim=start=0.4:end=5.8,asetpts=N/SR/TB,")
    assert "highpass=f=250,equalizer=f=1950:w=500:g=-40:t=h,afftdn=nf=-28:nr=12" in chain
    assert chain.endswith("alimiter=limit=0.630957:level=disabled:attack=5:release=50")
    # the global denoiser and gate are gone, not merely reordered
    assert "afftdn=nf=-25" not in chain
    assert "agate=" not in chain


def test_an_empty_profile_removes_the_gate_the_global_chain_would_add() -> None:
    chain = filter_chain(Measurement(2.0, -30.0, -10.0), (), AudioConfig(), profile=[])
    assert "agate=" not in chain
    assert "afftdn" not in chain
    assert chain.endswith("alimiter=limit=0.630957:level=disabled:attack=5:release=50")


def test_no_profile_leaves_the_global_chain_exactly_as_it_was() -> None:
    chain = filter_chain(Measurement(2.0, -20.0, -10.0), (), AudioConfig())
    assert "highpass=f=300,lowpass=f=3800,afftdn=nf=-35:nr=18" in chain
    assert "agate=threshold=0.003162:ratio=4:attack=5:release=250" in chain


# --------------------------------------------------------------------------------------
# the catalog the editor is generated from
# --------------------------------------------------------------------------------------


def test_the_catalog_describes_every_kind_the_renderer_accepts() -> None:
    catalog = step_catalog()
    kinds = {entry["kind"] for entry in catalog}
    assert kinds == {
        "highpass",
        "lowpass",
        "bandreject",
        "equalizer",
        "notch",
        "afftdn",
        "anlmdn",
        "agate",
    }
    for entry in catalog:
        assert entry["label"] and entry["help"]
        for param in entry["params"]:
            assert param["low"] < param["high"]
            assert param["default"] is None or param["low"] <= param["default"] <= param["high"]
            assert param["help"]


def test_every_catalog_default_renders() -> None:
    """A kind added to the whitelist has to be usable with nothing typed into it."""
    for entry in step_catalog():
        assert render_steps([{"kind": entry["kind"]}])


# --------------------------------------------------------------------------------------
# every value the whitelist admits is one ffmpeg accepts
# --------------------------------------------------------------------------------------

#: ``(kind, param, value)`` for both ends of every parameter's range, one at a time.
BOUNDS = [
    (spec.kind, param.name, value)
    for spec in STEP_KINDS
    for param in spec.params
    for value in (param.low, param.high)
]


def _rendered(kind: str, name: str, value: float) -> str:
    return ",".join(render_steps([{"kind": kind, "params": {name: value}}]))


def test_agate_times_at_the_bottom_of_their_range_do_not_render_as_zero() -> None:
    """``attack=0`` is what ``places=0`` made of 0.01-0.5 ms, and ffmpeg refuses it."""
    [gate] = render_steps([{"kind": "agate", "params": {"attack": 0.01, "release": 0.5}}])
    assert ":attack=0.01:" in gate
    assert gate.endswith(":release=0.5")
    # …and the shipped whole-millisecond values still render as the global chain does.
    [shipped] = render_steps([{"kind": "agate", "params": {"attack": 5, "release": 250}}])
    assert shipped.endswith(":attack=5:release=250")


@pytest.mark.parametrize(("kind", "name", "value"), BOUNDS)
def test_every_bound_renders_a_nonzero_value_inside_its_range(kind: str, name: str, value: float) -> None:
    spec = next(s for s in STEP_KINDS if s.kind == kind)
    param = next(p for p in spec.params if p.name == name)
    if kind == "agate" and name == "threshold":
        return  # stored in dBFS, rendered linear; covered by the ffmpeg run below
    option = spec.options.get(name, name)
    [step] = render_steps([{"kind": kind, "params": {name: value}}])
    rendered = next(
        float(part.split("=", 1)[1])
        for part in step.split("=", 1)[1].split(":")
        if part.split("=", 1)[0] == option
    )
    assert rendered == pytest.approx(value, rel=1e-6, abs=10.0 ** -param.places)
    assert param.low <= rendered <= param.high


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
@pytest.mark.parametrize(("kind", "name", "value"), BOUNDS)
def test_ffmpeg_accepts_every_bound(kind: str, name: str, value: float) -> None:
    """The whitelist's ranges are only a promise if ffmpeg agrees with every end of them."""
    graph = _rendered(kind, name, value)
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
         "-t", "0.05", "-af", graph, "-f", "null", "-"],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert result.returncode == 0, f"{graph}: {result.stderr.strip()[-300:]}"
