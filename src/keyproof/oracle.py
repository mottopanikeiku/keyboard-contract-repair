"""Owned synthetic workspace and independent Chromium behavioral gates.

Only behavior.js is a repair surface. Persistence evidence lives in Python route
handlers, never in a page-controlled success label or JavaScript counter.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from keyproof.contracts import BrowserAction, EvaluationReport, GateResult


async def _close_preserving_error(
    close: Callable[[], Awaitable[None]], error: BaseException | None
) -> None:
    """A dead driver must not replace cancellation with a secondary close error."""
    try:
        await close()
    except Exception:
        if error is None:
            raise


FIXTURE_DIR = Path(__file__).parent / "fixtures"
ORIGIN = "http://keyproof.test"
INITIAL_NAME = "Alex Morgan"
TASK_SPEC = (
    "On Harbor / Workspace settings, change Display name and activate Save changes using "
    "standard keyboard navigation. A native button must work with both Enter and Space, "
    "and pointer activation must keep working. Every activation must persist the exact current "
    "display name once, including repeated saves and different values; editing alone must not "
    "save. Notification settings must open a modal with focus on Weekly email summary, "
    "keep Tab and Shift+Tab inside it, and close through Escape or Done, returning focus to "
    "the Notification settings opener. Preserve visible, enabled, correctly named native "
    "controls and accessibility. All storage here is synthetic and local to this browser case. "
    "Persistence is observed for 60 virtual seconds per activation, with a final 60-second quiet window; "
    "it does not claim to detect actions scheduled beyond that bounded horizon."
)
_ALLOWED_KEYS = frozenset(
    {
        "Tab",
        "Shift+Tab",
        "Enter",
        "Space",
        "Escape",
        "Backspace",
        "Delete",
        "Home",
        "End",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Control+A",
        "Meta+A",
    }
)
_MAX_SOURCE_BYTES = 64 * 1024
_QUIET_WINDOW_MS = 60_000
_CLOCK_START = datetime(2025, 1, 1, tzinfo=UTC)


def fixture_source() -> str:
    return (FIXTURE_DIR / "behavior.js").read_text(encoding="utf-8")


def _source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _check_source(source: str) -> None:
    if len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("Editable behavior source exceeds 64 KiB")


def render_fixture(source: str) -> str:
    """Internal evaluator shell; persistence is owned by the Python route ledger."""
    _check_source(source)
    persist = """const persist = async (displayName) => {
      const response = await fetch('/api/profile', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({display_name: displayName})
      });
      if (!response.ok) throw new Error('Synthetic persistence rejected the request');
      return response.json();
    };"""
    notice = "Owned demonstration workspace · Synthetic local storage only. No email or external transactions."
    host = """(() => {
      'use strict';
      __PERSIST__
      const api = Object.freeze({saveDisplayName: async () => {
        const field = document.getElementById('display-name');
        const saved = await persist(field.value);
        document.getElementById('save-status').textContent = 'Changes saved';
        return saved;
      }});
      Object.defineProperty(window, 'harbor', {value: api, writable: false, configurable: false});
    })();""".replace("__PERSIST__", persist)
    # Source is a JSON string, not HTML or an inline JavaScript body. The trusted
    # loader sets textContent, so </script>, HTML comments, and Unicode separators
    # in a candidate cannot escape the script element or manufacture host markup.
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    loader = (
        "(() => {const script = document.createElement('script');"
        f"script.textContent = new TextDecoder().decode(Uint8Array.from(atob('{encoded}'), c => c.charCodeAt(0)));"
        "document.body.appendChild(script);})();"
    )
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
        for s in (host, loader, source)
    ]
    script_policy = " ".join(hashes)
    csp = (
        "default-src 'none'; base-uri 'none'; object-src 'none'; frame-src 'none'; "
        "form-action 'none'; style-src 'unsafe-inline'; img-src data:; "
        f"connect-src {ORIGIN}; script-src {script_policy}"
    )
    shell = (FIXTURE_DIR / "app.html").read_text(encoding="utf-8")
    return (
        shell.replace(
            "<head>", '<head>\n  <meta http-equiv="Content-Security-Policy" content="' + csp + '">'
        )
        .replace("__KEYPROOF_NOTICE__", notice)
        .replace("__KEYPROOF_SCRIPTS__", f"<script>{host}</script>\n<script>{loader}</script>")
    )


async def _launch_chromium(playwright: Playwright) -> Browser:
    """Require an OS network namespace in addition to Chromium's own sandbox."""
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise RuntimeError(
            "bubblewrap is required for network-isolated Chromium; no fallback is permitted"
        )
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        raise RuntimeError("Playwright Chromium executable is missing")
    # Playwright passes the inspector pipes as FDs 3 and 4. The wrapper contains
    # only trusted executable paths; candidate source never enters a command.
    with tempfile.TemporaryDirectory(prefix="keyproof-launch-") as directory:
        wrapper = Path(directory) / "chromium"
        wrapper.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(bubblewrap)
            + " --die-with-parent --unshare-net --ro-bind / / --dev /dev"
            + " --proc /proc --tmpfs /tmp --clearenv"
            + " --setenv PATH /usr/local/bin:/usr/bin:/bin"
            + " --setenv HOME /tmp/keyproof-home"
            + " --setenv XDG_CONFIG_HOME /tmp/keyproof-config"
            + " --setenv XDG_CACHE_HOME /tmp/keyproof-cache -- "
            + shlex.quote(str(executable))
            + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        return await playwright.chromium.launch(
            executable_path=str(wrapper),
            chromium_sandbox=True,
        )


@dataclass
class _IsolatedWorld:
    cdp: CDPSession
    context_id: int

    async def call(self, function: str, *arguments: Any) -> Any:
        result = await self.cdp.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": function,
                "executionContextId": self.context_id,
                "arguments": [{"value": value} for value in arguments],
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text")
            raise RuntimeError(f"Isolated evaluator JavaScript failed: {message}")
        return result["result"].get("value")


@dataclass
class _Session:
    context: BrowserContext
    page: Page
    world: _IsolatedWorld | None = None
    writes: list[dict[str, Any]] = field(default_factory=list)
    stored_name: str = INITIAL_NAME
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    async def read(self, function: str, *arguments: Any) -> Any:
        if self.world is None:
            raise RuntimeError("Isolated evaluator world is unavailable")
        return await self.world.call(function, *arguments)


async def _new_session(browser: Browser, source: str) -> _Session:
    context = await browser.new_context(
        viewport={"width": 1100, "height": 850},
        service_workers="block",
        accept_downloads=False,
        java_script_enabled=True,
    )
    context.set_default_timeout(2500)
    context.set_default_navigation_timeout(5000)
    page = await context.new_page()
    session = _Session(context=context, page=page)
    html = render_fixture(source)
    document_served = False

    async def route_request(route: Route) -> None:
        nonlocal document_served
        request = route.request
        if request.url == ORIGIN + "/" and request.method == "GET" and not document_served:
            document_served = True
            await route.fulfill(status=200, content_type="text/html", body=html)
        elif request.url == ORIGIN + "/api/profile" and request.method == "POST":
            try:
                payload = json.loads(request.post_data or "null")
            except (ValueError, TypeError):
                payload = None
            entry = {"method": request.method, "payload": payload, "accepted": False}
            session.writes.append(entry)
            if (
                isinstance(payload, dict)
                and set(payload) == {"display_name"}
                and isinstance(payload["display_name"], str)
                and 0 < len(payload["display_name"]) <= 80
            ):
                session.stored_name = payload["display_name"]
                entry["accepted"] = True
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "display_name": session.stored_name,
                            "synthetic": True,
                        }
                    ),
                )
            else:
                await route.fulfill(
                    status=422,
                    content_type="application/json",
                    body='{"detail":"Invalid display name"}',
                )
        else:
            session.blocked.append(request.url[:200])
            await route.abort("blockedbyclient")

    await context.route("**/*", route_request)
    # HTTP routing does not intercept WebSockets. Block them explicitly as well.
    await context.route_web_socket(re.compile(".*"), lambda socket: socket.close())
    page.on("pageerror", lambda error: session.errors.append(str(error)[:1000]))
    context.on("page", lambda popup: popup.close())
    try:
        # Pause before candidate code loads, then explicitly run every virtual
        # timer tick (not fast_forward, which can skip repeated timer firings).
        await page.clock.install(time=_CLOCK_START)
        await page.clock.pause_at(_CLOCK_START + timedelta(seconds=1))
        await page.goto(ORIGIN + "/", wait_until="load")
        cdp = await context.new_cdp_session(page)
        tree = await cdp.send("Page.getFrameTree")
        world = await cdp.send(
            "Page.createIsolatedWorld",
            {
                "frameId": tree["frameTree"]["frame"]["id"],
                "worldName": "keyproof-oracle",
            },
        )
        session.world = _IsolatedWorld(cdp, world["executionContextId"])
        await _settle(session, 80)
    except BaseException as exc:
        await _close_preserving_error(context.close, exc)
        raise
    return session


async def _settle(session: _Session, milliseconds: int) -> None:
    await session.page.clock.run_for(milliseconds)
    # Route handlers and fetch promises run outside the page's virtual timers.
    await session.page.wait_for_timeout(180)


async def _snapshot(session: _Session) -> dict[str, Any]:
    page = session.page
    focus = await session.read("""() => {
      const el = document.activeElement;
      return {id: el.id || null, tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role'), name: el.getAttribute('aria-label') ||
        (el.labels?.length ? [...el.labels].map(l => l.innerText).join(' ') : el.innerText || ''),
        value: 'value' in el ? el.value : null};
    }""")
    return {
        "task": TASK_SPEC,
        "aria_snapshot": (await page.locator("body").aria_snapshot())[:18000],
        "focus": focus,
        "visible_text": (await session.read("() => document.body.innerText"))[:12000],
        "persistence": {
            "synthetic": True,
            "display_name": session.stored_name,
            "request_count": len(session.writes),
            "requests": list(session.writes),
        },
        "errors": list(session.errors),
        "allowed_keys": sorted(_ALLOWED_KEYS),
    }


class TaskBrowser:
    """A keyboard-only agent surface; no DOM execution, file access, or holdout data."""

    def __init__(self, source: str, artifact_dir: Path | None = None):
        _check_source(source)
        self.source = source
        self.artifact_dir = artifact_dir
        self._playwright: Any = None
        self._browser: Browser | None = None
        self._session: _Session | None = None
        self._action_count = 0

    async def __aenter__(self) -> TaskBrowser:
        self._playwright = await async_playwright().start()
        try:
            self._browser = await _launch_chromium(self._playwright)
            self._session = await _new_session(self._browser, self.source)
        except BaseException as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._browser is not None:
                await _close_preserving_error(self._browser.close, exc)
        finally:
            if self._playwright is not None:
                await _close_preserving_error(
                    self._playwright.stop, exc if exc is not None else sys.exception()
                )

    async def observe(self) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("TaskBrowser must be entered before observing")
        async with asyncio.timeout(5):
            return await _snapshot(self._session)

    async def act(self, action: BrowserAction) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("TaskBrowser must be entered before acting")
        if self._action_count >= 40:
            raise ValueError("Task browser is limited to 40 actions")
        async with asyncio.timeout(8):
            page = self._session.page
            if action.kind == "press":
                if action.value not in _ALLOWED_KEYS:
                    raise ValueError("Unsupported keyboard key")
                await page.keyboard.press(action.value)
            elif action.kind == "type":
                if any(ord(char) < 32 or ord(char) == 127 for char in action.value):
                    raise ValueError("Typing accepts printable text only")
                await page.keyboard.insert_text(action.value)
            elif action.kind != "finish":
                raise ValueError("Unsupported browser action")
            self._action_count += 1
            await _settle(self._session, 180)
            result = await self.observe()
            result["action"] = action.model_dump()
            result["step"] = self._action_count
            return result

    async def settle(self, milliseconds: int) -> dict[str, Any]:
        """Advance a bounded virtual interval and observe the trusted persistence ledger."""
        if self._session is None:
            raise RuntimeError("TaskBrowser must be entered before settling")
        if type(milliseconds) is not int or not 0 <= milliseconds <= _QUIET_WINDOW_MS:
            raise ValueError("Settling requires 0–60000 virtual milliseconds")
        async with asyncio.timeout(12):
            await _settle(self._session, milliseconds)
            result = await self.observe()
            result["blocked"] = list(self._session.blocked)
            return result

    async def capture(self) -> str | None:
        """Capture the current trusted browser surface when an artifact directory was supplied."""
        if self._session is None:
            raise RuntimeError("TaskBrowser must be entered before capturing")
        if self.artifact_dir is None:
            return None
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"probe-{_source_hash(self.source)[:12]}.png"
        await self._session.page.screenshot(path=str(path), full_page=True, timeout=5000)
        return str(path)


async def _semantics(session: _Session, *, modal: bool = False) -> dict[str, Any]:
    return await session.read(
        """(modal) => {
      const checks = [];
      function check(id, tag, name, type, shouldShow) {
        const matches = document.querySelectorAll('#' + id);
        const el = matches[0];
        let visible = false;
        if (el) {
          const r = el.getBoundingClientRect();
          visible = r.width >= 16 && r.height >= 16 && r.left >= 0 && r.top >= 0 &&
            r.right <= innerWidth && r.bottom <= innerHeight;
          for (let node = el; node; node = node.parentElement) {
            const s = getComputedStyle(node);
            visible = visible && s.display !== 'none' && s.visibility === 'visible' &&
              Number(s.opacity) >= 0.5 && s.clipPath === 'none' &&
              (s.clip === 'auto' || s.clip === '') && !node.hidden &&
              node.getAttribute('aria-hidden') !== 'true';
          }
        }
        const actualName = el ? (el.getAttribute('aria-label') ||
          (el.labels?.length ? [...el.labels].map(l => l.innerText.trim()).join(' ') : el.textContent.trim())) : '';
        checks.push({id, present: matches.length === 1, tag: el?.tagName.toLowerCase(),
          name: actualName, type: el?.getAttribute('type'), visible,
          enabled: !!el && !el.disabled && !el.readOnly,
          passed: matches.length === 1 && el.tagName.toLowerCase() === tag &&
            actualName === name && el.getAttribute('type') === type &&
            !el.hasAttribute('role') && !el.disabled && !el.readOnly && visible === shouldShow});
      }
      check('display-name', 'input', 'Display name', 'text', true);
      check('save-name', 'button', 'Save changes', 'button', true);
      check('open-notifications', 'button', 'Notification settings', 'button', true);
      check('weekly-summary', 'input', 'Weekly email summary', 'checkbox', modal);
      check('close-notifications', 'button', 'Done', 'button', modal);
      const dialog = document.getElementById('notifications-dialog');
      const structure = document.title === 'Harbor · Workspace settings' &&
        document.querySelectorAll('h1').length === 1 &&
        document.querySelector('h1')?.textContent === 'Workspace settings' &&
        document.querySelectorAll('button').length === 3 &&
        document.querySelectorAll('input').length === 2 &&
        document.querySelectorAll('dialog').length === 1 &&
        dialog?.getAttribute('aria-labelledby') === 'dialog-heading' &&
        document.getElementById('dialog-heading')?.textContent === 'Notification settings' &&
        dialog.open === modal;
      return {passed: structure && checks.every(c => c.passed), structure, controls: checks};
    }""",
        modal,
    )


async def evaluate_source(
    source: str,
    *,
    phase: Literal["development", "holdout"] = "development",
    artifact_dir: Path | None = None,
) -> EvaluationReport:
    started = time.perf_counter()
    gates: list[GateResult] = []
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    violations: list[str] = []
    artifacts: list[str] = []
    digest = _source_hash(source)
    if phase not in {"development", "holdout"}:
        raise ValueError("Unknown evaluation phase")

    def gate(name: str, passed: bool, expected: Any, actual: Any, detail: str = "") -> None:
        gates.append(
            GateResult(name=name, passed=passed, expected=expected, actual=actual, detail=detail)
        )

    async def press(session: _Session, case: str, key: str) -> None:
        await session.page.keyboard.press(key)
        await _settle(session, 180)
        actions.append(
            {
                "case": case,
                "kind": "press",
                "value": key,
                "focus": await session.read(
                    "() => document.activeElement.id || document.activeElement.tagName"
                ),
                "request_count": len(session.writes),
                "stored_name": session.stored_name,
            }
        )

    async def type_name(session: _Session, case: str, value: str) -> None:
        await press(session, case, "Control+A")
        await session.page.keyboard.insert_text(value)
        await _settle(session, 180)
        actions.append(
            {
                "case": case,
                "kind": "type",
                "value": value,
                "request_count": len(session.writes),
                "stored_name": session.stored_name,
            }
        )

    async def axe(session: _Session, case: str) -> None:
        # CDP compiles trusted vendored code directly in our private world. No
        # candidate global, script element, or page-provided window.axe is used.
        await session.read("function() {\n" + axe_source + "\n}")
        result = await session.read("""async () => {
          if (!window.axe || typeof window.axe.run !== 'function') throw new Error('axe-core unavailable');
          const result = await window.axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa']}});
          return {violations: result.violations.map(v => ({id: v.id, impact: v.impact,
            description: v.description, nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))})),
            passes: result.passes.length, incomplete: result.incomplete.map(v => v.id)};
        }""")
        violations.extend(f"{case}: {v['id']}" for v in result["violations"])
        gate(
            case + ".axe",
            not result["violations"] and result["passes"] > 0,
            "No WCAG A/AA violations; axe ran real checks",
            result,
        )

    async def capture(session: _Session, case: str) -> None:
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path = artifact_dir / f"{phase}-{digest[:12]}-{case}.png"
            await session.page.screenshot(path=str(path), full_page=True, timeout=5000)
            artifacts.append(str(path))

    async def quiet_window(session: _Session, case: str) -> None:
        before = len(session.writes)
        await _settle(session, _QUIET_WINDOW_MS)
        actions.append(
            {
                "case": case,
                "kind": "quiet_window",
                "virtual_duration_ms": _QUIET_WINDOW_MS,
                "request_count_before": before,
                "request_count": len(session.writes),
                "stored_name": session.stored_name,
                "errors": list(session.errors),
                "blocked": list(session.blocked),
            }
        )

    async def save_case(browser: Browser, case: str, activation: str, values: list[str]) -> None:
        session = await _new_session(browser, source)
        try:
            page = session.page
            initial = await _semantics(session)
            gate(
                case + ".semantics.initial",
                initial["passed"],
                "Original visible native controls",
                initial,
            )
            gate(
                case + ".reset",
                not session.writes and session.stored_name == INITIAL_NAME,
                {"request_count": 0, "display_name": INITIAL_NAME},
                {"request_count": len(session.writes), "display_name": session.stored_name},
            )
            if case == "keyboard":
                await axe(session, case)
            await press(session, case, "Tab")
            focus = await session.read("() => document.activeElement.id")
            gate(case + ".entry_focus", focus == "display-name", "display-name", focus)
            for index, value in enumerate(values):
                if index:
                    await press(session, case, "Shift+Tab")
                before = len(session.writes)
                await type_name(session, case, value)
                gate(
                    f"{case}.{index}.edit_without_save",
                    len(session.writes) == before,
                    before,
                    len(session.writes),
                    "Typing must not trigger persistence",
                )
                await press(session, case, "Tab")
                focus = await session.read("() => document.activeElement.id")
                gate(f"{case}.{index}.save_focus", focus == "save-name", "save-name", focus)
                if activation == "pointer":
                    await page.get_by_role("button", name="Save changes", exact=True).click()
                    await _settle(session, 180)
                    actions.append(
                        {
                            "case": case,
                            "kind": "pointer",
                            "target": "Save changes",
                            "request_count": len(session.writes),
                            "stored_name": session.stored_name,
                        }
                    )
                else:
                    await press(session, case, activation)
                await quiet_window(session, case)
                delta = session.writes[before:]
                expected = {"display_name": value, "request_count": 1}
                actual = {
                    "display_name": session.stored_name,
                    "request_count": len(delta),
                    "requests": delta,
                }
                gate(
                    f"{case}.{index}.persist_exactly_once",
                    len(delta) == 1
                    and delta[0]["accepted"]
                    and delta[0]["payload"] == {"display_name": value}
                    and session.stored_name == value,
                    expected,
                    actual,
                    "Python-owned request ledger, not page success text",
                )
                semantic = await _semantics(session)
                gate(
                    f"{case}.{index}.semantics.after",
                    semantic["passed"],
                    "Visible enabled native controls",
                    semantic,
                )
            await quiet_window(session, case)
            settled = await _semantics(session)
            gate(
                case + ".semantics.settled",
                settled["passed"],
                "Controls preserved after quiet window",
                settled,
            )
            await axe(session, case + ".settled")
            await capture(session, case)
            gate(
                case + ".total_requests",
                len(session.writes) == len(values),
                len(values),
                len(session.writes),
            )
            gate(
                case + ".runtime",
                not session.errors and not session.blocked,
                "No script errors or blocked navigation/network",
                {"errors": session.errors, "blocked": session.blocked},
            )
        finally:
            await _close_preserving_error(session.context.close, sys.exception())

    async def modal_case(browser: Browser, case: str, opener_key: str, closer: str) -> None:
        session = await _new_session(browser, source)
        try:
            for _ in range(3):
                await press(session, case, "Tab")
            focus = await session.read("() => document.activeElement.id")
            gate(case + ".opener_focus", focus == "open-notifications", "open-notifications", focus)
            await press(session, case, opener_key)
            semantic = await _semantics(session, modal=True)
            gate(
                case + ".semantics",
                semantic["passed"],
                "Visible named native modal and controls",
                semantic,
            )
            focus = await session.read("() => document.activeElement.id")
            gate(case + ".initial_focus", focus == "weekly-summary", "weekly-summary", focus)
            await axe(session, case)
            await press(session, case, "Tab")
            focus = await session.read("() => document.activeElement.id")
            gate(case + ".forward", focus == "close-notifications", "close-notifications", focus)
            await press(session, case, "Tab")
            focus = await session.read("() => document.activeElement.id")
            # Chromium's native modal wraps via a transient body focus on some
            # releases; the next Tab must remain in the dialog, never the page.
            if focus == "":
                await press(session, case, "Tab")
                focus = await session.read("() => document.activeElement.id")
            gate(case + ".trap_forward", focus == "weekly-summary", "weekly-summary", focus)
            checked_before = await session.read(
                "() => document.getElementById('weekly-summary').checked"
            )
            await press(session, case, "Space")
            checked_after = await session.read(
                "() => document.getElementById('weekly-summary').checked"
            )
            gate(
                case + ".checkbox_toggle",
                checked_after != checked_before,
                not checked_before,
                checked_after,
            )
            await press(session, case, "Shift+Tab")
            focus = await session.read("() => document.activeElement.id")
            if focus == "":
                await press(session, case, "Shift+Tab")
                focus = await session.read("() => document.activeElement.id")
            gate(
                case + ".trap_reverse", focus == "close-notifications", "close-notifications", focus
            )
            await capture(session, case)
            await press(session, case, closer)
            closed = not await session.read(
                "() => document.getElementById('notifications-dialog').open"
            )
            focus = await session.read("() => document.activeElement.id")
            gate(case + ".closed", closed, True, closed)
            gate(case + ".focus_return", focus == "open-notifications", "open-notifications", focus)
            await quiet_window(session, case)
            settled = await _semantics(session)
            gate(
                case + ".semantics.settled",
                settled["passed"],
                "Closed modal and preserved controls after quiet window",
                settled,
            )
            await axe(session, case + ".settled")
            gate(case + ".no_profile_writes", not session.writes, 0, len(session.writes))
            gate(
                case + ".runtime",
                not session.errors and not session.blocked,
                "No script errors or blocked navigation/network",
                {"errors": session.errors, "blocked": session.blocked},
            )
        finally:
            await _close_preserving_error(session.context.close, sys.exception())

    try:
        _check_source(source)
        vendor = FIXTURE_DIR / "axe.min.js"
        if not vendor.is_file() or vendor.stat().st_size < 1000:
            raise RuntimeError("Vendored axe-core is missing; accessibility evaluation unavailable")
        axe_source = vendor.read_text(encoding="utf-8")
        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright)
            try:
                if phase == "development":
                    cases = [
                        (save_case, "keyboard", "Enter", ["Jordan Lee"]),
                        (save_case, "pointer", "pointer", ["Taylor Reed"]),
                        (modal_case, "modal_escape", "Enter", "Escape"),
                        (modal_case, "modal_done", "Enter", "Enter"),
                    ]
                else:
                    cases = [
                        (
                            save_case,
                            "keyboard",
                            "Space",
                            ["Riley Chen", "Riley Chen", "Samira O’Neill"],
                        ),
                        (save_case, "pointer", "pointer", ["Noor Patel", INITIAL_NAME]),
                        (modal_case, "modal_escape", "Space", "Escape"),
                        (modal_case, "modal_done", "Space", "Space"),
                    ]
                for run_case, name, activation, argument in cases:
                    try:
                        async with asyncio.timeout(25):
                            await run_case(browser, name, activation, argument)
                    except Exception as exc:
                        message = f"{name}: {type(exc).__name__}: {str(exc)[:1800]}"
                        errors.append(message)
                        gate(
                            name + ".execution",
                            False,
                            "Case completes in bounded Chromium session",
                            message,
                        )
            finally:
                await _close_preserving_error(browser.close, sys.exception())
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {str(exc)[-1800:]}")
        gate(
            "oracle.available",
            False,
            "Network namespace, Chromium sandbox and vendored axe available",
            errors[-1],
        )
    return EvaluationReport(
        phase=phase,
        passed=bool(gates) and all(item.passed for item in gates) and not errors,
        gates=gates,
        source_hash=digest,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        actions=actions,
        axe_violations=violations,
        errors=errors,
        artifacts=artifacts,
    )
