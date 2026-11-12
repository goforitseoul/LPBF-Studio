"""R7-B: before/after measurements of the history scans. Measurement only; nothing here changes how LPBF Studio works.

The script measures whichever ``lpbfstudio`` is importable, so "before" and "after" are two runs of the same script with
two source trees on ``PYTHONPATH`` (the baseline ``ce62165`` and the current tree). It uses only what both trees have.

    python benchmarks/bench_r7b.py ops  --grid 100x10 300x20 --out after.json  [--base /dev/shm] [--reps 30]
    python benchmarks/bench_r7b.py scan --grid 300x20 --out scan.json --reference tests/data/history_reference_ce62165.py.txt

Safety. Every run builds its own library in a new folder made by ``tempfile.mkdtemp`` under ``--base`` (default: the
system temporary folder) and removes it afterwards. A base that looks like a library, or lies inside the default
library, is refused. No existing library is ever opened.

Fixture. ``OBJECTSxREVISIONS`` exposure-set objects written with the history module's own write primitives, laid out as
phase H lays them out, each with its canonical file; file and folder ``fsync`` are skipped while building only. Then
every history folder is given a modification time one minute in the past: the library is *at rest*, as a library is
when a person opens it. (An operation's memory, R7-B O-1, only remembers objects at rest; ``--fresh`` skips this step and
shows the other case.) Measured operations run the unchanged product code, with real ``fsync``.

``ops`` measures, per grid point: resolve of a tracked and of an absent path; ``get`` of a set in its own file; the
lookup of a name no file holds; save of a tracked set; save of a new set; copy; finalization of a job with 1, 5 and 20
exposure sets; a DOE batch of 10. For every sample it records the wall time, the time spent inside ``scan_objects`` (the
rest is the fixed cost of the write), the number of scans and of history files read, and, for writes, the exact number
of set revisions and objects present when the sample started.

``scan`` compares ``scan_objects`` of the importable tree with that of a reference copy of ``history.py`` in the same
process, on the same fixture, alternating between the two (the micro-benchmark for O-2).

Timing is ``time.perf_counter`` around one call; ``--warmup`` unmeasured calls first. Reported: median, mean, p95
(nearest rank), min, max, and every sample.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import math
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from lpbfstudio import __version__, doe
from lpbfstudio import history as h
from lpbfstudio import jobs
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, _slug, default_library_path
from lpbfstudio.models import MachineProfile, ProcessSettings

SETS = h.ObjectType.EXPOSURE_SET
FINALIZE_SETS = (1, 5, 20)
DOE_VARIANTS = 10


# --------------------------------------------------------------------------- safety and fixture


def safe_base(base: Path) -> Path:
    base = base.resolve()
    if not base.is_dir():
        raise SystemExit(f"refused: {base} is not a folder")
    if (base / "sets").exists() or (base / h.HISTORY_DIR).exists():
        raise SystemExit(f"refused: {base} looks like an LPBF Studio library")
    default = default_library_path().expanduser().resolve()
    if base == default or default in base.parents:
        raise SystemExit("refused: the base folder is inside the default library")
    return base


@contextlib.contextmanager
def no_fsync():
    saved = h._fsync
    h._fsync = lambda fd: None
    try:
        yield
    finally:
        h._fsync = saved


def write_object(root: Path, index: int, revisions: int, base_set) -> None:
    es = base_set.model_copy(deep=True)
    es.name, es.locked = f"Bench_{index:05d}", False
    path = f"sets/{_slug(es.name)}.json"
    object_id, obj = h._reserve_object(h.ensure_object_root(root, SETS))
    h._commit_identity(obj, SETS, object_id)
    text = ""
    for r in range(revisions):
        es.description = f"benchmark object {index}, revision {r + 1}"
        text = es.model_dump_json(indent=2)
        data = text.encode("utf-8")
        seq, rev = h._allocate(obj, "bench")
        h._write_blob(rev, data, "bench")
        op, source = ("created", None) if r == 0 else ("saved", {"object_id": object_id, "seq": h.seq_text(r)})
        h._commit_record(rev, h.build_record(SETS.value, object_id, seq, op, path, data=data, source=source), "bench")
    with open(root / path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def build(root: Path, objects: int, revisions: int) -> Library:
    lib = Library(root)
    lib.save_process(ProcessSettings())
    base_set = lib.get(default_sets()[0].name)
    with no_fsync():
        for i in range(objects):
            write_object(root, i, revisions, base_set)
    return lib


def rest(root: Path, seconds: float = 60.0) -> None:
    then = time.time() - seconds
    for base, dirs, _ in os.walk(root / h.HISTORY_DIR / h.OBJECTS_DIR):
        for d in dirs:
            os.utime(os.path.join(base, d), (then, then))


def census(root: Path) -> dict:
    """The exposure-set objects and revision folders present now (a listing only; nothing is read)."""
    folder = root / h.HISTORY_DIR / h.OBJECTS_DIR / SETS.value
    objects = revisions = 0
    for name in os.listdir(folder):
        objects += 1
        with contextlib.suppress(OSError):
            revisions += len(os.listdir(folder / name / h.REVISIONS))
    return {"objects": objects, "revisions": revisions}


def check(lib: Library, names: list[str]) -> dict:
    """One scan: nothing is damaged or incomplete, and the named sets are tracked at their object's head."""
    views = h.scan_objects(lib.root, SETS)
    problems = [f"{v.object_id}: {v.state.value} {v.detail}" for v in views if v.state is h.ObjectState.D5 or v.incomplete]
    for name in names:
        res = h.resolve(lib.root, SETS, f"sets/{_slug(name)}.json")
        if res.state is not h.PathState.TRACKED or res.seq != res.head_seq:
            problems.append(f"{name}: {res.state.value}")
    return {"ok": not problems, "problems": problems[:10], "objects": len(views)}


# --------------------------------------------------------------------------- measurement


class Probe:
    """Counts the scans and the history files read, and times the scans, through the module's own functions."""

    def __init__(self) -> None:
        self.scans = self.reads = 0
        self.scan_s = 0.0
        self._scan, self._read = h.scan_objects, h._read_entry
        probe = self

        def scan_objects(root, object_type):
            t = time.perf_counter()
            try:
                return probe._scan(root, object_type)
            finally:
                probe.scan_s += time.perf_counter() - t
                probe.scans += 1

        def read_entry(path):
            probe.reads += 1
            return probe._read(path)

        h.scan_objects, h._read_entry = scan_objects, read_entry

    def take(self) -> tuple[int, int, float]:
        got = (self.scans, self.reads, self.scan_s)
        self.scans = self.reads = 0
        self.scan_s = 0.0
        return got

    def close(self) -> None:
        h.scan_objects, h._read_entry = self._scan, self._read


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {"median": round(statistics.median(ordered), 4), "mean": round(statistics.fmean(ordered), 4), "p95": round(p95, 4),
            "min": round(ordered[0], 4), "max": round(ordered[-1], 4)}


def measure(name: str, op, warmup: int, reps: int, probe: Probe, out: dict, root: Path | None = None, prepare=None) -> None:
    """``prepare`` runs before each sample, unmeasured, and its result is passed to ``op``. ``root`` set: a write."""
    samples = []
    for i in range(warmup + reps):
        arg = prepare() if prepare else None
        before = census(root) if root is not None else None
        probe.take()
        t = time.perf_counter()
        op(arg) if prepare else op()
        wall = time.perf_counter() - t
        scans, reads, scan_s = probe.take()
        if i >= warmup:
            sample = {"wall_ms": round(wall * 1000, 4), "scan_ms": round(scan_s * 1000, 4),
                      "other_ms": round((wall - scan_s) * 1000, 4), "scans": scans, "history_files_read": reads}
            if before is not None:
                sample["set_revisions_at_start"], sample["set_objects_at_start"] = before["revisions"], before["objects"]
            samples.append(sample)
    out[name] = {"reps": reps, "warmup": warmup, "wall_ms": summary([s["wall_ms"] for s in samples]),
                 "scan_ms": summary([s["scan_ms"] for s in samples]), "other_ms": summary([s["other_ms"] for s in samples]),
                 "scans": samples[-1]["scans"], "history_files_read": samples[-1]["history_files_read"], "samples": samples}
    w = out[name]["wall_ms"]
    print(f"  {name:26s} median {w['median']:>10.3f} ms  p95 {w['p95']:>10.3f}  scan {out[name]['scan_ms']['median']:>10.3f}  "
          f"scans {out[name]['scans']:>2}  files read {out[name]['history_files_read']}", flush=True)


def run_ops(base: Path, objects: int, revisions: int, warmup: int, reps: int, fresh: bool) -> dict:
    if objects < max(FINALIZE_SETS) + 6:
        raise SystemExit(f"the grid needs at least {max(FINALIZE_SETS) + 6} objects")
    tmp = Path(tempfile.mkdtemp(prefix="lpbf-r7b-", dir=base))
    root = tmp / "library"
    probe = None
    try:
        t = time.perf_counter()
        lib = build(root, objects, revisions)
        build_s = time.perf_counter() - t
        if not fresh:
            rest(root)
        names = [f"Bench_{i:05d}" for i in range(objects)]
        pre = check(lib, names[:3])
        if not pre["ok"]:
            raise SystemExit(f"fixture check failed: {pre['problems']}")
        start = census(root)
        print(f"{objects}x{revisions}: {start['objects']} set objects, {start['revisions']} set revisions "
              f"(built in {build_s:.1f} s, {'fresh' if fresh else 'at rest'})", flush=True)
        probe = Probe()
        r: dict = {}
        counter = iter(range(10 ** 9))
        tracked = f"sets/{_slug(names[0])}.json"

        measure("resolve_tracked", lambda: h.resolve(root, SETS, tracked), warmup, reps, probe, r)
        measure("resolve_untracked_absent", lambda: h.resolve(root, SETS, "sets/not_there.json"), warmup, reps, probe, r)
        measure("get_set", lambda: lib.get(names[1]), warmup, reps, probe, r)
        measure("lookup_new_name", lambda: lib.exists("No such set"), warmup, reps, probe, r)

        def save_tracked():
            es = lib.get(names[2])
            es.description = f"edit {next(counter)}"
            lib.save(es)

        def new_set():
            es = lib.get(names[2]).model_copy(deep=True)
            es.name = f"New_{next(counter):06d}"
            return es

        measure("save_tracked", save_tracked, warmup, reps, probe, r, root)
        measure("save_new_object", lib.save, warmup, reps, probe, r, root, prepare=new_set)
        measure("copy", lambda: lib.copy(names[3], f"Copy_{next(counter):06d}"), warmup, reps, probe, r, root)

        for k in FINALIZE_SETS:
            def draft(k=k):
                sets = [{"name": names[5 + i], "role": "part", "z_range": None} for i in range(k)]
                out = jobs.save_draft(lib, None, sets, MachineProfile().name, "Ti64")
                assert out.status is jobs.JobStatus.DRAFT_SAVED, out
                return out.build_job_id

            def finalize(job_id):
                out = jobs.finalize(lib, job_id)
                assert out.status is jobs.JobStatus.FINALIZED, out

            measure(f"job_finalize_{k}", finalize, warmup, reps, probe, r, root, prepare=draft)

        def variants():
            source, k = lib.get(names[4]), next(counter)
            made = []
            for v in range(DOE_VARIANTS):
                x = source.model_copy(deep=True)
                x.name, x.locked, x.description = f"Doe_{k:06d}_{v:02d}", False, f"variant {v}"
                made.append(x)
            return made

        def doe_batch(made):
            with doe.batch_source(names[4]):
                res = doe.save_batch(lib, made)
            assert res.complete, res.report()

        measure(f"doe_batch_{DOE_VARIANTS}", doe_batch, min(warmup, 1), max(5, reps // 3), probe, r, root, prepare=variants)

        probe.close()
        probe = None
        post = check(lib, names[:6])
        print(f"  correctness after the operations: {'ok' if post['ok'] else post['problems']}", flush=True)
        return {"objects": objects, "revisions_per_object": revisions, "at_rest": not fresh, "fixture_build_s": round(build_s, 2),
                "census_before": start, "census_after": census(root), "check_before": pre, "check_after": post, "results": r}
    finally:
        if probe is not None:
            probe.close()
        shutil.rmtree(tmp, ignore_errors=True)


def load_reference(path: Path):
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    name = "lpbfstudio._history_reference_for_benchmark"
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module, hashlib.sha256(raw).hexdigest()


def run_scan(base: Path, objects: int, revisions: int, warmup: int, reps: int, reference) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="lpbf-r7b-", dir=base))
    root = tmp / "library"
    try:
        build(root, objects, revisions)
        rest(root)
        ref_type = reference.ObjectType.EXPOSURE_SET
        same = [(v.object_id, v.state.value, v.a_max, v.head.seq) for v in h.scan_objects(root, SETS)] == \
               [(v.object_id, v.state.value, v.a_max, v.head.seq) for v in reference.scan_objects(root, ref_type)]
        current, before = [], []
        for i in range(warmup + reps):
            pair = []
            for scan, kind in ((reference.scan_objects, ref_type), (h.scan_objects, SETS)) if i % 2 else \
                    ((h.scan_objects, SETS), (reference.scan_objects, ref_type)):
                t = time.perf_counter()
                scan(root, kind)
                pair.append((kind is SETS, (time.perf_counter() - t) * 1000))
            if i >= warmup:
                for is_current, ms in pair:
                    (current if is_current else before).append(round(ms, 4))
        total = census(root)["revisions"]
        out = {"objects": objects, "revisions_per_object": revisions, "set_revisions": total, "same_views": same, "reps": reps,
               "reference_ms": summary(before), "current_ms": summary(current),
               "reference_us_per_revision": round(statistics.median(before) * 1000 / total, 3),
               "current_us_per_revision": round(statistics.median(current) * 1000 / total, 3),
               "ratio_current_to_reference": round(statistics.median(current) / statistics.median(before), 4),
               "reference_samples": before, "current_samples": current}
        print(f"{objects}x{revisions}: reference {out['reference_ms']['median']:.3f} ms, current {out['current_ms']['median']:.3f} ms, "
              f"ratio {out['ratio_current_to_reference']:.3f}, same views: {same}", flush=True)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- environment


def filesystem(path: Path) -> str:
    if sys.platform.startswith("linux"):
        best, kind = "", "unknown"
        with contextlib.suppress(OSError), open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                mount = left.split()[4]
                if (str(path) == mount or str(path).startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                    best, kind = mount, right.split()[0]
        return kind
    if os.name == "nt":
        import ctypes
        name = ctypes.create_unicode_buffer(64)
        drive = os.path.splitdrive(str(path))[0] + "\\"
        if ctypes.windll.kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, name, 64):
            return name.value
    return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["ops", "scan"])
    ap.add_argument("--grid", nargs="+", default=["100x10"], help="OBJECTSxREVISIONS, e.g. 100x10 300x20")
    ap.add_argument("--base", type=Path, default=Path(tempfile.gettempdir()), help="where the temporary library is made")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--fresh", action="store_true", help="ops: do not put the fixture at rest before measuring")
    ap.add_argument("--reference", type=Path, help="scan: a copy of the history.py to compare with")
    args = ap.parse_args(argv)
    base = safe_base(args.base)
    grid = [tuple(int(x) for x in g.lower().split("x")) for g in args.grid]
    doc = {"benchmark": "R7-B", "mode": args.mode, "label": args.label, "lpbfstudio_version": __version__,
           "history_module": str(Path(h.__file__).resolve()), "history_sha256": hashlib.sha256(
               Path(h.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
           "python": sys.version, "implementation": platform.python_implementation(), "platform": platform.platform(),
           "machine": platform.machine(), "cpu_count": os.cpu_count(), "base": str(base), "filesystem": filesystem(base),
           "cache_state": "warm: the fixture was written by this process just before, and warm-up calls precede the samples",
           "fsync": "real in every measured operation; skipped only while building the fixture",
           "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runs": []}
    if args.mode == "scan":
        if args.reference is None:
            raise SystemExit("scan needs --reference")
        reference, digest = load_reference(args.reference)
        doc["reference"], doc["reference_sha256"] = str(args.reference), digest
        for objects, revisions in grid:
            doc["runs"].append(run_scan(base, objects, revisions, args.warmup, args.reps, reference))
    else:
        for objects, revisions in grid:
            doc["runs"].append(run_ops(base, objects, revisions, args.warmup, args.reps, args.fresh))
    doc["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
