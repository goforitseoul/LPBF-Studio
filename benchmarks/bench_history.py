"""R7: reproducible history performance measurements. Measurement only; nothing here changes how LPBF Studio works.

Every run builds its own library in a new temporary folder and removes it afterwards. It never opens an existing library:
the folder is made by ``tempfile.mkdtemp`` under ``--base`` (default: the system temporary folder), and a base that looks
like a library, or lies inside the default library, is refused.

    python benchmarks/bench_history.py --scenarios S M L XL --out results.json [--base /dev/shm]

Fixture. ``Library(root)`` seeds the shipped sets, the machine profile and ``process.json`` through the normal write
path. The benchmark objects are then written with the history module's own write primitives (identity, revision
folder, blob, record), exactly as phase H lays them out, followed by the canonical file. This avoids building thousands
of revisions through ``Library.save``, whose cost grows with the total number of revisions. Folder ``fsync`` and file
``fsync`` are skipped while building the fixture only (its durability is not under test); measured operations run the
unchanged code. Before any measurement the fixture is checked: every object is live or tombstoned, every canonical
file resolves as tracked at its object's head, nothing is incomplete or damaged.

Timing. ``time.perf_counter`` around one call. Each operation runs ``warmup`` times unmeasured, then ``reps`` times.
Reported: median, p95 (nearest rank), min, max. Memory: the peak of ``tracemalloc`` over one more, separate, call.
Write operations add revisions or objects to the fixture; each run reports the totals before and after.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from lpbfstudio import __version__
from lpbfstudio import doe
from lpbfstudio import history as h
from lpbfstudio import jobs
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, _slug, default_library_path
from lpbfstudio.models import MachineProfile, ProcessSettings

SETS = h.ObjectType.EXPOSURE_SET

# name: (objects, revisions per object, tombstoned objects, warm-up, repetitions, writes measured)
SCENARIOS = {
    "S": (10, 5, 1, 2, 20, True),
    "M": (100, 10, 5, 2, 20, True),
    "L": (300, 20, 15, 1, 10, True),
    "XL": (1000, 20, 50, 1, 5, True),
}
DOE_VARIANTS = 10


# --------------------------------------------------------------------------- safety


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


# --------------------------------------------------------------------------- fixture


@contextlib.contextmanager
def no_fsync():
    saved = h._fsync
    h._fsync = lambda fd: None
    try:
        yield
    finally:
        h._fsync = saved


def write_object(root: Path, name_index: int, revisions: int, base_set, tombstone: bool) -> None:
    es = base_set.model_copy(deep=True)
    es.name = f"Bench_{name_index:05d}"
    es.locked = False
    path = f"sets/{_slug(es.name)}.json"
    folder = h.ensure_object_root(root, SETS)
    object_id, obj = h._reserve_object(folder)
    h._commit_identity(obj, SETS, object_id)
    text = ""
    for r in range(revisions):
        es.description = f"benchmark object {name_index}, revision {r + 1}"
        text = es.model_dump_json(indent=2)
        data = text.encode("utf-8")  # the canonical routine writes UTF-8 text, newline untranslated
        seq, rev = h._allocate(obj, "bench")
        h._write_blob(rev, data, "bench")
        op, source = ("created", None) if r == 0 else ("saved", {"object_id": object_id, "seq": h.seq_text(r)})
        h._commit_record(rev, h.build_record(SETS.value, object_id, seq, op, path, data=data, source=source), "bench")
    if tombstone:
        seq, rev = h._allocate(obj, "bench")
        source = {"object_id": object_id, "seq": h.seq_text(revisions)}
        h._commit_record(rev, h.build_record(SETS.value, object_id, seq, "deleted", path, source=source), "bench")
    else:
        with open(root / path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def build(root: Path, objects: int, revisions: int, tombstones: int) -> Library:
    lib = Library(root)
    lib.save_process(ProcessSettings())
    base_set = lib.get(default_sets()[0].name)
    with no_fsync():
        for i in range(objects):
            write_object(root, i, revisions, base_set, tombstone=i >= objects - tombstones)
    return lib


def describe(root: Path) -> dict:
    hist = root / h.HISTORY_DIR
    entries = files = 0
    blob_bytes = record_bytes = records = 0
    for dirpath, dirnames, filenames in os.walk(hist):
        entries += len(dirnames) + len(filenames)
        files += len(filenames)
        for f in filenames:
            size = os.path.getsize(os.path.join(dirpath, f))
            if f == h.CONTENT:
                blob_bytes += size
            elif f == h.RECORD:
                record_bytes += size
                records += 1
    views = h.scan_objects(root, SETS)
    return {
        "set_objects": len(views),
        "live": sum(v.state is h.ObjectState.D3 for v in views),
        "tombstoned": sum(v.state is h.ObjectState.D4 for v in views),
        "set_revisions": sum(len(v.committed) for v in views),
        "all_records": records,
        "content_bytes": blob_bytes,
        "record_bytes": record_bytes,
        "mean_record_bytes": round(record_bytes / records) if records else 0,
        "mean_content_bytes": round(blob_bytes / max(1, sum(1 for v in views for r in v.committed if r.kind == "content"))),
        "history_entries": entries,
        "history_files": files,
        "canonical_set_files": len(list((root / "sets").glob("*.json"))),
    }


def check(lib: Library) -> dict:
    """Correctness after the operations: every object classifiable, every canonical file tracked at its head."""
    root = lib.root
    problems: list[str] = []
    views = h.scan_objects(root, SETS)
    for v in views:
        if v.state not in (h.ObjectState.D3, h.ObjectState.D4) or v.incomplete:
            problems.append(f"{v.object_id}: {v.state.value} {v.incomplete} {v.detail}")
    for f in sorted((root / "sets").glob("*.json")):
        res = h.resolve(root, SETS, f"sets/{f.name}")
        if res.state is not h.PathState.TRACKED or res.history_ahead or res.seq != res.head_seq:
            problems.append(f"{f.name}: {res.state.value}")
            continue
        blob = jobs._read_blob(root, SETS, res.object_id, res.seq)
        if blob != f.read_bytes():
            problems.append(f"{f.name}: blob differs")
    for v in views:
        if v.state is h.ObjectState.D4 and (root / v.head.path).exists():
            problems.append(f"{v.head.path}: tombstoned but present")
    staging = root / h.HISTORY_DIR / h.STAGING_DIR
    if staging.exists() and os.listdir(staging):
        problems.append(f"staging not empty: {len(os.listdir(staging))}")
    lib.load_all()
    if lib.load_errors:
        problems.append(f"load errors: {lib.load_errors[:3]}")
    return {"ok": not problems, "problems": problems[:20], "objects_checked": len(views)}


# --------------------------------------------------------------------------- measurement


def stats(samples: list[float]) -> dict:
    s = sorted(samples)
    p95 = s[max(0, math.ceil(0.95 * len(s)) - 1)]
    return {"n": len(s), "median_ms": round(statistics.median(s) * 1e3, 3), "p95_ms": round(p95 * 1e3, 3),
            "min_ms": round(s[0] * 1e3, 3), "max_ms": round(s[-1] * 1e3, 3)}


def measure(name: str, op, warmup: int, reps: int, out: dict) -> None:
    for _ in range(warmup):
        op()
    samples = []
    for _ in range(reps):
        t = time.perf_counter()
        op()
        samples.append(time.perf_counter() - t)
    tracemalloc.start()
    try:
        op()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    out[name] = {**stats(samples), "peak_alloc_kib": round(peak / 1024, 1)}
    print(f"  {name:28s} median {out[name]['median_ms']:>10.3f} ms  p95 {out[name]['p95_ms']:>10.3f} ms  "
          f"peak {out[name]['peak_alloc_kib']:>9.1f} KiB", flush=True)


class Counter:
    def __init__(self, start: int = 0) -> None:
        self.n = start

    def next(self) -> int:
        self.n += 1
        return self.n


def profile(op) -> list[dict]:
    """Where one call spends its time: the 12 functions with the most own time, and the call counts of the file
    system and hashing primitives."""
    import cProfile
    import pstats
    pr = cProfile.Profile()
    pr.enable()
    op()
    pr.disable()
    st = pstats.Stats(pr)
    rows = []
    for (file, line, fn), (cc, nc, tt, ct, _) in st.stats.items():
        rows.append({"function": f"{Path(file).name}:{fn}" if line else fn, "calls": nc, "own_ms": round(tt * 1e3, 2),
                     "cumulative_ms": round(ct * 1e3, 2)})
    rows.sort(key=lambda x: -x["own_ms"])
    return rows[:12]


def run_scenario(base: Path, key: str, writes: bool, warmup: int | None = None, reps: int | None = None,
                 profiled: bool = False) -> dict:
    objects, revisions, tombstones, w0, r0, _ = SCENARIOS[key]
    warmup = w0 if warmup is None else warmup
    reps = r0 if reps is None else reps
    tmp = Path(tempfile.mkdtemp(prefix="lpbf-bench-", dir=base))
    root = tmp / "library"
    try:
        t = time.perf_counter()
        lib = build(root, objects, revisions, tombstones)
        build_s = time.perf_counter() - t
        before = describe(root)
        pre = check(lib)
        if not pre["ok"]:
            raise SystemExit(f"fixture check failed: {pre['problems']}")
        print(f"{key}: {objects} objects x {revisions} revisions, {before['set_revisions']} set revisions, "
              f"{before['history_entries']} history entries (built in {build_s:.1f} s)", flush=True)
        live = [f"Bench_{i:05d}" for i in range(objects - tombstones)]
        tracked = f"sets/{_slug(live[0])}.json"
        n = Counter()
        r: dict = {}

        # reads
        measure("library_open", lambda: Library(root), warmup, reps, r)
        measure("load_all", lib.load_all, warmup, reps, r)
        measure("scan_objects", lambda: h.scan_objects(root, SETS), warmup, reps, r)
        measure("resolve_tracked", lambda: h.resolve(root, SETS, tracked), warmup, reps, r)
        measure("resolve_untracked_absent", lambda: h.resolve(root, SETS, "sets/not_there.json"), warmup, reps, r)
        obj_id = h.resolve(root, SETS, tracked).object_id
        folder = root / h.HISTORY_DIR / h.OBJECTS_DIR / SETS.value / obj_id
        measure("classify_one_object", lambda: h._classify_object(folder, SETS.value, obj_id), warmup, reps, r)
        measure("get_set", lambda: lib.get(live[1]), warmup, reps, r)

        profiles = {}
        if profiled:
            profiles["resolve_tracked"] = profile(lambda: h.resolve(root, SETS, tracked))

        if writes:
            def save_tracked():
                es = lib.get(live[2])
                es.description = f"edit {n.next()}"
                lib.save(es)

            created: list[str] = []

            def save_new():
                es = lib.get(live[2]).model_copy(deep=True)
                es.name = f"New_{n.next():06d}"
                lib.save(es)
                created.append(es.name)

            def adopt_and_save():  # an untracked canonical file, saved: adoption then a content revision
                k = n.next()
                es = lib.get(live[2]).model_copy(deep=True)
                es.name = f"Adopt_{k:06d}"
                (root / f"sets/{_slug(es.name)}.json").write_text(es.model_dump_json(indent=2), encoding="utf-8")
                es.description = "adopted"
                lib.save(es)

            def copy():
                lib.copy(live[2], f"Copy_{n.next():06d}")

            names = {"current": live[3]}

            def rename():
                new = f"Renamed_{n.next():06d}"
                lib.rename(names["current"], new)
                names["current"] = new

            def delete():  # the sets made by save_new_object: one tracked object, one revision each
                lib.delete(created.pop())

            job_ids = []

            def job_draft():
                out = jobs.save_draft(lib, None, [{"name": live[1], "role": "part", "z_range": None}],
                                      MachineProfile().name, "Ti64")
                assert out.status is jobs.JobStatus.DRAFT_SAVED, out
                job_ids.append(out.build_job_id)

            def job_finalize():
                out = jobs.save_draft(lib, None, [{"name": live[1], "role": "part", "z_range": None}],
                                      MachineProfile().name, "Ti64")
                t0 = time.perf_counter()
                fin = jobs.finalize(lib, out.build_job_id)
                assert fin.status is jobs.JobStatus.FINALIZED, fin
                return time.perf_counter() - t0

            def doe_batch():
                base = lib.get(live[4])
                k = n.next()
                variants = []
                for v in range(DOE_VARIANTS):
                    x = base.model_copy(deep=True)
                    x.name, x.locked = f"Doe_{k:06d}_{v:02d}", False
                    x.description = f"variant {v}"
                    variants.append(x)
                with doe.batch_source(base.name):
                    res = doe.save_batch(lib, variants)
                assert res.complete, res.report()

            measure("save_tracked", save_tracked, warmup, reps, r)
            measure("save_new_object", save_new, warmup, reps, r)
            measure("adopt_untracked_then_save", adopt_and_save, warmup, reps, r)
            measure("copy", copy, warmup, reps, r)
            measure("rename", rename, warmup, reps, r)
            measure("delete_tombstone", delete, warmup, reps, r)
            measure("job_save_draft", job_draft, warmup, reps, r)
            fin = [job_finalize() for _ in range(warmup + reps)][warmup:]
            r["job_finalize"] = stats(fin)
            print(f"  {'job_finalize':28s} median {r['job_finalize']['median_ms']:>10.3f} ms  p95 {r['job_finalize']['p95_ms']:>10.3f} ms",
                  flush=True)
            measure(f"doe_batch_{DOE_VARIANTS}", doe_batch, max(0, warmup - 1), max(3, reps // 3), r)
            if profiled:
                profiles["save_tracked"] = profile(save_tracked)

        after = describe(root)
        post = check(lib)
        print(f"  correctness after operations: {'ok' if post['ok'] else post['problems']}", flush=True)
        return {"scenario": key, "objects": objects, "revisions_per_object": revisions, "tombstoned_objects": tombstones,
                "warmup": warmup, "reps": reps, "fixture_build_s": round(build_s, 2), "before": before, "after": after,
                "results": r, "profiles": profiles, "correctness_before": pre, "correctness_after": post}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- environment


def filesystem(path: Path) -> str:
    if sys.platform == "win32":
        import ctypes
        buf = ctypes.create_unicode_buffer(64)
        drive = os.path.splitdrive(str(path.resolve()))[0] + "\\"
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0, None, None, None, buf, 64)
        return buf.value if ok else "unknown"
    best, fstype = "", "unknown"
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                mount = parts[1].replace("\\040", " ")
                if str(path.resolve()).startswith(mount.rstrip("/") + "/") or str(path.resolve()) == mount:
                    if len(mount) > len(best):
                        best, fstype = mount, parts[2]
    except OSError:
        pass
    return fstype


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", type=Path, default=Path(tempfile.gettempdir()), help="where the temporary library is made")
    ap.add_argument("--scenarios", nargs="+", default=["S", "M", "L"], choices=list(SCENARIOS))
    ap.add_argument("--reads-only", nargs="*", default=[], choices=list(SCENARIOS), help="scenarios measured without writes")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--reps", type=int, help="override the scenario's repetitions")
    ap.add_argument("--warmup", type=int, help="override the scenario's warm-up runs")
    ap.add_argument("--profile", action="store_true", help="also record a cProfile breakdown of one resolve and one save")
    args = ap.parse_args(argv)
    base = safe_base(args.base)
    env = {"label": args.label, "lpbf_version": __version__, "python": platform.python_version(),
           "implementation": platform.python_implementation(), "os": platform.system(), "os_release": platform.release(),
           "machine": platform.machine(), "filesystem": filesystem(base), "cpu_count": os.cpu_count(),
           "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print(json.dumps(env), flush=True)
    runs = [run_scenario(base, key, writes=key not in args.reads_only, warmup=args.warmup, reps=args.reps,
                         profiled=args.profile) for key in args.scenarios]
    args.out.write_text(json.dumps({"environment": env, "runs": runs}, indent=1), encoding="utf-8")
    return 0 if all(r["correctness_after"]["ok"] for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
