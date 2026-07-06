# kubby

A GUI tool for managing local Kubernetes cluster resources.

Start a minikube cluster, watch pods come up, install kubectl/helm/podman from
the Docs page — all from one window.

## Install

The shipped artifact is a single self-contained binary (PyInstaller --onefile).
No Python or pip required on the host. The binary **does** need a few system
libraries to drive the GTK webview:

```bash
# Debian / Ubuntu 24.04+
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
# (Debian 11 / Ubuntu 20.04–22.04: use gir1.2-webkit2-4.0 instead)

# Fedora / RHEL
sudo dnf install -y python3-gobject gtk3 webkit2gtk4.1

# Arch
sudo pacman -S --needed python-gobject gtk3 webkit2gtk-4.1
```

If those packages are missing, `kubby` prints a per-distro one-liner and exits
before trying to open a window.

### Pre-built binary

Download the latest `kubby` binary from the releases page, then:

```bash
chmod +x kubby
./kubby                  # launch the GUI
./kubby --check          # print tool status (no GUI)
./kubby --debug          # enable DevTools (right-click → Inspect)
```

### Install the managed CLIs

The **Docs** tab in the GUI has an **Install** button for every managed CLI
(minikube, kubectl, helm, podman). Each one runs the tool's official upstream
installer with elevation (pkexec on a desktop session, sudo as a fallback):

| Tool      | Source                                                                |
|-----------|-----------------------------------------------------------------------|
| minikube  | `minikube.sigs.k8s.io/scripts/install.sh`                             |
| helm      | `get-helm-3` from `raw.githubusercontent.com/helm/helm/main/scripts/` |
| kubectl   | latest stable binary from `dl.k8s.io` → `/usr/local/bin/kubectl`      |
| podman    | host package manager (apt / dnf / pacman / zypper)                    |

The button doubles as upgrade — it is always enabled when no other job is in
flight. The button streams the installer's output into a global log panel at
the bottom of the window; on success the card flips to **installed**, on
failure the panel stays visible with diagnostics so you can read what went
wrong.

## Usage

| Command       | Effect                                                 |
|---------------|--------------------------------------------------------|
| `kubby`       | Launch the GUI                                         |
| `kubby --check` | Print install status for all managed tools, then exit |
| `kubby --debug` | Launch the GUI with DevTools enabled (right-click → Inspect in the webview) |

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
uv run -m kubby.app           # run from source
uv run -m kubby.app --check   # print tool status
```

### Build the binary

```bash
pip install pyinstaller   # one-time
./build-binary.sh         # output: dist/kubby
```

`build-binary.sh` is a thin wrapper around `pyinstaller --noconfirm kubby.spec`.
See `kubby.spec` for what gets bundled (UI assets, hidden webview / gi
imports, onefile mode).

## Requirements

- Linux desktop session with a display server (X11 or Wayland)
- The OS packages listed in the **Install** section above
- For building the binary: `pyinstaller` (any recent version)
- For running from source: Python 3.11+ and `uv` (or `pip install pywebview`)

## Layout

```
kubby/
  __init__.py
  __main__.py             # python -m kubby
  app.py                  # pywebview window + JS API
  installer/
    tools.py              # registry of managed tools + install scripts
    detector.py           # PATH + version detection
    linux.py              # package manager + elevation (pkexec / sudo)
  ui/
    index.html            # frontend shell
    app.js                # frontend logic
    style.css             # dark webview theme
kubby.spec                # PyInstaller spec (onefile build)
build-binary.sh           # developer: produce dist/kubby
pyproject.toml            # project config (deps: pywebview)
```

## Roadmap

Step 1 covers the dependency detection/installation flow and the cluster
overview (nodes, namespaces, pods). Subsequent steps add helm release
management, in-cluster resource editing, and multi-cluster support.
