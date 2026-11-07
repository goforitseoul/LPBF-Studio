"""Dotted parameter paths such as ``hatch.infill.hatch_distance_mm`` or ``contour.passes.0.power_w``.

The first segment of a path may name an exposure type by its kind, which keeps
paths stable when the exposure order changes. A contour pass of a format 2 set may be named by
its id, ``contour.passes.@3f9c2e1a.power_w``, which stays with the pass when passes are reordered;
the index form names the pass currently at that position. Format 2 output uses the id form,
format 1 output the index form.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Iterator

from pydantic import BaseModel

from .models import ContourPass, ExposureSet, identity_problem

READ_ONLY = {(ExposureSet, "schema_version"): "the file format changes only through an explicit upgrade",
             (ContourPass, "id"): "a contour pass keeps its id for life"}


class _NoSuchId(KeyError):
    """An ``@id`` path segment that names no item; carries the explanation for the message."""


def item_key(item: Any, index: int) -> str:
    """The path segment for a list item: ``@id`` when it has one (format 2 passes), else its index."""
    pid = getattr(item, "id", None)
    return f"@{pid}" if isinstance(item, ContourPass) and pid is not None else str(index)


def find_by_id(items: list, pid: str) -> int:
    """The position of the item with id ``pid``; raises _NoSuchId listing what is there."""
    for i, item in enumerate(items):
        if isinstance(item, ContourPass) and item.id == pid:
            return i
    if not any(isinstance(item, ContourPass) and item.id is not None for item in items):
        raise _NoSuchId("this set is format 1; its contour passes have no ids, use the index form")
    listed = ", ".join(f"#{i + 1} @{p.id} {p.name!r}" for i, p in enumerate(items))
    raise _NoSuchId(f"no contour pass has id {pid!r}; passes: {listed}")


def _step(obj: Any, key: str) -> Any:
    if isinstance(obj, ExposureSet) and key in obj.kinds():
        return obj.get(key)
    if isinstance(obj, list):
        if key.startswith("@"):
            return obj[find_by_id(obj, key[1:])]
        return obj[int(key)]
    if isinstance(obj, BaseModel):
        if key not in type(obj).model_fields:
            raise KeyError(key)
        return getattr(obj, key)
    raise KeyError(key)


def get_path(root: BaseModel, path: str) -> Any:
    obj: Any = root
    for key in path.split("."):
        try:
            obj = _step(obj, key)
        except _NoSuchId as exc:
            raise KeyError(f"unknown parameter path: {path} ({exc.args[0]})") from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise KeyError(f"unknown parameter path: {path}") from exc
    return obj


def assign_field(obj: BaseModel, name: str, value: Any) -> None:
    """Assign one field so that a rejected value leaves ``obj`` unchanged.

    With ``validate_assignment``, pydantic stores the new value before a model-level validator
    runs, and does not undo it when that validator fails. The assignment is therefore tried on a
    deep copy first and only repeated on ``obj`` once it has passed.
    """
    trial = obj.model_copy(deep=True)
    setattr(trial, name, value)
    setattr(obj, name, value)


def set_path(root: BaseModel, path: str, value: Any) -> None:
    """Set a leaf value, coercing strings to the field type (validated by pydantic).

    On any error (unknown path, unparseable or invalid value) ``root`` is left unchanged.
    """
    parent_path, _, leaf = path.rpartition(".")
    parent = get_path(root, parent_path) if parent_path else root
    if not isinstance(parent, BaseModel) or leaf not in type(parent).model_fields:
        raise KeyError(f"unknown parameter path: {path}")
    if isinstance(parent, ExposureSet) and leaf == "name":
        # the name is the set's identity in the library; changing it here would save a duplicate
        raise ValueError("the set name cannot be changed with a parameter assignment; use rename")
    for (owner, field_name), why in READ_ONLY.items():
        if isinstance(parent, owner) and leaf == field_name:
            raise ValueError(f"{path} is read-only: {why}")
    current = getattr(parent, leaf)
    if isinstance(value, str):
        value = coerce(value, current)
    assign_field(parent, leaf, value)


def with_assignments(root: BaseModel, assignments: Iterable[tuple[str, Any]]) -> BaseModel:
    """Apply several ``(path, value)`` assignments all-or-nothing.

    Returns a new, fully re-validated model; ``root`` itself is never modified. Raises on the
    first failing assignment.
    """
    changed = identity_problem(root) if isinstance(root, ExposureSet) else None
    if changed:  # re-validating below would make a bypassed change look original
        raise ValueError(changed)
    candidate = root.model_copy(deep=True)
    for path, value in assignments:
        set_path(candidate, path, value)
    return type(root).model_validate(candidate.model_dump())


def coerce(text: str, like: Any) -> Any:
    if isinstance(like, bool):
        low = text.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"not a boolean: {text}")
    if isinstance(like, Enum):
        return type(like)(text.strip())
    if isinstance(like, int):
        return int(text)
    if isinstance(like, float):
        return float(text.replace(",", "."))
    return text


def iter_leaves(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield (path, value) for every scalar parameter."""
    if isinstance(obj, ExposureSet):
        for name in type(obj).model_fields:
            if name == "exposure_types":
                for et in obj.exposure_types:
                    yield from iter_leaves(et, f"{prefix}{et.kind}.")
            else:
                yield f"{prefix}{name}", getattr(obj, name)
        return
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            if name == "kind" or (name == "id" and isinstance(obj, ContourPass)):
                continue  # a pass id is part of its paths in format 2, not a parameter row
            yield from iter_leaves(getattr(obj, name), f"{prefix}{name}.")
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from iter_leaves(item, f"{prefix}{item_key(item, i)}.")
        return
    yield prefix.rstrip("."), obj


def flatten(obj: BaseModel) -> dict[str, Any]:
    return dict(iter_leaves(obj))


def field_meta(root: BaseModel, path: str) -> dict[str, Any]:
    """UI metadata (label, unit, guard, help, min, max) for a leaf path."""
    parent_path, _, leaf = path.rpartition(".")
    parent = get_path(root, parent_path) if parent_path else root
    info = type(parent).model_fields[leaf]
    extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
    return dict(extra)


def numeric_paths(root: BaseModel) -> list[str]:
    return [p for p, v in iter_leaves(root) if isinstance(v, (int, float)) and not isinstance(v, bool)
            and not p.endswith("schema_version")]


def diff(a: BaseModel, b: BaseModel) -> list[tuple[str, Any, Any]]:
    fa, fb = flatten(a), flatten(b)
    out = []
    for key in sorted(set(fa) | set(fb)):
        va, vb = fa.get(key, "—"), fb.get(key, "—")
        if va != vb:
            out.append((key, va, vb))
    return out
