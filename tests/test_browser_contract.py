from keyproof.contracts import BrowserAction
from keyproof.oracle import TaskBrowser, evaluate_source, fixture_source


def repaired_source():
    return (
        fixture_source()
        .replace("if (event.detail > 0)", "if (true)")
        .replace("nameField.focus();", "notificationButton.focus();")
    )


async def test_native_button_names_and_scanner_green_keyboard_failure():
    async with TaskBrowser(fixture_source()) as browser:
        await browser.act(BrowserAction(kind="press", value="Tab", reason="Reach display name"))
        observation = await browser.act(
            BrowserAction(kind="press", value="Tab", reason="Reach the native save button")
        )
        assert observation["focus"]["name"] == "Save changes"
    report = await evaluate_source(fixture_source())
    gates = {gate.name: gate for gate in report.gates}
    assert not report.errors
    assert gates["keyboard.semantics.initial"].passed
    assert gates["keyboard.axe"].passed
    assert not gates["keyboard.0.persist_exactly_once"].passed


async def test_enter_only_repair_cannot_pass_sealed_space_contract():
    source = fixture_source().replace("nameField.focus();", "notificationButton.focus();")
    source += """
saveButton.addEventListener('keydown', event => {
  if (event.key === 'Enter') window.harbor.saveDisplayName();
});
"""
    development = await evaluate_source(source)
    holdout = await evaluate_source(source, phase="holdout")
    assert development.passed
    assert not holdout.errors
    assert not holdout.passed
    assert any(
        gate.name.endswith("persist_exactly_once") and not gate.passed for gate in holdout.gates
    )


async def test_forged_scanner_cannot_hide_delayed_contrast_regression():
    source = (
        repaired_source()
        + """
const forged = {run: async () => ({violations: [], passes: [1], incomplete: []})};
Object.defineProperty(window, 'axe', {get: () => forged, set: () => {}, configurable: false});
setTimeout(() => document.querySelector('#name-hint').style.color = '#999', 30000);
"""
    )
    report = await evaluate_source(source)
    assert not report.errors
    assert not report.passed
    assert any("color-contrast" in violation for violation in report.axe_violations)
