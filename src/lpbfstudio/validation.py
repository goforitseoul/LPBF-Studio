"""Rule-based checks on exposure sets beyond the per-field ranges in the model.

Every finding names the rule that produced it with a stable code (see ``RULES`` and
``docs/validation_rules.md``) and says what it is about:

- ``Scope.PARAMETER``: ``path`` is the canonical parameter path of one leaf value in the set,
  for example ``hatch.infill.power_w`` or ``contour.passes.1.downskin_power_w``.
- ``Scope.NODE``: ``path`` names an exposure type or region (``hatch.infill``, ``core``), for
  findings derived from several of its parameters; ``context["parameters"]`` lists them.
- ``Scope.SET``: the finding is about the set as a whole; ``path`` is empty.
- ``Scope.PROCESS``: ``path`` is a parameter of the process settings, not of the set.
- ``Scope.MACHINE``: ``path`` is a field of a machine profile; ``context["machine"]`` names it.

A set is checked against the machine profile named in its ``machine`` field (``validate_set``).
A blank, unknown or inconsistent reference gets its own finding and the machine-dependent checks
are not run; there is no fallback to another profile.

Paths name exposure types by kind, so they do not change when exposure types are reordered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from . import calc
from .models import (
    Contour, Core, Edge, ExposureSet, Hatch, HatchPattern, Homogenization, MachineProfile,
    ProcessSettings, SetCategory, Skin, SupportHatch,
)
from .paths import item_key


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Scope(str, Enum):
    PARAMETER = "parameter"
    NODE = "node"
    SET = "set"
    PROCESS = "process"
    MACHINE = "machine"


@dataclass(frozen=True)
class Rule:
    code: str
    severity: Severity
    scope: Scope
    title: str


def _rules(*rules: Rule) -> dict[str, Rule]:
    out: dict[str, Rule] = {}
    for r in rules:
        if r.code in out:
            raise ValueError(f"duplicate rule code {r.code}")
        out[r.code] = r
    return out


E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO
PAR, NODE, SET, PROC, MAC = Scope.PARAMETER, Scope.NODE, Scope.SET, Scope.PROCESS, Scope.MACHINE

# Codes are identifiers. Never renumber or reuse one; retire it and add a new code instead.
RULES: dict[str, Rule] = _rules(
    Rule("SET-001", E, SET, "Set has no exposure types"),
    Rule("SET-002", W, SET, "Part set has neither Hatch nor Core"),
    Rule("SET-003", I, SET, "Part set has no Contour"),
    Rule("MCH-001", E, PAR, "Laser power above the machine maximum"),
    Rule("MCH-002", E, PAR, "Scan speed above the machine maximum"),
    Rule("MCH-003", E, PAR, "Layer thickness outside the machine range"),
    Rule("MCH-004", E, PROC, "Platform temperature above the machine maximum"),
    Rule("ENG-001", W, NODE, "Volumetric energy below the machine energy window"),
    Rule("ENG-002", W, NODE, "Volumetric energy above the machine energy window"),
    Rule("ENG-003", W, NODE, "Infill skipping: idealized layer-cycle average energy density"),
    Rule("SKN-001", E, PAR, "Skin thickness smaller than one layer"),
    Rule("SKN-002", W, PAR, "Skin thickness not a multiple of the layer thickness"),
    Rule("SKN-003", W, PAR, "Positive skin ridge"),
    Rule("SKY-001", W, PAR, "Skywriting switched off"),
    Rule("HAT-001", E, PAR, "Hatch distance not smaller than the stripe width"),
    Rule("HAT-002", W, PAR, "Stripe overlap at least half the stripe width"),
    Rule("HAT-003", E, PAR, "Restriction angle blocks every hatch direction"),
    Rule("HAT-004", W, PAR, "Hatch rotation is a multiple of 180°"),
    Rule("HAT-005", W, PAR, "Infill skipping without both up- and downskin"),
    Rule("HOM-001", I, PAR, "Power reduction selected with factor 0"),
    Rule("HOM-002", W, PAR, "Power reduction factor above 0.6"),
    Rule("HOM-003", I, PAR, "Power reduction factor set but not used"),
    Rule("COR-001", W, NODE, "Core without Hatch"),
    Rule("CON-001", W, NODE, "Contour pass names not unique"),
    Rule("EDG-001", W, NODE, "Both edge methods switched off"),
    Rule("EDG-002", I, PAR, "Edge factor below the minimum edge factor"),
    Rule("MPR-001", E, MAC, "Machine profile has no name"),
    Rule("MPR-002", E, MAC, "Machine profile minimum layer thickness above its maximum"),
    Rule("MPR-003", E, MAC, "Machine profile energy window invalid"),
    Rule("MRF-001", W, PAR, "Set names no machine profile; machine checks not run"),
    Rule("MRF-002", E, PAR, "Named machine profile not found; machine checks not run"),
    Rule("MRF-003", E, PAR, "Named machine profile is inconsistent; machine checks not run"),
    Rule("MRF-004", W, SET, "No machine profile supplied; machine checks not run"),
)

# Rules whose result depends on the machine profile; skipped when no usable profile is known.
MACHINE_DEPENDENT = ("MCH-001", "MCH-002", "MCH-003", "MCH-004", "ENG-001", "ENG-002")


@dataclass
class Issue:
    severity: Severity
    path: str
    message: str
    code: str = ""
    scope: Scope = Scope.PARAMETER
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def location(self) -> str:
        """Human-readable target: the path, ``(set)``, or ``process.<path>``."""
        if self.scope == Scope.SET:
            return "(set)"
        if self.scope == Scope.PROCESS:
            return f"process.{self.path}"
        if self.scope == Scope.MACHINE:
            return f"machine.{self.path}"
        return self.path

    def __str__(self) -> str:
        return f"[{self.severity.value.upper()}] {self.code} {self.location}: {self.message}"


def _issue(code: str, path: str, msg: str, **context: Any) -> Issue:
    rule = RULES[code]
    return Issue(rule.severity, path, msg, code, rule.scope, context)


def check_machine(mp: MachineProfile) -> list[Issue]:
    """Consistency of a machine profile itself (MPR rules)."""
    issues: list[Issue] = []
    ctx = {"machine": mp.name}
    if not mp.name.strip():
        issues.append(_issue("MPR-001", "name", "the machine profile has no name, so no set can refer to it", **ctx))
    if mp.min_layer_mm > mp.max_layer_mm:
        issues.append(_issue("MPR-002", "min_layer_mm",
                             f"minimum layer thickness {mp.min_layer_mm} mm is above the maximum {mp.max_layer_mm} mm",
                             value=mp.min_layer_mm, limit=mp.max_layer_mm, unit="mm", parameters=["max_layer_mm"],
                             **ctx))
    lo, hi = mp.energy_window_j_mm3
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo < 0 or lo >= hi:
        issues.append(_issue("MPR-003", "energy_window_j_mm3",
                             f"energy window ({lo}, {hi}) J/mm³ must be two finite values with 0 ≤ minimum < maximum",
                             value=(lo, hi), unit="J/mm³", **ctx))
    return issues


def validate_set(es: ExposureSet, profiles: Mapping[str, MachineProfile],
                 process: ProcessSettings | None = None, unreadable: Sequence[str] = ()) -> list[Issue]:
    """Validate a set against the profile it names in ``es.machine``.

    ``profiles`` maps profile names to profiles (exact, case-sensitive match); ``unreadable``
    lists profile files that could not be loaded, for the error context.
    """
    ref = es.machine.strip()
    if not ref:
        head = [_issue("MRF-001", "machine", "the set names no machine profile; machine limits and the "
                       "energy window were not checked", available=sorted(profiles))]
        return head + _check(es, None, process)
    mp = profiles.get(ref)
    if mp is None:
        ctx: dict[str, Any] = {"available": sorted(profiles), "unreadable": list(unreadable)}
        close = [n for n in profiles if n.casefold() == ref.casefold()]
        if close:
            ctx["did_you_mean"] = close[0]
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        head = [_issue("MRF-002", "machine", f"machine profile {ref!r} not found{hint}; machine limits and the "
                       "energy window were not checked", **ctx)]
        return head + _check(es, None, process)
    problems = check_machine(mp)
    if any(i.severity == Severity.ERROR for i in problems):
        head = [_issue("MRF-003", "machine", f"machine profile {ref!r} is inconsistent; machine limits and the "
                       "energy window were not checked", machine=ref, codes=[i.code for i in problems])]
        return head + problems + _check(es, None, process)
    return problems + _check(es, mp, process)


def validate(es: ExposureSet, machine: MachineProfile | None = None,
             process: ProcessSettings | None = None) -> list[Issue]:
    """Validate a set against an explicitly given profile.

    ``machine=None`` does not assume a default: machine-dependent rules are skipped and MRF-004 says so.
    """
    if machine is None:
        head = [_issue("MRF-004", "", "no machine profile was supplied; machine limits and the energy "
                       "window were not checked")]
        return head + _check(es, None, process)
    return _check(es, machine, process)


def _check(es: ExposureSet, machine: MachineProfile | None, process: ProcessSettings | None) -> list[Issue]:
    issues: list[Issue] = []
    mctx = {"machine": machine.name} if machine is not None else {}

    def add(code: str, path: str, msg: str, **context: Any) -> None:
        issues.append(_issue(code, path, msg, **context))

    t = es.layer_thickness_mm
    if machine is not None and not machine.min_layer_mm <= t <= machine.max_layer_mm:
        add("MCH-003", "layer_thickness_mm",
            f"{t} mm is outside the machine range {machine.min_layer_mm}–{machine.max_layer_mm} mm",
            value=t, limit=(machine.min_layer_mm, machine.max_layer_mm), unit="mm", **mctx)

    if not es.exposure_types:
        add("SET-001", "", "the set has no exposure types")
    if es.category == SetCategory.PART:
        if "hatch" not in es.kinds() and "core" not in es.kinds():
            add("SET-002", "", "no Hatch or Core: the part interior will not be melted")
        if "contour" not in es.kinds():
            add("SET-003", "", "no Contour: expect rougher side walls and lower accuracy")

    def check_scan(node: str, p: float, v: float, power: str = "power_w", speed: str = "speed_mm_s",
                   **extra: Any) -> None:
        """Machine limits for one (power, speed) pair; ``node`` + field names give the parameter paths."""
        if machine is None:
            return
        if p > machine.max_power_w:
            add("MCH-001", f"{node}.{power}", f"{p} W exceeds the machine maximum of {machine.max_power_w} W",
                value=p, limit=machine.max_power_w, unit="W", **mctx, **extra)
        if v > machine.max_speed_mm_s:
            add("MCH-002", f"{node}.{speed}",
                f"{v} mm/s exceeds the machine maximum of {machine.max_speed_mm_s} mm/s",
                value=v, limit=machine.max_speed_mm_s, unit="mm/s", **mctx, **extra)

    def check_volumetric(node: str, p: float, v: float, h: float) -> None:
        if machine is None:
            return
        e = calc.volumetric_energy(p, v, h, t)
        lo, hi = machine.energy_window_j_mm3
        ctx = dict(energy_j_mm3=e, window_j_mm3=(lo, hi), unit="J/mm³",
                   parameters=[f"{node}.power_w", f"{node}.speed_mm_s", f"{node}.hatch_distance_mm",
                               "layer_thickness_mm"], **mctx)
        if e < lo:
            add("ENG-001", node, f"energy input {e:.1f} J/mm³ is below {lo:.0f}: risk of lack of fusion", **ctx)
        elif e > hi:
            add("ENG-002", node, f"energy input {e:.1f} J/mm³ is above {hi:.0f}: risk of overheating / keyholing",
                **ctx)

    def check_skin(node: str, s: Skin) -> None:
        if not s.enabled:
            return
        check_scan(node, s.power_w, s.speed_mm_s)
        check_volumetric(node, s.power_w, s.speed_mm_s, s.hatch_distance_mm)
        n, exact = calc.skin_layers(s.thickness_mm, t)
        if s.thickness_mm < t:
            add("SKN-001", f"{node}.thickness_mm", "skin thickness is smaller than one layer",
                value=s.thickness_mm, limit=t, unit="mm")
        elif not exact:
            add("SKN-002", f"{node}.thickness_mm",
                f"{s.thickness_mm} mm is not a multiple of the {t} mm layer (≈{s.thickness_mm / t:.2f} layers)",
                value=s.thickness_mm, layers=s.thickness_mm / t, unit="mm")
        if s.ridge_mm > 0:
            add("SKN-003", f"{node}.ridge_mm", "positive ridge: even vertical walls will be exposed as skin",
                value=s.ridge_mm, unit="mm")
        if not s.skywriting:
            add("SKY-001", f"{node}.skywriting", "skywriting is off: energy input varies at vector ends")

    for et in es.exposure_types:
        k = et.kind
        if isinstance(et, Hatch):
            f = et.infill
            fp = f"{k}.infill"
            if f.enabled:
                check_scan(fp, f.power_w, f.speed_mm_s)
                check_volumetric(fp, f.power_w, f.speed_mm_s, f.hatch_distance_mm)
                if f.pattern == HatchPattern.STRIPES:
                    if f.hatch_distance_mm >= f.stripes.stripe_width_mm:
                        add("HAT-001", f"{fp}.hatch_distance_mm", "hatch distance must be smaller than the stripe width",
                            value=f.hatch_distance_mm, limit=f.stripes.stripe_width_mm, unit="mm",
                            parameters=[f"{fp}.stripes.stripe_width_mm"])
                    if f.stripes.stripe_overlap_mm >= f.stripes.stripe_width_mm / 2:
                        add("HAT-002", f"{fp}.stripes.stripe_overlap_mm", "overlap is at least half the stripe width",
                            value=f.stripes.stripe_overlap_mm, limit=f.stripes.stripe_width_mm / 2, unit="mm",
                            parameters=[f"{fp}.stripes.stripe_width_mm"])
                if f.pattern == HatchPattern.CHESS:
                    check_scan(f"{fp}.chess", f.chess.gap_power_w, f.chess.gap_speed_mm_s,
                               power="gap_power_w", speed="gap_speed_mm_s")
                if not f.skywriting:
                    add("SKY-001", f"{fp}.skywriting",
                        "skywriting is off: acceleration at vector ends changes energy input and material properties")
                if f.restriction_angle_deg >= 90:
                    add("HAT-003", f"{fp}.restriction_angle_deg", "a 90° restriction blocks every hatch direction",
                        value=f.restriction_angle_deg, limit=90, unit="°")
                if f.rotation_angle_deg % 180 == 0:
                    add("HAT-004", f"{fp}.rotation_angle_deg",
                        "hatch direction never changes between layers: expect anisotropy",
                        value=f.rotation_angle_deg, unit="°")
                if f.homogenization == Homogenization.POWER_REDUCTION:
                    if f.power_reduction_factor == 0:
                        add("HOM-001", f"{fp}.power_reduction_factor",
                            "power reduction is selected but the factor is 0", value=0.0)
                    elif f.power_reduction_factor > 0.6:
                        p_min = calc.reduced_power(f.power_w, f.power_reduction_factor, 0, f.stripes.stripe_width_mm)
                        add("HOM-002", f"{fp}.power_reduction_factor",
                            f"short vectors drop to {p_min:.0f} W: risk of failed layers from insufficient fusion",
                            value=f.power_reduction_factor, limit=0.6, min_power_w=p_min)
                elif f.power_reduction_factor > 0:
                    add("HOM-003", f"{fp}.power_reduction_factor",
                        "factor is set but homogenization is not power reduction",
                        value=f.power_reduction_factor, parameters=[f"{fp}.homogenization"])
                if f.skip_layers > 0 and not (et.upskin.enabled and et.downskin.enabled):
                    add("HAT-005", f"{fp}.skip_layers",
                        "infill skipping without up- and downskin leaves surfaces under-exposed",
                        value=f.skip_layers, parameters=[f"{k}.upskin.enabled", f"{k}.downskin.enabled"])
                if f.skip_layers > 0:
                    ce = calc.infill_cycle_energy(f, t)
                    e_nom = calc.volumetric_energy(f.power_w, f.speed_mm_s, f.hatch_distance_mm, t)
                    n = f.skip_layers
                    if ce.available:
                        text = (f"infill skipping n={n}: E_eff = {ce.value:.1f} J/mm³ "
                                f"(nominal {e_nom:.1f} J/mm³), an {calc.CYCLE_ENERGY_CAVEAT}")
                    else:
                        text = f"infill skipping n={n}: E_eff unavailable ({ce.reason}); nominal {e_nom:.1f} J/mm³"
                    add("ENG-003", fp, text, e_eff_j_mm3=ce.value, e_nom_j_mm3=e_nom, skip_layers=n,
                        unavailable_reason=ce.reason, unit="J/mm³",
                        parameters=[f"{fp}.power_w", f"{fp}.speed_mm_s", f"{fp}.hatch_distance_mm",
                                    "layer_thickness_mm", f"{fp}.skip_layers"])
            check_skin(f"{k}.upskin", et.upskin)
            check_skin(f"{k}.downskin", et.downskin)
        elif isinstance(et, Core) and et.enabled:
            check_scan(k, et.power_w, et.speed_mm_s)
            check_volumetric(k, et.power_w, et.speed_mm_s, et.hatch_distance_mm)
            if "hatch" not in es.kinds():
                add("COR-001", k, "core without Hatch: the skin around the core will not be exposed")
        elif isinstance(et, Contour):
            # passes of format 2 sets are named by id (@id), format 1 passes by index as before
            keys = [item_key(cp, i) for i, cp in enumerate(et.passes)]
            names = [cp.name for cp in et.passes]
            if len(set(names)) != len(names):
                add("CON-001", k, "contour pass names are not unique",
                    parameters=[f"{k}.passes.{key}.name" for key in keys])
            for i, cp in enumerate(et.passes):
                if not cp.enabled:
                    continue
                extra = {"pass_id": cp.id, "pass_position": i + 1} if cp.id is not None else {}
                for prefix in ("", "upskin_", "downskin_"):
                    check_scan(f"{k}.passes.{keys[i]}", getattr(cp, f"{prefix}power_w"),
                               getattr(cp, f"{prefix}speed_mm_s"), power=f"{prefix}power_w",
                               speed=f"{prefix}speed_mm_s", **extra)
        elif isinstance(et, Edge) and et.enabled:
            check_scan(k, et.power_w, et.speed_mm_s)
            if not (et.method1_enabled or et.method2_enabled):
                add("EDG-001", k, "both edge methods are off: no edges will be exposed",
                    parameters=[f"{k}.method1_enabled", f"{k}.method2_enabled"])
            if et.method1_enabled and et.edge_factor < calc.MIN_EDGE_FACTOR:
                add("EDG-002", f"{k}.edge_factor", f"below the minimum edge factor {calc.MIN_EDGE_FACTOR}",
                    value=et.edge_factor, limit=calc.MIN_EDGE_FACTOR)
        elif isinstance(et, SupportHatch) and et.enabled:
            check_scan(k, et.power_w, et.speed_mm_s)

    if process is not None and machine is not None:
        if process.platform_temperature_c > machine.max_platform_temperature_c:
            add("MCH-004", "platform_temperature_c",
                f"exceeds machine maximum {machine.max_platform_temperature_c} °C",
                value=process.platform_temperature_c, limit=machine.max_platform_temperature_c, unit="°C", **mctx)

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == Severity.ERROR for i in issues)
