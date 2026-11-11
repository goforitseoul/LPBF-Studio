# User guide

LPBF Studio edits exposure sets: the parameters a laser powder bed fusion machine applies to each region of a part. It checks them against a machine profile and helps compare and vary them. It does not slice geometry, generate scan vectors, write machine job files or control a machine.

## Install and start

You need Python 3.10 or newer. From a clone of the repository:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[gui]"            # add ",dev" for the tests
lpbf-studio                        # desktop editor
lpbf --help                        # command line
```

Supported: Linux on local disks, and Windows. macOS is not tested. Network drives, FUSE mounts and synchronised folders are not supported.

The library lives in `~/.lpbfstudio/library`. Choose another folder with `--library DIR` or `LPBF_LIBRARY=DIR`. A new library gets three locked example sets and a generic machine profile. Their values are illustrative only.

## Exposure sets

A set has a layer thickness and up to four exposure types. Parts use Hatch, Core, Contour and Edge; supports use Support.

| Task | Editor | Command line |
|---|---|---|
| List sets | Library panel | `lpbf list` |
| New set | New part set / New support set | `lpbf new NAME` |
| Copy, including a locked reference set | Copy | `lpbf copy SOURCE NEW` |
| Rename | Rename (the name field is read-only) | `lpbf rename OLD NEW` |
| Delete | Delete | `lpbf delete NAME` |
| Change parameters | Select a node in the tree and edit the form, then Save; Revert discards | `lpbf set NAME path=value …` |
| Show every parameter path | — | `lpbf show NAME` |
| Import, export | Import…, Export… (JSON, or a CSV table) | `lpbf import FILE`, `lpbf export NAME FILE` |

Locked sets cannot be edited, renamed or deleted; copy one to start your own. When you switch sets, create or copy a set, or close the window with unsaved changes, the editor asks whether to Save, Discard or Cancel.

Each form field shows its unit and allowed range. Fields that need care carry a coloured dot **and** their guard level in words: *caution* (better know what it does) or *critical* (you have to know what it does).

A file that cannot be read, or that has a format this build does not know, is listed as skipped and never changed. Formats 1 and 2 are described in [file_format.md](file_format.md).

## Checks, energy and offsets

The tabs under the form show the selected set:

- **Energy input**: VED for hatch and core regions, areal energy for contours, edges and supports. See [calculations.md](calculations.md).
- **Checks**: rule findings with stable codes ([validation_rules.md](validation_rules.md)). Selecting a finding selects the tree node it is about and puts the cursor in its field. A finding about the whole set, the machine profile or the process settings, or about a setting the chosen pattern does not use, is explained in the status bar instead.
- **Offsets**: each scan line's total offset from the nominal contour.

`lpbf validate [NAMES]` prints the same findings and exits 1 if there are errors.

## Machine profiles and process settings

A set names its machine profile in its Machine profile field. The checks use that profile's power, speed, layer and energy-window limits, and an unknown or blank name is reported, never replaced by a default. Edit the profile with Machine profile…; on the command line use `lpbf machine list`, `check`, `import` and `export`. Process settings, such as the global beam offset and the recoat time, are edited with Process settings….

## DOE, compare, estimate, calculators

- **DOE…** / `lpbf doe`: full-factorial variants of the current set. The variant count is shown first; above 500 variants is refused unless you raise the limit. Saving is not transactional: if it stops part-way, the variants already saved stay and the report lists each one ([doe_audit.md](doe_audit.md)).
- **Compare…** / `lpbf compare A B`: parameters that differ between two sets.
- **Build-time estimate…** / `lpbf estimate`: a comparison estimate for a prismatic block, optionally in z segments. Jumps and delays are not modelled.
- **Calculators…**: skin ridge for an overhang angle, and the power-reduction curve.

## History

Every save, copy, rename, delete and import also keeps an unchangeable copy of the file in `<library>/.history/`. See [history.md](history.md). In this build the editor and the command line do not yet show whether a save was recorded. History grows without limit, and saves take longer as it grows. Build job manifests exist only as a programming interface, with no command or menu entry.

## Upgrade and recovery

Format 1 files can be upgraded to format 2, and an interrupted upgrade can be reconciled or restored. These are programming interfaces on Linux, with no command or editor entry; they are refused on Windows. See [file_format.md](file_format.md), [reconciliation.md](reconciliation.md) and [restore.md](restore.md).

## Running the tests

```bash
pip install -e ".[gui,dev]"
pytest
LPBF_REQUIRE_GUI_TESTS=1 pytest    # fail, rather than skip, if Qt cannot load
```

On Windows the POSIX lock, upgrade, reconcile and restore tests are skipped by design.

## Output encoding

On Windows, when the command line's output goes to a file or a pipe, it is written as UTF-8, whatever the system code page (for example GBK). Read such files as UTF-8. A console shows the text directly. To choose another encoding, set `PYTHONIOENCODING`; LPBF Studio then uses that. On Linux and macOS the output follows the system's locale, as before.

## Reporting problems

Open an issue at https://github.com/goforitseoul/LPBF-Studio/issues. Include the command or the steps, the output, your operating system and your Python version.
