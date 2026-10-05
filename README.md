# Keyboard contract repair

This is a small experiment in repairing keyboard save and dialog-focus bugs on a synthetic profile page.

Can a browser oracle distinguish a working repair from a page that merely says “Changes saved”—and does a team of agents repair it better than a single agent?

The editable [page behavior](src/keyproof/fixtures/behavior.js) ignores keyboard saves and restores focus to the wrong field after closing a dialog. The [oracle](src/keyproof/oracle.py) runs real Chromium, checks the actual POST payloads in a Python-owned endpoint, and inspects focus; the [probe runner](src/keyproof/probes.py) also tests rapid saves with an edit-only control. Accessibility scanning uses vendored [axe-core](https://github.com/dequelabs/axe-core), by Deque Systems and contributors, under [MPL-2.0](src/keyproof/fixtures/axe.LICENSE).

## Result

**The recorded team-versus-single comparison is a tie on correctness; the single agent used fewer model calls and tokens.** Both used `gpt-5.6-sol`. This is one pair on one synthetic task, not evidence that either approach is generally better.

| Mode | Development, before → after | Held-out checks | Model calls | Input / output tokens |
| --- | --- | --- | --- | --- |
| Team | 47/51 → 51/51 | 63/63 | 4 | 97,261 / 1,614 |
| Single | 47/51 → 51/51 | 63/63 | 1 | 18,996 / 227 |

Source: [recorded comparison, including exact candidate JavaScript](evidence/first-local-comparison.json). “Cheaper” here means lower recorded usage; there is no dollar-cost measurement. The original run did not record its exact checkout revision.

Passing the fixed checks is not enough: delayed reads and dropped rapid saves can pass both check sets while losing an earlier activation's value. The [counterexample-learning record](evidence/counterexample-learning.json) contains cold and successful warm runs that replay learned interaction sequences against these faults, plus a failed warm attempt with a browser timeout. It does not establish a speed advantage for warm memory. Weave cloud publishing was not verified; tracing was disabled in these recorded runs.

The [controller](src/keyproof/controller.py) accepts an edit only when development failures decrease without breaking a previously passing check, then evaluates the unchanged candidate on held-out checks. Probe assertions belong to Python, not the proposing model: the edit-only control rejects autosave behavior that could otherwise disguise an ignored Save activation.

## Reproduce without model calls

Use Linux, Python, `uv`, installed `bubblewrap`, and working unprivileged user namespaces. Chromium also needs its system libraries; [CI](.github/workflows/checks.yml) shows the Ubuntu setup. A CPU laptop is enough; no GPU, API key, model download, or paid compute is needed. Free dependency/browser downloads require internet during setup; tests run with cloud tracing disabled.

```sh
nice -n 19 uv sync --locked --python 3.12
nice -n 19 uv run playwright install chromium
nice -n 19 env WEAVE_DISABLED=true uv run pytest -q -x
```

This replays behavior, not model generation. The new [recorded-candidate tests](tests/test_recorded_comparison.py) rerun the published original and repairs, then extend each repair with rapid Enter/Space saves followed by an unsaved edit. [Local replay details](docs/REPLAY.md) record the command, environment, and observed result. No new model calls were made for this change.

## Limitations

- One tiny synthetic page and one model; no real application or user study.
- Passing a finite set of interaction checks is not accessibility certification or proof against every sequence.
- “Persistence” is a local Python request ledger, not a database or production backend.
- The held-out checks are published here; they are withheld from repair prompts, not secret from repository readers.
- No verified Weave cloud trace and no repeated-seed comparison or measured dollar cost.

## Prior work

The oracle builds on [Playwright's real-browser automation](https://playwright.dev/python/) and [axe-core's accessibility rules](https://github.com/dequelabs/axe-core). Its focus checks follow the [WAI-ARIA modal-dialog pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/), including returning focus to the invoking control. [Weave](https://weave-docs.wandb.ai/) is an optional tracing integration, not a demonstrated result here.
