"""Test helpers for history (DC-001 v0.6). These build on-disk states by hand, independently of ``history.py``, so that
the resolver is tested against the contract and not against its own writer."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ID_A = "a" * 32
ID_B = "b" * 32
ID_C = "c" * 32
SETS = "exposure_set"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seq(n: int) -> str:
    return f"{n:08d}"


def objdir(root: Path, oid: str = ID_A, otype: str = SETS) -> Path:
    return root / ".history" / "objects" / otype / oid


def identity_doc(oid: str = ID_A, otype: str = SETS) -> bytes:
    return json.dumps({"object_type": otype, "object_id": oid, "created_at": "2026-11-08T00:00:00Z"}).encode()


def make_identity(root: Path, oid: str = ID_A, otype: str = SETS) -> Path:
    d = objdir(root, oid, otype)
    d.mkdir(parents=True)
    (d / "identity.json").write_bytes(identity_doc(oid, otype))
    return d


def record_doc(n: int, path: str = "sets/a.json", *, oid: str = ID_A, otype: str = SETS, kind: str = "content",
               operation: str | None = None, data: bytes | None = b"x", previous_path: str | None = None,
               lock_change: str | None = None, source=None, **over) -> dict:
    op = operation or ("deleted" if kind == "deleted" else ("adopted" if n == 1 else "saved"))
    if source is None and op in ("saved", "renamed", "deleted"):
        source = {"object_id": oid, "seq": seq(n - 1)} if n > 1 else None
    doc = {"record_version": 1, "object_type": otype, "object_id": oid, "seq": seq(n), "kind": kind, "operation": op,
           "path": path, "previous_path": previous_path,
           "content_hash": {"algorithm": "sha256", "value": sha(data)} if kind == "content" else None,
           "byte_length": len(data) if kind == "content" else None, "lock_change": lock_change, "source": source,
           "created_at": "2026-11-08T00:00:00Z", "software": {"version": "0.1.0", "git_commit": "unknown"},
           "platform": {"os": "test", "architecture": "x", "python_version": "3"}}
    doc.update(over)
    return doc


def add_revision(root: Path, n: int, data: bytes | None = b"x", path: str = "sets/a.json", *, oid: str = ID_A,
                 otype: str = SETS, kind: str = "content", **kw) -> Path:
    rev = objdir(root, oid, otype) / "revisions" / seq(n)
    rev.mkdir(parents=True)
    if kind == "content":
        (rev / "content").write_bytes(data)
    (rev / "record.json").write_text(json.dumps(record_doc(n, path, oid=oid, otype=otype, kind=kind, data=data, **kw)),
                                     encoding="utf-8")
    return rev


def make_object(root: Path, datas=(b"x",), path: str = "sets/a.json", *, oid: str = ID_A, otype: str = SETS) -> Path:
    make_identity(root, oid, otype)
    for i, d in enumerate(datas, 1):
        add_revision(root, i, d, path, oid=oid, otype=otype)
    return objdir(root, oid, otype)


def canonical(root: Path, data: bytes = b"x", path: str = "sets/a.json") -> Path:
    f = root / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def tree(root: Path) -> dict[str, bytes | None]:
    """Every entry below ``root``, with file bytes. Used for 'nothing changed' assertions."""
    out: dict[str, bytes | None] = {}
    for base, dirs, files in os.walk(root):
        for d in dirs:
            out[Path(base, d).relative_to(root).as_posix()] = None
        for f in files:
            p = Path(base, f)
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def junction(link: Path, target: Path) -> None:
    """A real Windows junction."""
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def can_symlink(tmp: Path) -> bool:
    try:
        os.symlink(tmp / "nowhere", tmp / "probe-link")
    except (OSError, NotImplementedError):
        return False
    os.unlink(tmp / "probe-link")
    return True
