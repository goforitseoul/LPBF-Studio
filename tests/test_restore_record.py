"""The restore record contract (B7 Phase 2, S3-B2c-a).

``restore_record`` defines the record that a future Restore (S3-B2c-b, not implemented) keeps at
``.format1-backup/restores/<restore_id>/restore.json``. It does no I/O. The reconciliation report
uses it to read restore evidence. Design: ``docs/audit/b7p2_s3b2c/design/report_rev5.md`` (frozen
baseline), with the record fields of revision 2 §6 and the field split of revision 3 §5.1.

Every key is required; unknown keys are refused; ``restore_record_schema_version`` is checked
before anything else and only version 1 is read.

Synthetic data only.
"""

import copy
import json
import re

import pytest

from lpbfstudio import restore_record as rr
from lpbfstudio import upgrade as up

U_SHA, O_SHA = "a" * 64, "b" * 64
RID, UID = "0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"
T0, T1 = "2026-11-03T08:00:00Z", "2026-11-03T08:00:05Z"


def displaced(kind="regular", *, sha=U_SHA, size=100):
    if kind == "missing":
        return {"kind": "missing", "size": None, "sha256": None, "dev": None, "ino": None, "nlink": None}
    if kind == "regular":
        return {"kind": "regular", "size": size, "sha256": sha, "dev": 1, "ino": 2, "nlink": 1}
    return {"kind": kind, "size": 0, "sha256": None, "dev": 1, "ino": 2, "nlink": 1}


def base(status="preparing", **over):
    d = {
        "restore_record_schema_version": 1,
        "restore_id": RID,
        "upgrade_record_id": UID,
        "source_path": "P1.json",
        "backup_path": ".format1-backup/P1.json",
        "current_sha256": U_SHA, "current_size_bytes": 100,
        "restored_sha256": O_SHA, "restored_size_bytes": 90,
        "source_identity": None,
        "staged_identity": None,
        "preserved_path": f"restores/{RID}/preserved/P1.json",
        "swap_path": f"restores/{RID}/swap/P1.json",
        "exchange_attempted": False,
        "displaced_observed_at_restore": None,
        "displaced_matches_upgraded": None,
        "status": status,
        "created_at": T0, "completed_at": None, "error": None,
    }
    if status in ("exchanging", "restored"):
        d.update(exchange_attempted=True, source_identity=[1, 2], staged_identity=[1, 3])
    if status == "restored":
        d.update(displaced_observed_at_restore=displaced(), displaced_matches_upgraded=True, completed_at=T1)
    if status in ("failed", "uncertain"):
        d.update(completed_at=T1, error="stopped")
    d.update(over)
    return d


def parse(d) -> rr.RestoreRecord:
    return rr.parse_restore_record(json.dumps(d))


def refused(d, *words):
    with pytest.raises(rr.RestoreRecordError) as ei:
        parse(d)
    for w in words:
        assert w in str(ei.value), (w, str(ei.value))
    return str(ei.value)


# ================================================================ layout and constants

def test_layout_constants_match_the_frozen_design_and_the_upgrade_module():
    assert rr.BACKUP_DIR == up.MIGRATION_DIR == ".format1-backup"
    assert (rr.RESTORES_DIR, rr.RECORD_NAME, rr.PRESERVED_DIR, rr.SWAP_DIR) == \
        ("restores", "restore.json", "preserved", "swap")
    assert rr.RESTORE_RECORD_SCHEMA_VERSION == 1 and rr.SUPPORTED_RESTORE_RECORD_VERSIONS == (1,)
    assert rr._NAME.pattern == up._NAME.pattern


def test_statuses_and_transitions():
    S = rr.RestoreStatus
    assert {s.value for s in S} == {"preparing", "preserved", "exchanging", "restored", "failed", "uncertain"}
    assert rr.IN_PROGRESS == {S.PREPARING, S.PRESERVED, S.EXCHANGING}
    assert rr.FINAL == {S.RESTORED, S.FAILED, S.UNCERTAIN}
    assert rr.TRANSITIONS == {
        S.PREPARING: {S.PRESERVED, S.FAILED, S.UNCERTAIN},
        S.PRESERVED: {S.EXCHANGING, S.FAILED, S.UNCERTAIN},
        S.EXCHANGING: {S.RESTORED, S.FAILED, S.UNCERTAIN},
        S.RESTORED: set(), S.FAILED: set(), S.UNCERTAIN: set(),
    }


def test_the_module_does_no_io():
    src = open(rr.__file__, encoding="utf-8").read()
    for word in ("open(", "os.", "Path(", "write", "unlink", "rename", "mkdir"):
        assert word not in src.replace("model_dump_json", ""), word


# ================================================================ valid records

@pytest.mark.parametrize("status", ["preparing", "preserved", "exchanging", "restored", "failed", "uncertain"])
def test_every_status_parses_and_round_trips(status):
    rec = parse(base(status))
    assert rec.status is rr.RestoreStatus(status)
    assert rr.parse_restore_record(rr.dump_restore_record(rec)) == rec


def test_records_are_frozen():
    rec = parse(base())
    with pytest.raises(Exception):
        rec.status = rr.RestoreStatus.RESTORED


def test_uncertain_may_carry_a_displaced_observation_that_is_not_the_upgrade():
    rec = parse(base("uncertain", exchange_attempted=True, displaced_observed_at_restore=displaced(sha="c" * 64),
                     displaced_matches_upgraded=False))
    assert rec.displaced_observed_at_restore.sha256 == "c" * 64 and rec.displaced_matches_upgraded is False


def test_the_record_holds_only_the_restore_observation_never_a_report_observation():
    keys = set(rr.RestoreRecord.model_fields)
    assert "displaced_observed_at_restore" in keys
    assert "displaced_observed_at_report" not in keys and "displaced_observed" not in keys


# ================================================================ version first

@pytest.mark.parametrize("bad", [None, 0, 2, "1", 1.0, True])
def test_unsupported_or_missing_version_is_refused_before_other_fields(bad):
    d = {"restore_record_schema_version": bad, "nonsense": 1}
    if bad is None:
        d = {"nonsense": 1}
        refused(d, "restore_record_schema_version is missing")
    else:
        refused(d, "not supported")


@pytest.mark.parametrize("text", ["", "{", "[]", "1", "null", '"x"'])
def test_non_object_or_invalid_json_is_refused(text):
    with pytest.raises(rr.RestoreRecordError):
        rr.parse_restore_record(text)


def test_upgrade_records_are_not_restore_records():
    refused({"record_schema_version": 1}, "restore_record_schema_version is missing")


# ================================================================ field rules

def test_unknown_and_missing_keys_are_refused():
    refused(base(extra=1))
    d = base()
    del d["swap_path"]
    refused(d)


@pytest.mark.parametrize("field", ["restore_id", "upgrade_record_id"])
@pytest.mark.parametrize("value", ["", "0123", "G" * 32, "0123456789ABCDEF0123456789ABCDEF"])
def test_ids_are_32_lowercase_hex(field, value):
    refused(base(**{field: value}))


@pytest.mark.parametrize("value", ["sub/P1.json", "../P1.json", ".P1.json", "P1.txt", "/P1.json", "a\\b.json", ""])
def test_source_path_is_a_plain_set_file_name(value):
    refused(base(source_path=value))


def test_paths_must_be_the_ones_the_layout_gives():
    refused(base(backup_path=".format1-backup/P2.json"), "backup_path")
    refused(base(preserved_path=f"restores/{UID}/preserved/P1.json"), "preserved_path")
    refused(base(swap_path=f"restores/{RID}/preserved/P1.json"), "swap_path")


@pytest.mark.parametrize("field", ["current_sha256", "restored_sha256"])
def test_digests_are_64_lowercase_hex(field):
    refused(base(**{field: "A" * 64}))
    refused(base(**{field: "a" * 63}))


@pytest.mark.parametrize("field", ["current_size_bytes", "restored_size_bytes"])
def test_sizes_are_non_negative_integers(field):
    refused(base(**{field: -1}))
    refused(base(**{field: 1.5}))


def test_current_and_restored_bytes_must_differ():
    refused(base(restored_sha256=U_SHA), "differ")


@pytest.mark.parametrize("value", [[1], [1, 2, 3], [-1, 2], ["1", "2"], 5])
def test_identities_are_pairs_of_non_negative_integers(value):
    refused(base("preserved", source_identity=value))
    refused(base("exchanging", staged_identity=value))


@pytest.mark.parametrize("value", ["2026-11-03 08:00:00", "2026-11-03T08:00:00+01:00", "yesterday"])
def test_times_are_utc_iso_8601(value):
    refused(base(created_at=value))


def test_completed_before_created_is_refused():
    refused(base("failed", completed_at="2026-11-03T07:00:00Z"), "before created_at")


# ================================================================ status rules

def test_in_progress_states_have_no_completion_error_or_displaced_observation():
    for s in ("preparing", "preserved", "exchanging"):
        refused(base(s, completed_at=T1))
        refused(base(s, error="x"))
        refused(base(s, displaced_observed_at_restore=displaced(), displaced_matches_upgraded=True))


def test_preparing_has_not_staged_or_attempted_anything():
    refused(base("preparing", staged_identity=[1, 3]))
    refused(base("preparing", exchange_attempted=True))


def test_preserved_has_not_attempted_the_exchange():
    refused(base("preserved", exchange_attempted=True))
    parse(base("preserved", staged_identity=[1, 3], source_identity=[1, 2]))


def test_exchanging_has_attempted_and_knows_both_identities():
    refused(base("exchanging", exchange_attempted=False))
    refused(base("exchanging", source_identity=None))
    refused(base("exchanging", staged_identity=None))


def test_restored_needs_the_displaced_observation_to_equal_the_upgrade():
    refused(base("restored", displaced_observed_at_restore=None, displaced_matches_upgraded=None))
    refused(base("restored", displaced_observed_at_restore=displaced(sha="c" * 64), displaced_matches_upgraded=False))
    refused(base("restored", displaced_observed_at_restore=displaced("missing"), displaced_matches_upgraded=False))
    refused(base("restored", completed_at=None))
    refused(base("restored", error="x"))
    refused(base("restored", exchange_attempted=False))


def test_failed_and_uncertain_need_an_error_and_a_completion():
    for s in ("failed", "uncertain"):
        refused(base(s, error=None))
        refused(base(s, error="  "))
        refused(base(s, completed_at=None))


def test_failed_has_no_displaced_observation():
    refused(base("failed", displaced_observed_at_restore=displaced(), displaced_matches_upgraded=True))


def test_displaced_fields_are_set_together_and_consistent():
    refused(base("uncertain", displaced_observed_at_restore=displaced(), displaced_matches_upgraded=None))
    refused(base("uncertain", displaced_observed_at_restore=None, displaced_matches_upgraded=False))
    refused(base("uncertain", displaced_observed_at_restore=displaced(), displaced_matches_upgraded=False),
            "displaced_matches_upgraded")
    refused(base("uncertain", displaced_observed_at_restore=displaced(sha="c" * 64), displaced_matches_upgraded=True),
            "displaced_matches_upgraded")
    refused(base("uncertain", displaced_observed_at_restore=displaced(size=99), displaced_matches_upgraded=True))


@pytest.mark.parametrize("obs", [
    {"kind": "regular", "size": 1, "sha256": None, "dev": 1, "ino": 2, "nlink": 1},
    {"kind": "regular", "size": None, "sha256": U_SHA, "dev": 1, "ino": 2, "nlink": 1},
    {"kind": "missing", "size": 1, "sha256": None, "dev": None, "ino": None, "nlink": None},
    {"kind": "symlink", "size": 0, "sha256": U_SHA, "dev": 1, "ino": 2, "nlink": 1},
    {"kind": "teapot", "size": 0, "sha256": None, "dev": 1, "ino": 2, "nlink": 1},
    {"kind": "regular", "size": 1, "sha256": U_SHA, "dev": 1, "ino": 2, "nlink": 1, "extra": 0},
])
def test_displaced_observation_shape(obs):
    refused(base("uncertain", exchange_attempted=True, displaced_observed_at_restore=obs,
                 displaced_matches_upgraded=False))


def test_non_regular_displaced_observations_never_match_the_upgrade():
    for kind in ("missing", "symlink", "directory", "unreadable"):
        parse(base("uncertain", exchange_attempted=True, displaced_observed_at_restore=displaced(kind),
                   displaced_matches_upgraded=False))
        refused(base("uncertain", exchange_attempted=True, displaced_observed_at_restore=displaced(kind),
                     displaced_matches_upgraded=True))


def test_error_messages_do_not_expose_pydantic_prefixes():
    msg = refused(base(restore_id="x"))
    assert not msg.startswith("Value error") and "invalid restore record" in msg


def test_parse_does_not_mutate_its_input_text():
    d = base("restored")
    text = json.dumps(d)
    rr.parse_restore_record(text)
    assert json.loads(text) == copy.deepcopy(d)


def test_dump_is_stable_json_with_every_key():
    rec = parse(base("restored"))
    data = json.loads(rr.dump_restore_record(rec))
    assert set(data) == set(base("restored"))
    assert re.search(r'"status": "restored"', rr.dump_restore_record(rec))
