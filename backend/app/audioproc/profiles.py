"""Per-channel filter chains: the whitelist, the validator and the renderer (§5.5).

:mod:`app.audioproc.chain` builds the chain every call gets from the *global* settings.
That is the right shape for the things every recording needs — trim the dead air, turn the
page tone down, land on a consistent loudness — and the wrong shape for the things one
channel needs.  One talkgroup (medevac transports, on the author's system) carries rotor and
turbine noise in a narrow band around 1.9 kHz that no global denoiser setting removes
without eating the speech on the other four hundred channels; a 40 dB cut from 1.7 to
2.2 kHz removes it and leaves the voice.

An **audio profile** is that per-channel middle section, stored as data rather than as
settings: an ordered list of ``{"kind": …, "params": {…}}`` steps that replaces the
denoiser-and-gate stages of the global chain for the talkgroups assigned to it.  The trim,
the tone envelope and the loudness/limiter tail are *not* part of a profile — they are
measured per call and are the guarantees the archive makes about every file it stores, so
they stay where they are and a profile cannot switch them off.

Nothing here opens a file or starts a process.  A profile is operator-typed data on its way
to an ffmpeg command line, so the whole module is written as a validator that happens to
emit a string: every kind is in :data:`STEP_KINDS`, every parameter is a number inside a
range that table declares, and anything else raises :class:`ProfileError` before a filter
graph exists.  A chain :func:`render_chain` accepted cannot contain an operator's
punctuation, because it does not contain any of an operator's characters at all — only
numbers this module formatted itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUILTIN_PROFILES",
    "MAX_STEPS",
    "STEP_KINDS",
    "ProfileError",
    "StepKind",
    "StepParam",
    "normalize_chain",
    "render_chain",
    "render_steps",
    "step_catalog",
]


class ProfileError(ValueError):
    """A chain that must not reach ffmpeg: unknown kind, bad parameter, too many steps."""


#: A chain longer than this is not a filter chain, it is a mistake.  Twelve is about three
#: times the longest chain that has ever been useful here (high-pass, notch, denoise, gate)
#: and short enough that the whole graph still fits on one line in a log.
MAX_STEPS = 12

#: Nothing below 20 Hz or above 20 kHz is meaningful.  The bound is the audible range
#: rather than this archive's Nyquist, because a profile may be written for a 16 kHz
#: recorder feed and ffmpeg is content to be handed a corner it will not reach.
_HZ_LOW = 20.0
_HZ_HIGH = 20_000.0


@dataclass(frozen=True)
class StepParam:
    """One parameter of one step kind: its range, its default and what it is for."""

    name: str
    low: float
    high: float
    default: float | None = None
    #: decimal places the rendered number is fixed to
    places: int = 3
    unit: str = ""
    help: str = ""

    @property
    def optional(self) -> bool:
        """No default, so leaving it blank leaves it off the filter entirely.

        Not the same as "not required": *nothing* in a step is required. A parameter with a
        default gets that default when it is not given; one without gets left out, and
        ffmpeg falls back to whatever the filter itself considers normal.
        """
        return self.default is None


@dataclass(frozen=True)
class StepKind:
    """One entry in the whitelist: what it is called, what it takes, what it renders."""

    kind: str
    label: str
    help: str
    params: tuple[StepParam, ...] = ()
    #: extra ``key=value`` pairs appended verbatim; they hold no operator input
    fixed: tuple[str, ...] = ()
    #: the ffmpeg filter this renders to, when it differs from ``kind``
    filter_name: str = ""
    #: ``{param name: ffmpeg option name}`` for the params whose names differ
    options: Mapping[str, str] = field(default_factory=dict)


def _num(value: float, places: int = 3) -> str:
    """A filter-graph number: fixed point, no exponent, no trailing noise.

    The same formatting :mod:`app.audioproc.chain` uses, kept identical on purpose — the
    "Default" builtin has to render byte-for-byte what the global chain renders, or
    assigning it to a talkgroup would rewrite every one of its files for no audible reason.

    The trailing-zero strip is conditional on there being a decimal point, which the
    ``chain`` version can leave out because it never formats to zero places: ``250`` at
    ``places=0`` would otherwise come back as ``25``.
    """
    text = f"{value:.{places}f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


#: Every step an operator may put in a chain.  A kind absent from here cannot be stored,
#: cannot be previewed and cannot be rendered — the whitelist *is* the security boundary,
#: which is why it is a table of ranges rather than a set of names.
STEP_KINDS: tuple[StepKind, ...] = (
    StepKind(
        kind="highpass",
        label="High-pass",
        help=(
            "Rolls off everything below the corner. 250–300 Hz removes engine rumble, "
            "handling thumps and rotor beat without touching speech, which starts at the "
            "male fundamental near 85 Hz but carries almost no intelligibility below 300."
        ),
        params=(
            StepParam("f", _HZ_LOW, _HZ_HIGH, default=300.0, places=1, unit="Hz", help="Corner frequency"),
        ),
    ),
    StepKind(
        kind="lowpass",
        label="Low-pass",
        help=(
            "Rolls off everything above the corner. This is a two-pole filter, so the "
            "corner is where it is already 3 dB down and about 2 dB down 400 Hz below "
            "that: a P25 vocoder carries consonant energy to roughly 3.7 kHz, so 3400 is "
            "inside the voice and the global chain uses 3800 (measured: 3400 costs 3.35 dB "
            "of the 2.5-3.8 kHz band, 3800 costs 0.01)."
        ),
        params=(
            StepParam("f", _HZ_LOW, _HZ_HIGH, default=3000.0, places=1, unit="Hz", help="Corner frequency"),
        ),
    ),
    StepKind(
        kind="bandreject",
        label="Band reject",
        help=(
            "Removes a band outright (`bandreject`, a two-pole notch). Use it when the "
            "noise occupies a band and nothing you want is inside it; when speech shares "
            "the band, prefer a deep `equalizer` cut, which attenuates rather than removes."
        ),
        params=(
            StepParam("f", _HZ_LOW, _HZ_HIGH, default=1950.0, places=1, unit="Hz", help="Center frequency"),
            StepParam("width", 10.0, 10_000.0, default=500.0, places=1, unit="Hz", help="Band width"),
        ),
        fixed=("t=h",),
        options={"width": "w"},
    ),
    StepKind(
        kind="equalizer",
        label="Band gain",
        help=(
            "A peaking filter over one band: negative gain cuts it, positive lifts it. This "
            "is the aircraft fix — `f=1950 width=500 gain_db=-40` is a 40 dB hole from 1.7 "
            "to 2.2 kHz, which takes the rotor and turbine band out and leaves the voice "
            "above and below it audible."
        ),
        params=(
            StepParam("f", _HZ_LOW, _HZ_HIGH, default=1950.0, places=1, unit="Hz", help="Center frequency"),
            StepParam("width", 10.0, 10_000.0, default=500.0, places=1, unit="Hz", help="Band width"),
            StepParam(
                "gain_db", -60.0, 20.0, default=-40.0, places=2, unit="dB", help="Gain inside the band"
            ),
        ),
        fixed=("t=h",),
        options={"width": "w", "gain_db": "g"},
    ),
    StepKind(
        kind="notch",
        label="Notch",
        help=(
            "A narrow `equalizer` cut placed by Q rather than by width — for a single "
            "steady whistle (a data carrier, a squelch tail, a rotor harmonic) whose "
            "frequency you can read off a spectrum but whose band is one line wide."
        ),
        params=(
            StepParam("f", _HZ_LOW, _HZ_HIGH, default=1000.0, places=1, unit="Hz", help="Center frequency"),
            StepParam("q", 0.1, 100.0, default=10.0, places=2, help="Q — higher is narrower"),
            StepParam("gain_db", -60.0, 0.0, default=-40.0, places=2, unit="dB", help="Depth of the cut"),
        ),
        fixed=("t=q",),
        filter_name="equalizer",
        options={"q": "w", "gain_db": "g"},
    ),
    StepKind(
        kind="afftdn",
        label="FFT denoise",
        help=(
            "Spectral noise reduction. `nf` is the assumed noise floor in dB — lower is "
            "more aggressive — and `nr` how many dB of it to take out. The global chain "
            "runs `nf=-35:nr=18` behind a 300-3800 Hz band-pass, which is the combination "
            "measured best on this system's audio; a channel with its own band edges takes "
            "the same denoiser well. `nf` is where the filter *thinks* the noise is, not "
            "how hard it pushes: measured floors here are a median -63 dBFS, and every `nf` "
            "from -30 to -45 removes the same 18 dB of noise while the too-loud end of that "
            "range takes 2-3 dB of the 2.5-3.8 kHz speech with it."
        ),
        params=(
            StepParam("nf", -80.0, -20.0, default=-25.0, places=1, unit="dB", help="Noise floor"),
            StepParam("nr", 0.01, 97.0, default=None, places=2, unit="dB", help="Reduction (optional)"),
        ),
    ),
    StepKind(
        kind="anlmdn",
        label="Non-local means denoise",
        help=(
            "**Not recommended on this system.** Measured against 20 calls of vocoder "
            "audio it *raised* the noise floor 8-9 dB (radio 5233201, Δsnr -6.05) and did "
            "nothing on the next radio tested (+0.35): non-local means wants a broadband "
            "hiss with structure to average over, and what a P25 codec emits is not that. "
            "For hiss, use a 300-3800 Hz band-pass with `afftdn nf=-35 nr=18` instead. "
            "Kept selectable for a channel with a different problem. `s` is the strength, "
            "`p` the patch duration and `r` the research duration, the last two in "
            "seconds. All three are optional."
        ),
        params=(
            StepParam("s", 0.00001, 10.0, default=None, places=5, help="Strength"),
            StepParam("p", 0.001, 0.1, default=None, places=4, unit="s", help="Patch duration"),
            StepParam("r", 0.002, 0.3, default=None, places=4, unit="s", help="Research duration"),
        ),
    ),
    StepKind(
        kind="agate",
        label="Noise gate",
        help=(
            "Ducks everything under the threshold, so a 30 dB lift on a quiet talker does "
            "not turn the hiss between his words into a roar. The threshold is the dBFS an "
            "operator reads off the meter; it is converted to the linear value ffmpeg wants."
        ),
        params=(
            StepParam(
                "threshold", -100.0, 0.0, default=-50.0, places=2, unit="dBFS", help="Gate opens above this"
            ),
            StepParam("ratio", 1.0, 9000.0, default=4.0, places=1, help="Ducking ratio below it"),
            # Two places, because the range starts at 0.01 ms: anything coarser renders the
            # low end as ``attack=0``, which ffmpeg refuses — and then every call on the
            # talkgroup fails processing.  ``_num`` strips the trailing zeros, so the
            # shipped 5 and 250 still render as ``5`` and ``250``.
            StepParam("attack", 0.01, 9000.0, default=5.0, places=2, unit="ms", help="Attack"),
            StepParam("release", 0.01, 9000.0, default=250.0, places=2, unit="ms", help="Release"),
        ),
    ),
)

_BY_KIND = {spec.kind: spec for spec in STEP_KINDS}

#: loudnorm's loudness range target — the same 11 LU :mod:`app.audioproc.chain` uses.
_LRA = 11.0

#: …and the same limiter options, for the same reason.  Spelled out here rather than
#: imported because this module is deliberately numpy-free (:mod:`app.audioproc.chain`
#: pulls in :mod:`app.audioproc.tones`, which is not) and the admin API leans on that.
#: ``level=disabled`` defeats ``alimiter``'s auto-level, which would otherwise push every
#: previewed clip up onto the ceiling; see the constant of the same name in ``chain``.
_LIMITER_OPTIONS = "level=disabled:attack=5:release=50"


def _coerce(spec: StepKind, param: StepParam, raw: Any) -> float:
    """One parameter, as a finite float inside its declared range, or :class:`ProfileError`."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ProfileError(f"{spec.kind}.{param.name} must be a number, not {type(raw).__name__}")
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):  # NaN, ±inf
        raise ProfileError(f"{spec.kind}.{param.name} must be a finite number")
    if not (param.low <= value <= param.high):
        unit = f" {param.unit}" if param.unit else ""
        raise ProfileError(
            f"{spec.kind}.{param.name} must be between {_num(param.low, 5)} and "
            f"{_num(param.high, 5)}{unit} (got {_num(value, 5)})"
        )
    return value


def _step_params(spec: StepKind, params: Mapping[str, Any]) -> dict[str, float]:
    """Validate one step's parameters and fill in the defaults it did not carry.

    A parameter with no default is genuinely optional (``afftdn.nr``, all of ``anlmdn``):
    absent means "leave it off the filter and let ffmpeg use its own default", which is not
    the same as choosing a number here and pretending it was the operator's.
    """
    unknown = sorted(set(params) - {p.name for p in spec.params})
    if unknown:
        known = ", ".join(p.name for p in spec.params) or "nothing"
        raise ProfileError(f"{spec.kind} takes no parameter {unknown[0]!r} (it takes {known})")
    values: dict[str, float] = {}
    for param in spec.params:
        given = params.get(param.name)
        if given is not None:
            values[param.name] = _coerce(spec, param, given)
        elif param.default is not None:
            values[param.name] = float(param.default)
    return values


def _one_step(raw: Any, index: int) -> tuple[StepKind, dict[str, float]]:
    if not isinstance(raw, Mapping):
        raise ProfileError(f"step {index + 1} must be an object with a 'kind'")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _BY_KIND:
        known = ", ".join(sorted(_BY_KIND))
        raise ProfileError(f"step {index + 1}: unknown filter {kind!r} (known kinds: {known})")
    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ProfileError(f"step {index + 1}: 'params' must be an object")
    spec = _BY_KIND[kind]
    return spec, _step_params(spec, params)


def _check_length(count: int) -> None:
    if count > MAX_STEPS:
        raise ProfileError(f"a chain may hold at most {MAX_STEPS} steps (got {count})")


def normalize_chain(steps: Any) -> list[dict[str, Any]]:
    """The chain as it should be stored: validated, defaults filled, nothing else in it.

    This is what a profile row's ``chain`` column holds, so a stored profile is by
    construction one that renders — a chain is validated when it is *written*, not when a
    call is being processed at three in the morning.
    """
    if steps is None:
        steps = []
    if isinstance(steps, (str, bytes, Mapping)) or not isinstance(steps, Sequence):
        raise ProfileError("a chain must be a list of steps")
    _check_length(len(steps))
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(steps):
        spec, values = _one_step(raw, index)
        out.append({"kind": spec.kind, "params": values})
    return out


def _render_one(spec: StepKind, values: Mapping[str, float]) -> str:
    parts: list[str] = []
    for param in spec.params:
        if param.name not in values:
            continue
        value = values[param.name]
        if spec.kind == "agate" and param.name == "threshold":
            # ffmpeg's agate wants a linear amplitude; the profile stores the dBFS the
            # operator typed, exactly as ``audio_gate_db`` does.
            parts.append(f"threshold={_num(_db_to_linear(value), 6)}")
            continue
        # agate's times are whole milliseconds in the shipped chain; ``_num`` rendering `5`
        # rather than `5.00` is what keeps the "Default" builtin byte-identical to it.
        parts.append(f"{spec.options.get(param.name, param.name)}={_num(value, param.places)}")
    parts.extend(spec.fixed)
    name = spec.filter_name or spec.kind
    return f"{name}={':'.join(parts)}" if parts else name


def render_steps(steps: Iterable[Any]) -> list[str]:
    """Each validated step as one ffmpeg filter, in order.  No trim, no loudness, no tail.

    This is what :func:`app.audioproc.chain.filter_chain` splices in where its denoiser and
    gate stages would otherwise go, which is why it stops short of the tail: the trim in
    front and the loudnorm/limiter behind are measured per call and belong to the call, not
    to the profile.
    """
    stages: list[str] = []
    for index, raw in enumerate(steps):
        spec, values = _one_step(raw, index)
        stages.append(_render_one(spec, values))
    _check_length(len(stages))
    return stages


def render_chain(
    steps: Iterable[Any],
    *,
    target_lufs: float = -16.0,
    true_peak_db: float = -4.0,
) -> str:
    """The whole standalone ``-af`` string for a chain, tail included.

    The fixed tail — ``loudnorm`` to the target and ``alimiter`` at the true-peak ceiling —
    is appended by the renderer and cannot be removed by a profile, because it is the one
    promise every file in the archive makes: a consistent level, and nothing that clips.
    This is the form the editor's preview renders and the form the tests pin.

    The tail is the *same* tail :func:`app.audioproc.chain.filter_chain` ends on, limiter
    options included, and it has to stay that way: the preview is an A/B against what the
    talkgroup will actually sound like once the profile is saved, and a preview whose
    limiter auto-levels when the real one does not is an A/B against a file that will never
    exist.
    """
    stages = render_steps(steps)
    stages.append(f"loudnorm=I={_num(target_lufs, 2)}:TP={_num(true_peak_db, 2)}:LRA={_num(_LRA, 1)}")
    stages.append(f"alimiter=limit={_num(_db_to_linear(true_peak_db), 6)}:{_LIMITER_OPTIONS}")
    return ",".join(stages)


def step_catalog() -> list[dict[str, Any]]:
    """The whitelist as JSON, so the editor builds its fields from this same table.

    The alternative — the ranges written once in Python and again in TypeScript — is the
    kind of duplication that is correct on the day it is written and wrong by the next
    release.  The form is generated from this, so a new step kind appears in the UI by
    being added to :data:`STEP_KINDS` and nowhere else.
    """
    return [
        {
            "kind": spec.kind,
            "label": spec.label,
            "help": spec.help,
            "params": [
                {
                    "name": p.name,
                    "low": p.low,
                    "high": p.high,
                    "default": p.default,
                    "unit": p.unit,
                    "help": p.help,
                    "optional": p.optional,
                }
                for p in spec.params
            ],
        }
        for spec in STEP_KINDS
    ]


#: The profiles a fresh database is seeded with, and the rows the migration inserts.
#:
#: "Default" is today's global chain written as data: an operator who wants to see what a
#: profile *is* can open it, and a talkgroup can be pinned to the shipped behavior even
#: after the global settings move on.  A talkgroup with **no** profile follows the global
#: ``audio_*`` settings — which is what "Default" is a snapshot of, so assigning it changes
#: nothing on a default-configured deployment and freezes the chain on any other.
BUILTIN_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "name": "Default",
        "description": (
            "The shipped chain, as data: the 300-3800 Hz speech band-pass, FFT denoise at "
            "a -35 dB noise floor taking 18 dB out, then the -50 dBFS gate. What a "
            "talkgroup with no profile gets from the global settings."
        ),
        "chain": [
            {"kind": "highpass", "params": {"f": 300.0}},
            {"kind": "lowpass", "params": {"f": 3800.0}},
            {"kind": "afftdn", "params": {"nf": -35.0, "nr": 18.0}},
            {
                "kind": "agate",
                "params": {"threshold": -50.0, "ratio": 4.0, "attack": 5.0, "release": 250.0},
            },
        ],
    },
    {
        "name": "Aircraft cabin",
        "description": (
            "Rotor and turbine noise: a 40 dB hole from 1.7 to 2.2 kHz over a 250 Hz "
            "high-pass, with a harder denoise behind it. Written for the medevac transports "
            "on the author's system, where the global chain leaves the beat audible and anything "
            "aggressive enough to remove it eats the speech."
        ),
        "chain": [
            {"kind": "highpass", "params": {"f": 250.0}},
            {"kind": "equalizer", "params": {"f": 1950.0, "width": 500.0, "gain_db": -40.0}},
            {"kind": "afftdn", "params": {"nf": -28.0, "nr": 12.0}},
        ],
    },
    {
        "name": "Speech band-pass",
        "description": (
            "The telephone band and nothing else: 300 Hz to 3 kHz, then a denoise. For a "
            "channel whose noise sits at both ends of the spectrum rather than in one band."
        ),
        "chain": [
            {"kind": "highpass", "params": {"f": 300.0}},
            {"kind": "lowpass", "params": {"f": 3000.0}},
            {"kind": "afftdn", "params": {"nf": -25.0}},
        ],
    },
)
