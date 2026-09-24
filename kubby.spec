# -*- mode: python ; coding: utf-8 -*-
#
# kubby.spec — build a single-file kubby binary via PyInstaller.
#
#   pyinstaller --noconfirm --clean kubby.spec
#
# Output: dist/kubby (onefile, console attached).
#
# kubby is Linux-only per the plan (Ubuntu 24.04+ / Debian / Fedora / Arch).
# Since the Textual TUI replaced the web GUI there are no native GUI
# bindings left to bundle — the TUI is pure Python and talks to the
# terminal directly.
#
# Path resolution in kubby/app.py uses sys._MEIPASS when frozen
# (PyInstaller sets this to the temp extraction directory). The datas
# entry below keeps the TUI stylesheet at `kubby/tui/styles.tcss` inside
# the bundle, so `KubbyApp.CSS_PATH = "styles.tcss"` — which Textual
# resolves relative to the file that defines the App class — finds it.

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['kubby/app.py'],
    pathex=[],
    binaries=[],
    datas=[
        # (source, destination-in-bundle)
        ('kubby/tui/styles.tcss', 'kubby/tui'),
        # Textual ships a handful of non-Python files (tree-sitter query
        # files used by its code editor widget). Harmless to bundle, and
        # it keeps the frozen build identical to the source tree if we
        # ever use those widgets.
        *collect_data_files('textual'),
    ],
    hiddenimports=[
        # setuptools >= 70 no longer vendors jaraco; pkg_resources imports
        # them dynamically. PyInstaller's static analysis can't see these
        # imports, so we must list them explicitly (PYI-5550).
        'jaraco.text',
        'jaraco.functools',
        'jaraco.context',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# onefile: PYZ + scripts + binaries + datas all packed into a single ELF.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='kubby',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # The terminal IS the UI: kubby renders into it, and `kubby --check`
    # plus any startup error message print here too.
    console=True,
    disable_windowed_traceback=False,
)
