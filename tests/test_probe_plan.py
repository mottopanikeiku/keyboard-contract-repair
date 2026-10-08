"""The model-proposed probe language is bounded before any browser runs it."""

import pytest
from pydantic import ValidationError

from keyproof.learning_contracts import ProbePlan, ProbeStep, probe_digest
from keyproof.probes import _expected_names


def plan(*steps: tuple[str, str], name: str = "probe", hypothesis: str = "h") -> ProbePlan:
    return ProbePlan(
        name=name,
        hypothesis=hypothesis,
        steps=[ProbeStep(kind=kind, value=value) for kind, value in steps],
    )


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("edit", ""),
        ("edit", "Line\nbreak"),
        ("edit", "Del\x7f"),
        ("save", "Escape"),
        ("save", "Tab"),
        ("wait", ""),
        ("wait", "-1"),
        ("wait", "10001"),
        ("wait", "１２"),  # Full-width digits pass str.isdecimal but are not ASCII.
    ],
)
def test_invalid_steps_are_rejected(kind, value):
    with pytest.raises(ValidationError):
        ProbeStep(kind=kind, value=value)


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([("save", "Enter"), ("edit", "Jordan Lee")], id="starts-with-save"),
        pytest.param([("edit", "Jordan Lee"), ("wait", "500")], id="never-saves"),
        pytest.param(
            [("edit", "A"), ("wait", "10000"), ("wait", "10000"), ("wait", "1"), ("save", "Enter")],
            id="waits-over-20s",
        ),
    ],
)
def test_unexecutable_plans_are_rejected(steps):
    with pytest.raises(ValidationError):
        plan(*steps)


def test_expected_names_follow_the_value_at_each_activation():
    probe = plan(
        ("edit", "Jordan Lee"),
        ("save", "Enter"),
        ("save", "Space"),
        ("edit", "Taylor Reed"),
        ("wait", "250"),
        ("save", "Enter"),
        ("edit", "Unsaved Draft"),
    )
    assert _expected_names(probe) == ["Jordan Lee", "Jordan Lee", "Taylor Reed"]


def test_probe_digest_binds_steps_not_labels():
    steps = (("edit", "Jordan Lee"), ("save", "Enter"))
    assert probe_digest(plan(*steps, name="a", hypothesis="x")) == probe_digest(
        plan(*steps, name="b", hypothesis="y")
    )
    assert probe_digest(plan(*steps)) != probe_digest(
        plan(("edit", "Jordan Lee"), ("save", "Space"))
    )
