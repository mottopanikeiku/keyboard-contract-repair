# Local browser replay

Run on 2026-10-05 from published checkout `226f7a695640ccc9f6dd6137c35a38790bb55d07`, with the added `tests/test_recorded_comparison.py`. No source from the owner's working clone was used.

## What was checked

The new tests read the exact JavaScript stored in `evidence/first-local-comparison.json`; they do not regenerate repairs or invoke a model.

| Candidate | Development | Held-out | New rapid-save probe |
| --- | --- | --- | --- |
| Recorded original | 47/51, same four recorded failures | Not replayed | Not replayed |
| Recorded team repair | 51/51 | 63/63 | Passed |
| Recorded single repair | 51/51 | 63/63 | Passed |

Scores and source hashes are asserted against the published record. The new, hand-authored probe edits the name to Jordan Lee and saves with Enter, edits it to Taylor Reed and saves with Space, then edits it to Unsaved Draft without saving. For each recorded repair, Python observed exactly the two accepted POST payloads in that order, with Taylor Reed retained as the stored name. The separate edit-only control also passed. This checks the existing candidates against an additional sequence; it is not a new team-versus-single model comparison or a model-discovered counterexample.

The existing tests also exercised the original keyboard failure despite a clean accessibility scan, an Enter-only repair that fails held-out Space activation, delayed contrast changes despite a forged scanner, dropped rapid saves, deferred reads, autosave substitution, probe admission, patch acceptance, shutdown handling, local HTTP responses, and exported-result validation. Provider responses in execution-boundary tests were replaced with test functions; no actual provider was called.

## Command and result

```sh
nice -n 19 env WEAVE_DISABLED=true uv run pytest -q -x tests/test_recorded_comparison.py tests/test_browser_contract.py tests/test_counterexample_probes.py tests/test_patches.py tests/test_execution_boundaries.py tests/test_learning_http.py tests/test_portable_evidence.py
```

Result: **46 passed in 154.74 seconds**. These were all test modules present in the fresh clone plus the new replay module. No cloud trace was published and no paid compute was used.

The new test module also passed:

```sh
nice -n 19 uv run ruff check tests/test_recorded_comparison.py
nice -n 19 uv run ruff format --check tests/test_recorded_comparison.py
```

Both checks passed; the file was already formatted. Full-source lint, package build, dashboard smoke tests, fresh model generation, and Weave cloud publishing were not run.

## Environment

Linux x86-64 on an AMD Ryzen AI 5 PRO 340 CPU; no GPU used. `uv sync --locked --python 3.12` selected Python 3.12.13. The lockfile installed Playwright 1.62.0, pytest 9.1.1, and Weave 0.53.9. Chromium ran with the existing bubblewrap network isolation. Playwright used its Ubuntu 24.04 fallback browser build on this unsupported distribution. Available memory before setup was 3,005 MiB; tests were serial and ran at nice level 19.

## What this does not establish

The replay confirms the retained candidates still meet these assertions under the current locked dependencies. It cannot recover the missing exact runtime revision of the original model comparison, reproduce stochastic model outputs, establish a dollar-cost advantage, or show that these repairs generalize to real applications.

A useful next experiment is to apply the same independent request and focus checks to an owner-controlled real profile workflow. Keep the single-agent baseline and record the exact runtime revision; first run the browser checks without model calls. Selecting and authorizing that application is an owner decision. The current synthetic fixture and recorded failures should remain regression tests.
