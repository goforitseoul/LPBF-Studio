"""DC-001 §7.2, §7.3 and §7.11: the outcome model and the caller-scoped collector."""

import contextvars
import itertools
import threading

import pytest

from lpbfstudio import outcomes as oc
from lpbfstudio.outcomes import AdoptionStatus as A
from lpbfstudio.outcomes import CanonicalReason as CR
from lpbfstudio.outcomes import CanonicalStatus as C
from lpbfstudio.outcomes import HistoryReason as HR
from lpbfstudio.outcomes import HistoryStatus as H
from lpbfstudio.outcomes import InvalidOutcome, RebaselineOutcome, RebaselineStatus, WriteOutcome
from lpbfstudio.outcomes import Readback as R


def make(canonical=C.DONE, readback=R.READ, history=H.RECORDED, **kw):
    base = dict(operation="save", path="sets/a.json", previous_path=None, canonical=canonical, canonical_reason=None,
                canonical_error=None, readback=readback, readback_errno=None, observed_hash=None,
                previous_path_readback=None, history=history, history_reason=None, history_errno=None,
                object_id="a" * 32, seq="00000002", adoption=A.NOT_NEEDED, adopted_seq=None)
    if readback is R.READ:
        base["observed_hash"] = "9" * 64
    if readback is R.FAILED:
        base["readback_errno"] = 5
    if canonical is not C.DONE:
        base["canonical_reason"] = CR.CANONICAL_STEP_FAILED
        base["canonical_error"] = "boom"
    if readback is R.NOT_PERFORMED:
        base.update(canonical_reason=CR.PRECONDITION_REFUSED, object_id=None, seq=None, observed_hash=None)
    if history in (H.INCOMPLETE, H.NOT_RECORDED):
        base["history_reason"] = HR.BLOB_WRITE_FAILED if history is H.INCOMPLETE else HR.STAGING_WRITE_FAILED
    if history is H.NOT_RECORDED:
        base["seq"] = None
    base.update(kw)
    return WriteOutcome(**base)


# the table in §7.2, with the two gaps that §7.2's determination rules imply: an uncertain read-back of ABSENT
VALID = {
    (C.DONE, R.READ, H.RECORDED), (C.DONE, R.ABSENT, H.RECORDED),
    (C.DONE, R.READ, H.RECORDED_NOT_CURRENT), (C.DONE, R.ABSENT, H.RECORDED_NOT_CURRENT),
    (C.DONE, R.FAILED, H.RECORDED_UNVERIFIED),
    *((C.DONE, r, h) for r in (R.READ, R.ABSENT, R.FAILED) for h in (H.INCOMPLETE, H.NOT_RECORDED)),
    *((C.NOT_DONE, r, h) for r in (R.READ, R.ABSENT) for h in (H.RECORDED_NOT_CURRENT, H.INCOMPLETE, H.NOT_RECORDED)),
    (C.NOT_DONE, R.NOT_PERFORMED, H.NOT_APPLICABLE),
    (C.UNCERTAIN, R.READ, H.RECORDED_NOT_CURRENT), (C.UNCERTAIN, R.ABSENT, H.RECORDED_NOT_CURRENT),
    (C.UNCERTAIN, R.READ, H.RECORDED), (C.UNCERTAIN, R.ABSENT, H.RECORDED),
    (C.UNCERTAIN, R.FAILED, H.RECORDED_UNVERIFIED),
    *((C.UNCERTAIN, r, h) for r in (R.READ, R.ABSENT, R.FAILED) for h in (H.INCOMPLETE, H.NOT_RECORDED)),
}


@pytest.mark.parametrize("combo", list(itertools.product(C, R, H)), ids=lambda c: "-".join(x.value for x in c))
def test_every_combination_is_buildable_only_if_the_table_lists_it(combo):
    c, r, h = combo
    if combo in VALID:
        assert make(c, r, h).history is h
    else:
        with pytest.raises(InvalidOutcome):
            make(c, r, h)


def test_complete_is_true_only_for_the_first_row():
    assert make().complete
    assert make(adoption=A.COMMITTED, adopted_seq="00000001").complete
    assert not make(history_reason=HR.RECORD_DURABILITY_FAILED, history_errno=5).complete
    assert not make(C.DONE, R.FAILED, H.RECORDED_UNVERIFIED).complete
    assert not make(C.UNCERTAIN, R.READ, H.RECORDED).complete
    assert not make(C.DONE, R.READ, H.NOT_RECORDED, seq=None).complete


def test_history_committed():
    assert make().history_committed
    assert make(C.DONE, R.READ, H.RECORDED_NOT_CURRENT).history_committed
    assert make(C.DONE, R.FAILED, H.RECORDED_UNVERIFIED).history_committed
    assert not make(C.DONE, R.READ, H.INCOMPLETE).history_committed
    assert make(C.DONE, R.READ, H.NOT_RECORDED, seq=None, adoption=A.COMMITTED_SOURCE_CHANGED, adopted_seq="00000001",
                history_reason=HR.ADOPTION_SOURCE_CHANGED).history_committed


@pytest.mark.parametrize("status,good", [(A.COMMITTED, True), (A.COMMITTED_SOURCE_CHANGED, True),
                                         (A.COMMITTED_UNVERIFIED, True), (A.NOT_NEEDED, False), (A.INCOMPLETE, False),
                                         (A.NOT_RECORDED, False)])
def test_i5_adopted_seq_exactly_when_committed(status, good):
    reasons = {A.COMMITTED_SOURCE_CHANGED: HR.ADOPTION_SOURCE_CHANGED, A.COMMITTED_UNVERIFIED: HR.ADOPTION_RECHECK_FAILED,
               A.INCOMPLETE: HR.BLOB_WRITE_FAILED, A.NOT_RECORDED: HR.SOURCE_READ_FAILED}
    hist = H.NOT_RECORDED if status in reasons else H.RECORDED
    kw = dict(seq=None, history_reason=reasons[status]) if status in reasons else {}
    make(C.DONE, R.READ, hist, adoption=status, adopted_seq="00000001" if good else None, **kw)
    with pytest.raises(InvalidOutcome):
        make(C.DONE, R.READ, hist, adoption=status, adopted_seq=None if good else "00000001", **kw)


def test_i6_seq_is_required_for_every_recorded_status():
    for r, h in ((R.READ, H.RECORDED), (R.READ, H.RECORDED_NOT_CURRENT), (R.FAILED, H.RECORDED_UNVERIFIED)):
        with pytest.raises(InvalidOutcome):
            make(C.DONE, r, h, seq=None)


def test_i4_not_applicable_needs_null_ids():
    make(C.NOT_DONE, R.NOT_PERFORMED, H.NOT_APPLICABLE)
    for bad in ({"seq": "00000001"}, {"object_id": "a" * 32}, {"adoption": A.COMMITTED, "adopted_seq": "00000001"}):
        with pytest.raises(InvalidOutcome):
            make(C.NOT_DONE, R.NOT_PERFORMED, H.NOT_APPLICABLE, **bad)


def test_adoption_that_stopped_h_requires_not_recorded_and_its_reason():
    with pytest.raises(InvalidOutcome):  # A-8 must be not_recorded
        make(C.DONE, R.READ, H.RECORDED, adoption=A.COMMITTED_SOURCE_CHANGED, adopted_seq="00000001")
    with pytest.raises(InvalidOutcome):  # and with its reason
        make(C.DONE, R.READ, H.NOT_RECORDED, seq=None, adoption=A.COMMITTED_SOURCE_CHANGED, adopted_seq="00000001",
             history_reason=HR.BLOB_WRITE_FAILED)
    with pytest.raises(InvalidOutcome):
        make(C.DONE, R.READ, H.NOT_RECORDED, seq=None, adoption=A.COMMITTED_UNVERIFIED, adopted_seq="00000001",
             history_reason=HR.ADOPTION_SOURCE_CHANGED)


def test_field_rules():
    with pytest.raises(InvalidOutcome):
        make(observed_hash=None)  # READ needs the hash
    with pytest.raises(InvalidOutcome):
        make(C.DONE, R.ABSENT, H.RECORDED, observed_hash="9" * 64)
    with pytest.raises(InvalidOutcome):
        make(C.DONE, R.FAILED, H.RECORDED_UNVERIFIED, readback_errno=None)
    with pytest.raises(InvalidOutcome):
        make(previous_path_readback=R.READ)  # rename only
    with pytest.raises(InvalidOutcome):
        make(operation="nonsense")
    with pytest.raises(InvalidOutcome):
        make(canonical_reason=CR.CANONICAL_STEP_FAILED)  # DONE has none
    with pytest.raises(InvalidOutcome):
        make(C.UNCERTAIN, R.READ, H.RECORDED, canonical_reason=None)
    with pytest.raises(InvalidOutcome):
        make(C.DONE, R.READ, H.NOT_RECORDED, seq=None, history_reason=None)  # a gap always has its reason


def test_reason_vocabulary_is_exactly_the_contract():
    assert {r.value for r in HR} == {
        "history_root_unavailable", "state_unverifiable", "state_unknown", "state_changed_outside_history",
        "state_missing", "state_tombstoned_file_present", "import_overwrite_undefined", "source_read_failed",
        "object_reservation_collisions_exhausted", "object_reservation_failed", "identity_temp_write_failed",
        "identity_commit_failed", "revision_reservation_collisions_exhausted", "revision_reservation_failed",
        "sequence_exhausted", "staging_write_failed", "staging_readback_failed", "blob_write_failed", "blob_verify_failed",
        "record_temp_write_failed", "record_commit_failed", "record_durability_failed", "adoption_source_changed",
        "adoption_recheck_failed", "canonical_step_failed", "canonical_readback_failed", "canonical_mismatch"}
    assert {r.value for r in CR} == {"precondition_refused", "canonical_step_failed"}


def test_contract_examples_build():
    make(adoption=A.COMMITTED, adopted_seq="00000001", observed_hash="9" * 64)
    make(C.DONE, R.READ, H.NOT_RECORDED, seq=None, adoption=A.COMMITTED_SOURCE_CHANGED, adopted_seq="00000001",
         history_reason=HR.ADOPTION_SOURCE_CHANGED)
    make(C.NOT_DONE, R.READ, H.RECORDED_NOT_CURRENT, history_reason=HR.CANONICAL_STEP_FAILED, seq="00000005")
    make(C.UNCERTAIN, R.FAILED, H.RECORDED_UNVERIFIED, operation="delete", history_reason=HR.CANONICAL_READBACK_FAILED,
         history_errno=5, seq="00000006", readback_errno=5)


def test_outcome_is_frozen():
    out = make()
    with pytest.raises(Exception):
        out.seq = "00000009"


# --------------------------------------------------------------------------- rebaseline


def reb(status, **kw):
    base = dict(mode="upgrade", path="sets/a.json", object_id="a" * 32, seq="00000002", status=status, reason=None,
                errno=None, detail="")
    base.update(kw)
    return RebaselineOutcome(**base)


def test_rebaseline_complete_only_when_recorded_without_a_reason():
    assert reb(RebaselineStatus.RECORDED).complete
    assert not reb(RebaselineStatus.RECORDED, reason=HR.RECORD_DURABILITY_FAILED.value, errno=5).complete
    assert not reb(RebaselineStatus.RECORDED_NOT_CURRENT).complete
    assert not reb(RebaselineStatus.RECORDED_UNVERIFIED).complete
    assert not reb(RebaselineStatus.INCOMPLETE, reason="blob_write_failed").complete
    assert not reb(RebaselineStatus.REFUSED, seq=None, reason="rebaseline_state").complete


def test_rebaseline_external_mode_cannot_be_built():
    with pytest.raises(InvalidOutcome):
        reb(RebaselineStatus.REFUSED, mode="external", seq=None, reason="rebaseline_state")


def test_rebaseline_rules():
    with pytest.raises(InvalidOutcome):
        reb(RebaselineStatus.RECORDED, seq=None)
    with pytest.raises(InvalidOutcome):
        reb(RebaselineStatus.REFUSED, seq=None, reason=None)
    with pytest.raises(InvalidOutcome):
        reb(RebaselineStatus.REFUSED, seq="00000002", reason="rebaseline_state")
    with pytest.raises(InvalidOutcome):
        reb(RebaselineStatus.NOT_RECORDED, seq=None, reason=None)


# --------------------------------------------------------------------------- collector


def test_collector_keeps_order_and_resets():
    assert oc.deliver(make()) is False  # none open: nothing to keep it
    with oc.collect_outcomes() as sink:
        a, b = make(path="sets/a.json"), make(path="sets/b.json")
        assert oc.deliver(a) and oc.deliver(b)
        assert sink.outcomes == (a, b) and len(sink) == 2 and list(sink) == [a, b] and sink.last is b
    assert oc.deliver(make()) is False
    assert len(sink) == 2


def test_collector_resets_after_an_exception():
    with pytest.raises(ValueError):
        with oc.collect_outcomes() as sink:
            oc.deliver(make())
            raise ValueError("x")
    assert len(sink) == 1 and oc.deliver(make()) is False


def test_nested_collectors_both_receive():
    with oc.collect_outcomes() as outer:
        oc.deliver(make(path="sets/1.json"))
        with oc.collect_outcomes() as inner:
            oc.deliver(make(path="sets/2.json"))
        oc.deliver(make(path="sets/3.json"))
    assert [o.path for o in outer] == ["sets/1.json", "sets/2.json", "sets/3.json"]
    assert [o.path for o in inner] == ["sets/2.json"]


def test_a_new_plain_thread_does_not_reach_the_collector():
    seen = []
    with oc.collect_outcomes() as sink:
        t = threading.Thread(target=lambda: seen.append(oc.deliver(make())))
        t.start()
        t.join()
    assert seen == [False] and len(sink) == 0


def test_a_copied_context_run_in_another_thread_does_not_reach_the_collector():
    """Python 3.14 free-threaded builds let threads inherit the context; the sink is bound to its thread anyway."""
    seen = []
    with oc.collect_outcomes() as sink:
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: seen.append(ctx.run(oc.deliver, make())))
        t.start()
        t.join()
    assert seen == [False] and len(sink) == 0


def test_a_thread_can_open_its_own_collector():
    got = []

    def work():
        with oc.collect_outcomes() as s:
            oc.deliver(make())
        got.append(len(s))

    with oc.collect_outcomes() as sink:
        t = threading.Thread(target=work)
        t.start()
        t.join()
    assert got == [1] and len(sink) == 0
