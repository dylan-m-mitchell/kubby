# AGENTS.md

Guidance for AI agents (and humans) working in this repository. Committed so
everyone gets the same instructions.

## What this is

`kubby` is a terminal UI for managing local Kubernetes resources, built with
[Textual](https://textual.textualize.io/). Python 3.12+, dependencies managed
with [`uv`](https://docs.astral.sh/uv/).

## Setup and commands

```bash
uv sync --locked        # install dependencies (after cloning)
uv run kubby            # launch the TUI
uv run kubby --check    # print tool status, no TUI
uv run pytest           # the test suite — this is the test command
uvx ruff@0.16.8 check . --select E9,F   # lint, same rules CI runs
```

There is no `package.json` and no npm test runner. The only npm usage in the
repo is installing the OpenCode CLI globally, in the agent review workflow.

## Branches and PRs

- `dev` is where pull requests land. `master` is the release branch.
- Feature branches only run CI through their pull request, deliberately, so a
  commit is not tested twice.
- Open a PR against `dev`: `gh pr create -b dev -h <branch>`.
- Tag `vX.Y.Z` to build and publish a release.

## Layout

```
kubby/
  service.py            UI-agnostic domain logic (detect/install/minikube)
  settings.py           settings.json load/save
  installer/            tool registry, detection, elevation, argv building
  tui/                  the Textual UI — app shell, panels, popups, styles
tests/                  pytest suite, driven headlessly via Textual's pilot
.github/workflows/      ci.yml, release.yml, agent-review.yml
scripts/                the agent review loop
```

UI changes go in `kubby/tui/` (there is no `kubby/ui/`).

## Testing notes

- The suite needs no display, no network and no cluster. TUI tests drive a
  real `KubbyApp` through Textual's pilot with a `FakeService` standing in for
  the host.
- Anything that waits on a worker thread must poll (`helpers.wait_until`)
  rather than assert immediately, or it will race the thread it is waiting
  for. This has bitten the suite more than once.
- `asyncio_mode = "auto"`, so async tests need no decorator.

## The agent review loop

Opening a PR starts `.github/workflows/agent-review.yml`, which runs a
`ci-reviewer` agent over the diff, applies the fixes it judges safe, and
repeats up to 2 rounds. It is free by default and needs no configuration.

- It runs `ruff` and `pytest` itself before every push, so never assume a
  commit is gated until CI says so.
- These files are guardrails: `scripts/agent-review-loop.sh`,
  `.opencode/agents/ci-reviewer.md`, `.github/**`, and this file — because
  OpenCode loads `AGENTS.md` as instructions folded into the reviewer's
  system prompt, so a PR rewriting it would edit the rules the reviewer is
  reasoning under. The loop discards changes to them and says so in its
  sticky comment. Expect a human to look at a PR that edits them.
- The loop only triggers on open, reopen and ready-for-review. It does not
  re-run on a plain push.

## Conventions

- Keep changes small and in scope; match the surrounding style and comments.
- Comments explain *why*, especially where behaviour is load-bearing or
  surprising. The codebase leans heavily on this.
- Blocking work (subprocesses, disk, network) runs in a Textual worker and
  returns to the UI thread; never block the event loop.
