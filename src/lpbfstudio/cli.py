"""Command-line interface: ``lpbf <command>``."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import click

from . import APP_NAME, __version__, calc
from .doe import (DEFAULT_MAX_VARIANTS, batch_source, collect_factors, count_text, generate, level_lines, parse_values,
                  save_batch)
from .estimate import estimate_plan, fmt_duration
from .library import Library, LibraryError, export_json
from .models import SetCategory
from .defaults import new_set
from .paths import diff, field_meta, flatten, get_path, with_assignments
from .validation import Severity, check_machine, has_errors


def _lib(ctx: click.Context) -> Library:
    return ctx.obj["lib"]


def _fail(msg: str) -> None:
    click.secho(f"error: {msg}", fg="red", err=True)
    sys.exit(1)


@click.group()
@click.option("--library", "library_path", type=click.Path(file_okay=False), envvar="LPBF_LIBRARY",
              help="Library folder (default ~/.lpbfstudio/library or $LPBF_LIBRARY).")
@click.version_option(__version__, prog_name=APP_NAME)
@click.pass_context
def main(ctx: click.Context, library_path: str | None) -> None:
    """LPBF Studio: create, check and vary laser powder bed fusion parameter sets."""
    ctx.ensure_object(dict)
    ctx.obj["lib"] = Library(library_path)


@main.command("list")
@click.pass_context
def list_sets(ctx: click.Context) -> None:
    """List exposure sets in the library."""
    lib = _lib(ctx)
    for name, es in sorted(lib.load_all().items(), key=lambda kv: kv[0].lower()):
        lock = "locked" if es.locked else "      "
        types = " > ".join(et.node_label for et in es.exposure_types)
        fmt = f"  (format {es.schema_version})" if es.schema_version != 1 else ""  # format 1 lines unchanged
        click.echo(f"{lock}  {es.category.value:7}  {es.layer_thickness_mm * 1000:4.0f} µm  {name:32}  {types}{fmt}")
    for path, reason in lib.load_errors:
        click.secho(f"warning: skipped {path.name}: {reason}", fg="yellow", err=True)


@main.command()
@click.argument("name")
@click.option("--category", type=click.Choice([c.value for c in SetCategory]), default="part")
@click.pass_context
def new(ctx: click.Context, name: str, category: str) -> None:
    """Create a new, empty-default exposure set."""
    lib = _lib(ctx)
    try:
        lib.save(new_set(name, SetCategory(category)), overwrite=False)
    except LibraryError as exc:
        _fail(str(exc))
    click.echo(f"created {name}")


@main.command()
@click.argument("source")
@click.argument("new_name")
@click.pass_context
def copy(ctx: click.Context, source: str, new_name: str) -> None:
    """Copy a set (the way to start from a locked reference set)."""
    try:
        _lib(ctx).copy(source, new_name)
    except LibraryError as exc:
        _fail(str(exc))
    click.echo(f"copied {source} -> {new_name}")


@main.command()
@click.argument("old")
@click.argument("new_name")
@click.pass_context
def rename(ctx: click.Context, old: str, new_name: str) -> None:
    """Rename an unlocked set."""
    try:
        _lib(ctx).rename(old, new_name)
    except LibraryError as exc:
        _fail(str(exc))
    click.echo(f"renamed {old} -> {new_name}")


@main.command()
@click.argument("name")
@click.pass_context
def delete(ctx: click.Context, name: str) -> None:
    """Delete an unlocked set."""
    try:
        _lib(ctx).delete(name)
    except LibraryError as exc:
        _fail(str(exc))
    click.echo(f"deleted {name}")


@main.command()
@click.argument("name")
@click.option("--filter", "pattern", default="", help="Only show paths containing this text.")
@click.pass_context
def show(ctx: click.Context, name: str, pattern: str) -> None:
    """Print every parameter of a set with its path, value and unit."""
    es = _get(ctx, name)
    for path, value in flatten(es).items():
        if pattern and pattern not in path:
            continue
        meta = field_meta(es, path)
        unit = meta.get("unit", "")
        v = value.value if hasattr(value, "value") else value
        click.echo(f"{path:52} {v!s:>14} {unit}")


@main.command("set")
@click.argument("name")
@click.argument("assignments", nargs=-1, required=True)
@click.pass_context
def set_values(ctx: click.Context, name: str, assignments: tuple[str, ...]) -> None:
    """Change parameters: lpbf set MySet hatch.infill.power_w=300 layer_thickness_mm=0.03"""
    lib = _lib(ctx)
    es = _get(ctx, name)
    if es.locked:
        _fail(f"{name} is locked; copy it first (lpbf copy {name} NEW_NAME)")
    pairs = []
    for a in assignments:
        path, sep, value = a.partition("=")
        if not sep:
            _fail(f"expected path=value, got {a!r}")
        pairs.append((path.strip(), value.strip()))
    try:
        es = with_assignments(es, pairs)  # all or nothing
    except (KeyError, ValueError) as exc:
        _fail(str(exc).splitlines()[0])
    try:
        lib.save(es)
    except LibraryError as exc:
        _fail(str(exc))
    click.echo(f"updated {name}")


@main.command("validate")
@click.argument("names", nargs=-1)
@click.pass_context
def validate_cmd(ctx: click.Context, names: tuple[str, ...]) -> None:
    """Check sets against the machine profile each one names and the process rules (all sets if none given)."""
    lib = _lib(ctx)
    process = lib.load_process()
    targets = names or tuple(lib.names())
    failed = False
    if not names:  # a file that could not be loaded was not validated, so "all sets" did not pass
        for path, reason in lib.load_errors:
            click.secho(f"{path.name}: FAIL, not validated ({reason})", bold=True, fg="red")
            failed = True
    for n in targets:
        issues = lib.validate(_get(ctx, n), process)
        failed |= _print_issues(n, issues)
    _warn_machine_errors(lib)
    sys.exit(1 if failed else 0)


COLORS = {Severity.ERROR: "red", Severity.WARNING: "yellow", Severity.INFO: "cyan"}


def _print_issues(title: str, issues) -> bool:
    status = "FAIL" if has_errors(issues) else "ok"
    click.secho(f"{title}: {status}", bold=True, fg="red" if status == "FAIL" else "green")
    for i in issues:
        click.secho(f"  {i}", fg=COLORS[i.severity])
    return has_errors(issues)


def _warn_machine_errors(lib: Library) -> None:
    for path, reason in lib.machine_errors:
        click.secho(f"warning: machine profile file {path.name}: {reason}", fg="yellow", err=True)


@main.group()
def machine() -> None:
    """Machine profiles that sets refer to by name."""


@machine.command("list")
@click.pass_context
def machine_list(ctx: click.Context) -> None:
    """List machine profiles and whether each one is consistent."""
    lib = _lib(ctx.parent)
    profiles = lib.load_machines()
    users: dict[str, int] = {}
    for es in lib.load_all().values():
        users[es.machine] = users.get(es.machine, 0) + 1
    for name, mp in sorted(profiles.items(), key=lambda kv: kv[0].lower()):
        status = "invalid" if has_errors(check_machine(mp)) else "ok"
        click.echo(f"{status:8} {name:32} {mp.max_power_w:6.0f} W  {mp.max_speed_mm_s:6.0f} mm/s  "
                   f"{mp.min_layer_mm * 1000:.0f}–{mp.max_layer_mm * 1000:.0f} µm  used by {users.get(name, 0)} set(s)")
    _warn_machine_errors(lib)


@machine.command("check")
@click.argument("names", nargs=-1)
@click.pass_context
def machine_check(ctx: click.Context, names: tuple[str, ...]) -> None:
    """Check machine profiles for consistency (all if none given)."""
    lib = _lib(ctx.parent)
    profiles = lib.load_machines()
    failed = False
    for n in names or tuple(sorted(profiles)):
        if n not in profiles:
            click.secho(f"error: no machine profile named {n!r}", fg="red", err=True)
            failed = True
            continue
        failed |= _print_issues(n, check_machine(profiles[n]))
    _warn_machine_errors(lib)
    sys.exit(1 if failed else 0)


@machine.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--overwrite", is_flag=True)
@click.pass_context
def machine_import(ctx: click.Context, file: str, overwrite: bool) -> None:
    """Import a machine profile from a JSON file."""
    try:
        mp = _lib(ctx.parent).import_machine(file, overwrite=overwrite)
    except (LibraryError, ValueError) as exc:
        _fail(str(exc).splitlines()[0])
    click.echo(f"imported machine profile {mp.name}")


@machine.command("export")
@click.argument("name")
@click.argument("output", type=click.Path(dir_okay=False))
@click.pass_context
def machine_export(ctx: click.Context, name: str, output: str) -> None:
    """Export a machine profile to a JSON file."""
    try:
        mp = _lib(ctx.parent).get_machine(name)
    except LibraryError as exc:
        _fail(str(exc))
    Path(output).write_text(mp.model_dump_json(indent=2), encoding="utf-8")
    click.echo(f"wrote {output}")


@main.command()
@click.argument("name")
@click.pass_context
def energy(ctx: click.Context, name: str) -> None:
    """Energy input of every exposure region, and effective scan-line offsets."""
    lib = _lib(ctx)
    es = _get(ctx, name)
    click.echo(f"{'Exposure':10} {'Region':26} {'P [W]':>8} {'v [mm/s]':>9} {'h [mm]':>7} {'E':>9}")
    for m in calc.energy_summary(es):
        h = f"{m.hatch_mm:.3f}" if m.hatch_mm else "—"
        click.echo(f"{m.exposure:10} {m.region:26} {m.power_w:8.1f} {m.speed_mm_s:9.0f} {h:>7} {m.energy:9.2f} {m.unit}")
    for m in calc.energy_summary(es):
        if m.cycle is not None:
            click.echo(f"\n{calc.CYCLE_ENERGY_LABEL}, {m.exposure} {m.region}, infill skipping n={m.cycle.skip_layers}:")
            click.echo(f"  {calc.cycle_energy_text(m.cycle)}")
            click.echo(f"  E_eff = P/(v·h·(n+1)·t): an {calc.CYCLE_ENERGY_CAVEAT}.")
            click.echo("  The energy above and the energy-window checks use the nominal value.")
    click.echo("\nOffsets from nominal contour (GBO from process settings, PBO = 0):")
    for k, v in calc.effective_offsets(es, lib.load_process()).items():
        click.echo(f"  {k:14} {v:+.3f} mm")
    note = calc.homogenization_note(es)
    if note:
        click.echo(f"\n{note}")


@main.command()
@click.argument("a")
@click.argument("b")
@click.pass_context
def compare(ctx: click.Context, a: str, b: str) -> None:
    """Show parameters that differ between two sets."""
    rows = diff(_get(ctx, a), _get(ctx, b))
    if not rows:
        click.echo("identical")
    for path, va, vb in rows:
        fa = va.value if hasattr(va, "value") else va
        fb = vb.value if hasattr(vb, "value") else vb
        click.echo(f"{path:52} {fa!s:>16}  ->  {fb!s}")


@main.command()
@click.argument("base")
@click.option("--factor", "-f", "factors", multiple=True, required=True,
              help="path=levels, e.g. hatch.infill.hatch_distance_mm=0.09,0.11,0.13 or =0.09:0.13:0.02")
@click.option("--name", "template", default="{base}_DOE{i}", show_default=True,
              help="Name template; use {base}, {i}, a factor path or its last segment.")
@click.option("--dry-run", is_flag=True, help="Print the variants and check their names without saving.")
@click.option("--max-variants", type=click.IntRange(min=1), default=DEFAULT_MAX_VARIANTS, show_default=True,
              help="Refuse designs with more variants than this.")
@click.pass_context
def doe(ctx: click.Context, base: str, factors: tuple[str, ...], template: str, dry_run: bool,
        max_variants: int) -> None:
    """Generate full-factorial variants of a set.

    Nothing is saved unless every variant name is free.
    """
    lib = _lib(ctx)
    es = _get(ctx, base)

    def fail(exc: Exception) -> None:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc).splitlines()[0]
        _fail(f"{msg} (see --max-variants)" if "limit of" in msg else msg)

    pairs = []
    for f in factors:
        path, sep, levels = f.partition("=")
        if not sep:
            _fail(f"expected path=levels, got {f!r}")
        path = path.strip()
        try:
            pairs.append((path, parse_values(levels, get_path(es, path), max_levels=max_variants)))
        except (KeyError, ValueError) as exc:
            fail(exc)
    try:
        parsed = collect_factors(pairs)  # what is generated, counted and (dry run) listed
        variants = generate(es, parsed, template, max_variants=max_variants)
    except (KeyError, ValueError) as exc:
        fail(exc)
    click.echo(f"DOE: {count_text(parsed)}")
    if dry_run:  # the levels exactly as parsed and combined, so a misread list shows up here
        for line in level_lines(parsed):
            click.echo("  " + line)
    problems = lib.check_new_names([v.name for v in variants])
    if problems:
        verb = "would be" if dry_run else "was"
        _fail(f"nothing {verb} saved; {len(problems)} variant name(s) cannot be used:\n  " + "\n  ".join(problems))
    if dry_run:
        for v in variants:
            click.echo("would create " + v.name)
        return
    with batch_source(es.name):
        result = save_batch(lib, variants, on_saved=lambda name: click.echo("created " + name))
    if not result.complete:
        click.secho(f"error: {result.report()}", fg="red", err=True)
        sys.exit(130 if result.interrupted else 1)  # 130: the usual exit status after Ctrl+C


@main.command()
@click.option("--area", type=float, required=True, help="Cross-section area per layer [mm²].")
@click.option("--perimeter", type=float, required=True, help="Contour length per layer [mm] (support line length for support sets).")
@click.option("--segment", "-s", "segments", multiple=True, required=True,
              help="z_from:z_to:SET_NAME, repeat for a z-segmented part.")
@click.pass_context
def estimate(ctx: click.Context, area: float, perimeter: float, segments: tuple[str, ...]) -> None:
    """Rough build time of a prismatic block, optionally z-segmented with different sets."""
    lib = _lib(ctx)
    plan = []
    for seg in segments:
        try:
            z0, z1, name = seg.split(":", 2)
            plan.append((float(z0), float(z1), _get(ctx, name)))
        except ValueError:
            _fail(f"expected z_from:z_to:SET_NAME, got {seg!r}")
    try:
        results = estimate_plan(plan, area, perimeter, lib.load_process().recoat_time_s)
    except ValueError as exc:
        _fail(str(exc))
    total = 0.0
    for r in results:
        parts = ", ".join(f"{k} {fmt_duration(v)}" for k, v in r.seconds.items())
        click.echo(f"z {r.z_from_mm:g}–{r.z_to_mm:g} mm  {r.set_name:26} {r.layers:5d} layers  {fmt_duration(r.total_s):>12}  ({parts})")
        total += r.total_s
    click.secho(f"Total ≈ {fmt_duration(total)} (comparison estimate; jumps and delays not modelled)", bold=True)


@main.command("export")
@click.argument("name")
@click.argument("output", type=click.Path(dir_okay=False))
@click.pass_context
def export_cmd(ctx: click.Context, name: str, output: str) -> None:
    """Export a set to .json (re-importable) or .csv (parameter table)."""
    es = _get(ctx, name)
    out = Path(output)
    if out.suffix.lower() == ".csv":
        write_csv(es, out)
    else:
        export_json(es, out)
    click.echo(f"wrote {out}")


@main.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--overwrite", is_flag=True)
@click.pass_context
def import_cmd(ctx: click.Context, file: str, overwrite: bool) -> None:
    """Import a set from a JSON file."""
    try:
        es = _lib(ctx).import_file(file, overwrite=overwrite)
    except LibraryError as exc:
        _fail(str(exc))
    click.echo(f"imported {es.name}")


@main.command()
@click.pass_context
def gui(ctx: click.Context) -> None:
    """Open the desktop editor."""
    from .gui.app import run

    sys.exit(run(_lib(ctx).root))


def write_csv(es, out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "label", "value", "unit", "guard"])
        for path, value in flatten(es).items():
            meta = field_meta(es, path)
            v = value.value if hasattr(value, "value") else value
            w.writerow([path, meta.get("label", ""), v, meta.get("unit", ""), meta.get("guard", "")])


def _get(ctx: click.Context, name: str):
    try:
        return _lib(ctx).get(name)
    except LibraryError as exc:
        _fail(str(exc))


if __name__ == "__main__":
    main()
