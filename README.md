# kubby

A GUI tool for managing local Kubernetes cluster resources.

## Install

```bash
sudo apt install ./kubby_*.deb
```

That's it. apt handles all dependencies automatically:

| Dependency | How it's handled |
|---|---|
| **podman**, **curl**, GTK/WebKit libs | Installed by apt via `Depends` |
| **kubectl**, **helm**, **minikube** | Downloaded by the post-install script |

To uninstall:

```bash
sudo apt remove kubby      # keeps kubectl, helm, minikube
sudo apt purge kubby       # removes /opt/kubby and CLI tools (kubectl, helm, minikube) if kubby installed them
```

## Usage

```bash
kubby                # launch the GUI
kubby --check        # print tool status (no GUI)
kubby --debug        # enable DevTools (right-click → Inspect)
```

## Managed CLIs

| Tool      | Role                                           |
|-----------|------------------------------------------------|
| minikube  | local Kubernetes cluster                       |
| helm      | Kubernetes package manager                     |
| podman    | daemonless container engine (minikube driver)  |
| kubectl   | official Kubernetes CLI                        |

## Building from source

If you're developing kubby and need to rebuild the `.deb`:

```bash
bash build-deb.sh
```

This installs the build toolchain, packages the app into a `.deb`, and prints the
install command. Run from the project root.

For rapid iteration without packaging:

```bash
uv run kubby            # run from source
uv run kubby --check    # tool status
```

## Requirements

- **Ubuntu 24.04+** (or Debian-based system with apt)
- Linux desktop session with a display server (X11 or Wayland)
- WebKit2GTK 4.1 (installed automatically by apt)

## Layout

```
kubby/
  __init__.py
  __main__.py        # python -m kubby
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
pyproject.toml       # project config + entry point
build-deb.sh         # developer: build the .deb from source
debian/              # Debian packaging
  control            # package metadata + runtime dependencies
  rules              # build rules (venv bundling via dh)
  kubby.postinst     # downloads kubectl, helm, minikube
  kubby.postrm       # cleanup on purge
```

## Roadmap

Step 1 covers the dependency detection/installation flow; subsequent steps
will add cluster lifecycle (create/start/stop minikube), in-cluster views
(pods, deployments, services), and helm release management.
