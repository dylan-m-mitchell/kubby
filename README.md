# kubby

[![CI](https://github.com/dylan-m-mitchell/kubby/actions/workflows/ci.yml/badge.svg)](https://github.com/dylan-m-mitchell/kubby/actions/workflows/ci.yml)

A terminal UI for managing local Kubernetes cluster resources.

Start a minikube cluster, watch pods come up, install kubectl/helm/podman from
the tools panel — all in one TUI (built with [Textual](https://textual.textualize.io/)).
Press `?` inside the app for the full key reference.

## Install

The shipped artifact is a single self-contained binary (PyInstaller --onefile).
No Python, pip, or system GUI libraries required — kubby runs in any
terminal.

### Pre-built binary

Download the latest `kubby` binary from the releases page, then:

```bash
chmod +x kubby
./kubby                  # launch the TUI
./kubby --check          # print tool status (no TUI)
./kubby --debug          # stream Textual devtools (see Usage)
```

### Install the managed CLIs

The **tools** panel has an install action for every managed CLI
(minikube, kubectl, helm, podman): `i` installs the highlighted tool, `I`
installs everything that's missing. Each one runs the tool's official
upstream installer with elevation (pkexec on a desktop session, sudo as a
fallback):

| Tool      | Source                                                                |
|-----------|-----------------------------------------------------------------------|
| minikube  | `minikube.sigs.k8s.io/scripts/install.sh`                             |
| helm      | `get-helm-4` from `raw.githubusercontent.com/helm/helm/main/scripts/` |
| kubectl   | latest stable binary from `dl.k8s.io` → `/usr/local/bin/kubectl`      |
| podman    | host package manager (apt / dnf / pacman / zypper)                    |

An install doubles as upgrade — it is always enabled when no other job is in
flight. The installer's output streams into the log panel at the bottom of
the window; on success the row flips to **installed**, on failure the panel
stays visible with diagnostics so you can read what went wrong.

## Usage

| Command         | Effect                                                             |
|-----------------|--------------------------------------------------------------------|
| `kubby`         | Launch the TUI                                                     |
| `kubby --check` | Print install status for all managed tools, then exit              |
| `kubby --debug` | Enable Textual devtools: needs `textual-dev` installed *and* a running `textual dev` server to stream to |

Inside the app: `tab` cycles panels, `?` opens the key reference, `q`
quits. Each panel lists its own keys in the keybar at the bottom of the
screen — greyed keys are unavailable in the current state.

## Managed CLIs

| Tool      | Role                                                |
|-----------|-----------------------------------------------------|
| minikube  | local Kubernetes cluster                            |
| kubectl   | official Kubernetes CLI                             |
| helm      | Kubernetes package manager                          |
| podman    | daemonless container engine (minikube driver)       |

## Building from source

```bash
git clone https://github.com/.../kubby
cd kubby
uv sync
uv run kubby             # run from source
uv run kubby --check     # print tool status
```

### Tests

```bash
uv run pytest        # unit tests (pytest is a dev dependency)
```

Tests live in `tests/` and cover the pure-logic modules (tool registry and
its version parsers, settings load/save coercion, package-manager +
elevation detection, settings → `minikube` argv translation, the service
layer) and the TUI itself (driven headlessly through Textual's pilot —
panels, modals, keybindings, job lifecycle). They need no display, no
network, and no cluster.

### Build the binary

```bash
pip install pyinstaller   # one-time
./build-binary.sh         # output: dist/kubby
```

`build-binary.sh` is a thin wrapper around `pyinstaller --noconfirm kubby.spec`.
See `kubby.spec` for what gets bundled (the TUI stylesheet, Textual's data
files, onefile mode).

## Continuous integration

Every PR runs lint, tests and a binary build (`.github/workflows/ci.yml`);
tagging `vX.Y.Z` builds, smoke-tests and publishes a release
(`.github/workflows/release.yml`).

Opening a PR also starts the **agent review loop**
(`.github/workflows/agent-review.yml`). The `ci-reviewer` OpenCode agent
reviews the diff, posts a fix plan as a sticky PR comment, applies the fixes
it considers safe, and the loop goes around again — up to 3 rounds — until
the agent reports `CLEAN`, or a finding needs a human. Nothing is pushed
until the round passes the same `ruff` + `pytest` checks CI runs, and a
failing round hands its error output straight back to the agent.

- **Opt out:** open the PR as a draft, or add the `skip-agent-review` label.
- **Model:** set the `REVIEW_MODEL` repository variable to choose one.
  Without it, adding the `OPENCODE_API_KEY` secret (an OpenCode Console
  service-account key) selects a paid model; with neither, the loop falls
  back to a free model so it still runs.
- **Guardrails:** the agent cannot commit, push, edit `.github/`, or reach
  the network — the surrounding script owns every GitHub action, and fork
  PRs are reviewed but never edited (their token is read-only).

## Requirements

- Any interactive terminal on Linux (X11/Wayland not required)
- For building the binary: `pyinstaller` (any recent version)
- For running from source: Python 3.12+ and `uv`

## Layout

```
.github/workflows/      ci.yml, release.yml, agent-review.yml
.opencode/agents/
  ci-reviewer.md        # the PR review agent (permissions + system prompt)
scripts/
  agent-review-loop.sh  # review → plan → fix → gate → push, ≤ 3 rounds
kubby/
  __init__.py
  __main__.py             # python -m kubby
  app.py                  # CLI: --check / --debug, launches the TUI
  service.py              # UI-agnostic service layer (detect/install/minikube)
  images.py               # local podman image listing
  settings.py             # settings.json load/save
  installer/
    tools.py              # registry of managed tools + install scripts
    detector.py           # PATH + version detection
    linux.py              # package manager + elevation (pkexec / sudo)
    minikube.py           # settings → minikube argv
  tui/
    app.py                # Textual shell: layout, bindings, workers
    panels.py             # minikube / tools / cluster / images / log panels
    popups.py             # settings, help, confirm and preflight modals
    styles.tcss           # theme + widget styling
kubby.spec                # PyInstaller spec (onefile build)
build-binary.sh           # developer: produce dist/kubby
pyproject.toml            # project config (deps: textual)
tests/                    # pytest suite (uv run pytest)
```

## Roadmap

Step 1 covers the dependency detection/installation flow and the cluster
overview (nodes, namespaces, pods). Subsequent steps add helm release
management, in-cluster resource editing, and multi-cluster support.
