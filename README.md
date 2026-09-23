# ToolCall Replay

[Try the interactive synthetic demo](https://ardian-mehaj-portfolio.vercel.app/projects/toolcall-replay/) · [Demo source and local preview](docs/interactive-demo/README.md) · [Contact me](mailto:mehajardian@gmail.com)

Compare a baseline and candidate tool-call trace against explicit rules. The evaluator is deterministic and does not execute tools or contact a model.

## Try the synthetic example

Requires Python 3.12.14 and uv. The initial installation downloads locked development dependencies; the installed runtime uses the Python standard library.

From this checkout:

```sh
uv sync --frozen
scripts/lab.sh start
```

Open the loopback URL printed by the launcher. Choose Risky broad update and replay the trace. The candidate correctly receives FAIL for an over-broad lookup, a forbidden export and a missing approval. This is the expected rejection of the synthetic trace, not an application crash. Invalid input produces an error instead of a partial report.

Stop the server when finished:

```sh
scripts/lab.sh stop
```

## Check a rule against the evaluator

The [synthetic suite](examples/synthetic-suite.json) declares seven rules, including `lookup-scope`, `no-directory-export` and `update-needs-approval`. The [CLI report test](tests/test_cli_reports.py) asserts that the baseline passes all seven and that the risky candidate fails exactly those three. After `uv sync --frozen`, run:

```sh
scripts/demo.sh
```

The script invokes the real `toolcall_replay.cli evaluate` command twice, compares the generated reports and checks those rule IDs. The evaluator exits with code 1 for the expected risky-trace `FAIL`; the script exits with code 0 only when the assertions hold. The locally launched workbench UI uses the evaluator through its adapter. The hosted portfolio walkthrough displays prepared trace data. No tool calls are executed.

## What this public release contains

This is a clean public source snapshot of the local project, not a copy of its private Git history. Application source files and synthetic input files are unchanged from the verified local implementation. Private orchestration records, author paths, archived build-proof machinery and one-shot research runners are not distributed. Their canonical local versions and history remain preserved.

The tests shipped here cover the public runtime. They are a defined subset of the larger local verification suite, not a claim that every historical control is reproduced by this package. `SOURCE_MANIFEST.json` identifies every copied file, and `PUBLIC_RELEASE_SCOPE.json` lists the selected runtime tests and the omitted verification categories.

```sh
uv run --frozen pytest -q
```

The prepared [CI workflow](.github/workflows/ci.yml) runs the public tests and static checks on GitHub after publication. Its presence here is not a hosted CI result.

Local check on 2026-09-23 (this public snapshot): `uv run --frozen pytest -q` returned 298 passed; `scripts/demo.sh` returned code 0 and printed `BASELINE=PASS` with the three expected candidate failures. These checks cover the distributed test selection, not the private project's full history.

## Limits

Only the supported trace schema and seven rule types are evaluated. This does not prove an agent is safe in production.

The browser server is designed for a local machine. Do not expose it directly on the Internet. The recruiter preview is a separate static explanation with recorded synthetic results.

## My role

I use AI extensively to build these projects. I understand and review the code, and I am still learning to write it independently.
