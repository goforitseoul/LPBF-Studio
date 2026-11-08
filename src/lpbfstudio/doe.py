"""Design of experiments: generate parameter-set variants from a base set.

Rules (see docs/doe_audit.md):

- A range ``start:stop:step`` is inclusive and must end exactly at ``stop``; integer parameters
  need whole-number start, stop and step.
- Every factor needs at least one level, and its levels must be finite and distinct.
- A factor is one parameter (not a group), named once, and not one of the fields the generator
  sets itself (``name``, ``description``, ``locked``, ``schema_version``) or a contour pass ``id``
  (read-only identity).
- The number of variants is the product of the level counts. It is checked against
  ``max_variants`` before any variant is built.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, Sequence

from pydantic import BaseModel

from .library import Library, LibraryError
from .models import ContourPass, ExposureSet
from .paths import _step, coerce, find_by_id, get_path, item_key, set_path, with_assignments

DEFAULT_MAX_VARIANTS = 500
# fields that the generator sets itself, or that identify the set rather than the process
PROTECTED_FIELDS = ("name", "description", "locked", "schema_version")

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_DIGITS = 9  # range levels are rounded to this many decimals
_STEP_TOL = 1e-9  # relative tolerance on the number of steps in a range


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and not isinstance(value, Enum)


def _parse_range(text: str, like: Any, max_levels: int) -> list[Any]:
    parts = [p.strip() for p in text.split(":")]
    if len(parts) != 3:
        raise ValueError(f"a range must be start:stop:step, got {text!r}")
    try:
        start, stop, step = (float(p) for p in parts)
    except ValueError:
        raise ValueError(f"a range must be start:stop:step with three numbers, got {text!r}") from None
    if not all(math.isfinite(x) for x in (start, stop, step)):
        raise ValueError(f"range values must be finite numbers, got {text!r}")
    if step <= 0:
        raise ValueError(f"step must be positive, got {text!r}")
    if stop < start:
        raise ValueError(f"stop {stop:g} is below start {start:g} in {text!r}; write it as start:stop:step")
    integer = isinstance(like, int)
    if integer and not all(x.is_integer() for x in (start, stop, step)):
        raise ValueError(f"{text!r}: an integer parameter needs whole-number start, stop and step")
    k = (stop - start) / step
    steps = round(k)
    if abs(k - steps) > _STEP_TOL * max(1.0, abs(k)):
        lo = round(start + math.floor(k) * step, _DIGITS)
        hi = round(start + math.ceil(k) * step, _DIGITS)
        raise ValueError(f"step {step:g} does not divide {start:g} to {stop:g}: the last level would be "
                         f"{lo:g} or {hi:g}; use one of those as stop, or list the levels")
    n = steps + 1
    if n > max_levels:
        raise ValueError(f"range {text!r} has {n} levels, more than the limit of {max_levels}")
    vals = [round(start + i * step, _DIGITS) for i in range(n)]
    vals[-1] = stop
    return [int(v) for v in vals] if integer else vals


def check_levels(values: Sequence[Any], path: str | None = None) -> list[Any]:
    """Return ``values`` as a list after checking it is non-empty, finite and free of duplicates."""
    where = f" for {path}" if path else ""
    vals = list(values)
    if not vals:
        raise ValueError(f"no levels given{where}")
    if any(isinstance(v, float) and not math.isfinite(v) for v in vals):
        raise ValueError(f"levels{where} must be finite numbers")
    seen: list[Any] = []
    for v in vals:
        if v in seen:
            raise ValueError(f"duplicate level {_fmt(v)}{where}; each level must be given once")
        seen.append(v)
    return vals


# a comma between digits whose right-hand part starts with 0 and another digit ("0,05",
# "960,05"): in a level list that part can only be meant as a fraction
_DECIMAL_COMMA = re.compile(r"(\d+),(0\d+)")


def _check_decimal_comma(text: str) -> None:
    m = _DECIMAL_COMMA.search(text)
    if m:
        whole, frac = m.groups()
        raise ValueError(
            f"{m.group(0)!r} looks like a decimal comma. In a level list the comma separates levels, "
            f"so this would be read as {whole} and {int(frac)}. Write {whole}.{frac} with a decimal point; "
            f"if you meant two levels, write {whole}, {int(frac)}")


def parse_values(text: str, like: Any, max_levels: int = DEFAULT_MAX_VARIANTS) -> list[Any]:
    """Parse ``"0.09, 0.11, 0.13"`` or a range ``"0.09:0.13:0.02"`` (start:stop:step, inclusive).

    Lists are separated by commas, semicolons or whitespace; text parameters by commas and
    semicolons only, so a level may contain spaces. ``like`` is the current value and gives the type.
    For decimal parameters, a comma directly followed by a zero-led number (``0,05``) is refused as
    a probable decimal comma.
    """
    text = text.strip()
    if ":" in text and _is_number(like):
        vals = _parse_range(text, like, max_levels)
    else:
        if isinstance(like, float):
            _check_decimal_comma(text)
        sep = r"[,;]" if _is_text(like) else r"[,;\s]+"
        vals = [coerce(part.strip(), like) for part in re.split(sep, text) if part.strip()]
        if len(vals) > max_levels:
            raise ValueError(f"{len(vals)} levels is more than the limit of {max_levels}")
    return check_levels(vals)


def count_variants(factors: Mapping[str, Sequence[Any]]) -> int:
    """The number of variants a full-factorial design of these factors has."""
    return math.prod(len(v) for v in factors.values())


def count_text(factors: Mapping[str, Sequence[Any]]) -> str:
    """``"3 × 2 = 6 variants"``, or ``"3 variants"`` for a single factor."""
    n = count_variants(factors)
    total = f"{n} variant{'' if n == 1 else 's'}"
    return total if len(factors) == 1 else f"{' × '.join(str(len(v)) for v in factors.values())} = {total}"


def canonical_path(root: BaseModel, path: str) -> str:
    """The path with exposures named by kind, list indices made non-negative and format 2 contour
    passes named by id (``@id``, whichever form was given), so that two spellings of the same
    parameter compare equal."""
    obj: Any = root
    out: list[str] = []
    by_kind = False
    for key in path.split("."):
        if isinstance(obj, list):
            if key.startswith("@"):
                i = find_by_id(obj, key[1:])
            else:
                i = int(key)
                i = i + len(obj) if i < 0 else i
            obj = obj[i]
            out.append(obj.kind if by_kind else item_key(obj, i))
            by_kind = False
            continue
        by_kind = isinstance(obj, ExposureSet) and key == "exposure_types"
        obj = _step(obj, key)
        if not by_kind:
            out.append(key)
    return ".".join(out)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        # ``:g`` (6 significant digits) is kept wherever it is exact, so existing names do not
        # change; otherwise the shortest text that reads back as the same float
        short = f"{value:g}"
        return short if float(short) == value else repr(value)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _reason(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return "; ".join(e["msg"] for e in errors())
        except Exception:  # noqa: BLE001 - fall back to the plain message
            pass
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def render_name(template: str, values: dict[str, Any], index: int, base: str | None = None) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key == "i":
            return str(index)
        if key == "base" and base is not None:
            return base
        if key in values:
            return _fmt(values[key])
        # allow the short leaf name, e.g. {hatch_distance_mm}, when only one factor has it
        matches = [path for path in values if path.rsplit(".", 1)[-1] == key]
        if len(matches) > 1:
            raise ValueError(f"template placeholder {{{key}}} matches several factors ({', '.join(matches)}); "
                             "use the full path")
        if matches:
            return _fmt(values[matches[0]])
        raise KeyError(f"template placeholder {{{key}}} does not match a factor")

    return _PLACEHOLDER.sub(repl, template)


def _check_factors(base: ExposureSet, factors: Mapping[str, Sequence[Any]]) -> None:
    seen: dict[str, str] = {}
    for path, values in factors.items():
        current = get_path(base, path)  # raises KeyError for unknown paths
        if isinstance(current, (BaseModel, list)):
            raise ValueError(f"{path} is a group of parameters, not a parameter")
        canon = canonical_path(base, path)
        parent_path, _, leaf = path.rpartition(".")
        if leaf == "id" and isinstance(get_path(base, parent_path), ContourPass):
            raise ValueError(f"{path} cannot be a DOE factor; contour pass ids are read-only")
        if canon in PROTECTED_FIELDS:
            raise ValueError(f"{path} cannot be a DOE factor; the generator sets it for each variant")
        if canon in seen:
            raise ValueError(f"{seen[canon]} and {path} are the same parameter; give it once")
        seen[canon] = path
        check_levels(values, path)


def generate(base: ExposureSet, factors: dict[str, Sequence[Any]], name_template: str = "{base}_DOE{i}",
             max_variants: int = DEFAULT_MAX_VARIANTS) -> list[ExposureSet]:
    """Full-factorial variants. Each factor maps a parameter path to its levels.

    The first factor varies slowest; ``{i}`` counts from 1. ``base`` is not modified.
    """
    if not factors:
        raise ValueError("at least one factor is required")
    _check_factors(base, factors)
    total = count_variants(factors)
    if total > max_variants:
        raise ValueError(f"{count_text(factors)} is more than the limit of {max_variants}; use fewer levels")
    paths = list(factors)
    for path in paths:  # report a bad level once, by factor, before building any combination
        for value in factors[path]:
            try:
                set_path(base.model_copy(deep=True), path, value)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"factor {path}: level {_fmt(value)} is invalid: {_reason(exc)}") from exc
    combos = [dict(zip(paths, combo)) for combo in itertools.product(*(factors[p] for p in paths))]
    names = [render_name(name_template, values, i, base.name) for i, values in enumerate(combos, start=1)]
    if any(not n.strip() for n in names):
        raise ValueError("the name template gives an empty name")
    if len(set(names)) != len(names):
        raise ValueError("name template produces duplicate names; include every factor or {i}")
    out: list[ExposureSet] = []
    for i, (values, name) in enumerate(zip(combos, names), start=1):
        desc = ", ".join(f"{p}={_fmt(v)}" for p, v in values.items())
        try:
            es = with_assignments(base, values.items())
        except (KeyError, ValueError) as exc:
            raise ValueError(f"variant {i} ({desc}) is invalid: {_reason(exc)}") from exc
        es.locked = False
        es.name = name
        es.description = f"DOE variant of {base.name}: {desc}"
        out.append(es)
    return out


def collect_factors(pairs: Sequence[tuple[str, Sequence[Any]]]) -> dict[str, Sequence[Any]]:
    """Turn ``(path, levels)`` pairs into a factor dict, refusing a path given twice."""
    out: dict[str, Sequence[Any]] = {}
    for path, values in pairs:
        if path in out:
            raise ValueError(f"{path} is given twice; give each factor once")
        out[path] = values
    return out


NOT_TRANSACTIONAL = "DOE saving is not transactional: the variants saved before the failure stay in the library."


@dataclass
class BatchSave:
    """What a DOE batch save left in the library.

    When a save stops early, every variant that was attempted is checked on disk: ``saved`` holds
    those whose file holds exactly that variant, ``failed`` those verifiably not saved (with the
    reason), and ``uncertain`` those whose state could not be verified (with the reason).
    ``not_attempted`` were never started.
    """

    saved: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    uncertain: list[tuple[str, str]] = field(default_factory=list)
    not_attempted: list[str] = field(default_factory=list)
    interrupted: bool = False
    stopped_at: str | None = None
    error: str | None = None

    @property
    def complete(self) -> bool:
        return not (self.interrupted or self.failed or self.uncertain or self.not_attempted)

    def report(self) -> str:
        def names(xs: list[str]) -> str:
            return ", ".join(xs) or "none"

        def reasons(xs: list[tuple[str, str]]) -> str:
            return ", ".join(f"{n} ({why})" for n, why in xs) or "none"

        head = (f"saving interrupted at {self.stopped_at}" if self.interrupted
                else f"saving stopped at {self.stopped_at}: {self.error}")
        return "\n".join([
            head,
            f"saved ({len(self.saved)}): {names(self.saved)}",
            f"failed ({len(self.failed)}): {reasons(self.failed)}",
            f"uncertain ({len(self.uncertain)}): {reasons(self.uncertain)}",
            f"not attempted ({len(self.not_attempted)}): {names(self.not_attempted)}",
            NOT_TRANSACTIONAL,
        ])


def _on_disk(lib: Library, es: ExposureSet) -> tuple[str, str]:
    """``("saved", "")``, ``("absent", "")``, ``("different", why)`` or ``("unknown", why)`` for the
    file a new set called ``es.name`` is written to. Reads the file; never guesses."""
    try:
        f = lib._file(es.name)
        if not f.exists():
            return "absent", ""
        stored = lib._read(f)
    except (OSError, ValueError, LibraryError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc).splitlines()[0]
        return "unknown", f"cannot be verified: {reason}"
    if stored == es:
        return "saved", ""
    return "different", f"its file holds a different set ({stored.name!r})"


_BATCH_BASE: contextvars.ContextVar[str | None] = contextvars.ContextVar("lpbf_doe_batch_base", default=None)


@contextlib.contextmanager
def batch_source(base_name: str) -> Iterator[None]:
    """A ``save_batch`` inside this block saves variants generated from the set ``base_name``; their history records name
    its current revision as their source (DC-001 §7.9, E-AC1)."""
    token = _BATCH_BASE.set(base_name)
    try:
        yield
    finally:
        _BATCH_BASE.reset(token)


def save_batch(lib: Library, variants: Sequence[ExposureSet],
               on_saved: Callable[[str], None] | None = None) -> BatchSave:
    """Save ``variants`` one by one as new sets, in order, stopping at the first library or
    file-system error or at a KeyboardInterrupt (Ctrl+C), which is not passed on.

    Nothing already saved is removed, so an early stop leaves a partial batch. The outcome is
    then taken from the files on disk, not from what the loop believes it saved: a set moved
    into place just before an interrupt counts as saved, and a state that cannot be read back is
    reported as uncertain. A second interrupt while checking leaves the rest uncertain.
    """
    out = BatchSave()
    reached = 0  # index of the variant being saved when the loop stopped
    try:
        with lib._doe_batch(_BATCH_BASE.get()):
            for reached, v in enumerate(variants):
                lib.save(v, overwrite=False)
                if on_saved is not None:
                    on_saved(v.name)
        out.saved = [v.name for v in variants]
        return out
    except KeyboardInterrupt:
        out.interrupted = True
    except (LibraryError, OSError) as exc:
        out.error = (exc.strerror or str(exc)) if isinstance(exc, OSError) else str(exc)
    current = variants[reached]
    out.stopped_at = current.name
    out.not_attempted = [v.name for v in variants[reached + 1:]]
    attempted = list(variants[:reached + 1])
    for k, v in enumerate(attempted):
        try:
            state, why = _on_disk(lib, v)
        except KeyboardInterrupt:  # pressed again while checking
            out.interrupted = True
            out.uncertain += [(x.name, "not verified: interrupted while checking") for x in attempted[k:]]
            break
        if state == "saved":
            out.saved.append(v.name)
        elif v is not current:
            # reported saved by the library earlier, but its file no longer holds it
            out.uncertain.append((v.name, why or "was saved, but its file is no longer there"))
        elif state == "absent":
            out.failed.append((v.name, "interrupted before it was saved" if out.interrupted and out.error is None
                               else str(out.error)))
        elif state == "different":
            out.failed.append((v.name, f"{out.error}; {why}" if out.error else why))
        else:
            out.uncertain.append((v.name, why))
    return out


def format_level(value: Any) -> str:
    """A level as names, descriptions and the dry-run listing show it."""
    return _fmt(value)


def level_lines(factors: Mapping[str, Sequence[Any]]) -> list[str]:
    """``"path: v1, v2"`` per factor, from the parsed levels that ``generate`` combines."""
    return [f"{path}: {', '.join(format_level(v) for v in values)}" for path, values in factors.items()]
