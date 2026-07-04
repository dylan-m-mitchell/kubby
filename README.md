# kubui

A GUI tool for managing local Kubernetes cluster resources.

## Quick start

```bash
uv run kubui            # opens the GUI
uv run kubui --check    # prints install status of all managed tools (no GUI)
uv run kubui --debug    # right-click → Inspect opens DevTools
```

`uv run main.py` works too (delegates to the same entry point).

## Step 1 — first-run tool installation

The GUI ships with four required CLIs:

| Tool      | Role                                                |
|-----------|-----------------------------------------------------|
| minikube  | local Kubernetes cluster                            |
| helm      | Kubernetes package manager                         |
| podman    | daemonless container engine (minikube driver)      |
| kubectl   | official Kubernetes CLI                             |

At first launch kubui detects which are missing and lets you install each
missing tool with one click. Installation uses the host's package manager
(apt, dnf, pacman, zypper, or Linuxbrew) with `pkexec` (graphical polkit
prompt) for elevation.

## Requirements

- Linux desktop session with:
  - **display server** (X11 or Wayland)
  - **WebKit2GTK 4.0 or 4.1** (preinstalled on most Debian/Ubuntu/Fedora/Arch systems)
  - **Polkit** for graphical install elevation (`pkexec`)
- Python ≥ 3.12

## Layout

```
kubui/
  __init__.py
  __main__.py        # python -m kubui
  app.py             # pywebview window + JS API
  installer/
    tools.py         # registry of managed tools
    detector.py      # PATH + version detection
    linux.py         # package manager + elevation
  ui/
    index.html       # frontend shell
    app.js           # frontend logic
    style.css        # dark webview theme
main.py              # legacy thin launcher
pyproject.toml       # uv project, [project.scripts] entry point
```

## Roadmap

The codebase is sized for incremental growth. Step 1 covers the dependency
detection/installation flow; subsequent steps will add cluster lifecycle
(create/start/stop minikube), in-cluster views (pods, deployments,
services), and helm release management — all reachable from this same UI.
