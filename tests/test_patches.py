import pytest

from keyproof.contracts import EvaluationReport, GateResult, SourcePatch, TextEdit
from keyproof.controller import PatchRejected, acceptance, apply_patch


def patch(*edits: tuple[str, str]) -> SourcePatch:
    return SourcePatch(
        summary="Repair keyboard behavior",
        edits=[TextEdit(before=before, after=after) for before, after in edits],
    )


def report(**gates: bool) -> EvaluationReport:
    return EvaluationReport(
        phase="development",
        passed=all(gates.values()),
        gates=[GateResult(name=name, passed=passed) for name, passed in gates.items()],
        source_hash="isolated-test-source",
        elapsed_ms=0,
    )


def test_overlapping_occurrences_are_ambiguous():
    # str.count('aa') would incorrectly report one occurrence in 'aaa'.
    with pytest.raises(PatchRejected):
        apply_patch("aaa", patch(("aa", "fixed")))


def test_edits_match_original_not_prior_replacements():
    # A replacement may contain another edit's before string; it must not be edited again.
    assert (
        apply_patch("alpha; beta;", patch(("alpha", "beta"), ("beta", "gamma"))) == "beta; gamma;"
    )


def test_overlapping_edit_ranges_are_rejected():
    with pytest.raises(PatchRejected):
        apply_patch("abcdef", patch(("abcd", "fixed"), ("cdef", "also fixed")))


def test_utf8_candidate_limit_is_bytes_not_characters():
    with pytest.raises(PatchRejected):
        apply_patch("a" * 32000 + " unique", patch(("unique", "界" * 16000)))


def test_incremental_repair_preserves_prior_gates():
    before = report(save=False, focus=False, dismissal=True)
    accepted, _ = acceptance(before, report(save=True, focus=False, dismissal=True))
    assert accepted
    accepted, _ = acceptance(before, report(save=True, focus=True, dismissal=False))
    assert not accepted


def test_removed_gate_cannot_masquerade_as_improvement():
    before = report(save=False, focus=True)
    accepted, _ = acceptance(before, report(focus=True))
    assert not accepted


def test_holdout_never_drives_acceptance():
    candidate = report(save=True)
    candidate.phase = "holdout"
    accepted, _ = acceptance(report(save=False), candidate)
    assert not accepted
