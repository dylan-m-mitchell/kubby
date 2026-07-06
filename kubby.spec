# -*- mode: python ; coding: utf-8 -*-
#
# kubby.spec — build a single-file kubby binary via PyInstaller.
#
#   pyinstaller --noconfirm --clean kubby.spec
#
# Output: dist/kubby (~30-50 MB, depending on how many platform libs the
# dynamic GTK backend pulls in).
#
# kubby is Linux-only per the plan (Ubuntu 24.04+ / Debian / Fedora / Arch);
# the spec only bundles the GTK backend and the GObject introspection
# bindings the GTK backend touches at runtime. If we ever ship
# cross-platform, add the relevant `webview.platforms.*` entries to
# hiddenimports.
#
# Path resolution in kubby/app.py is `Path(__file__).resolve().parent / "ui"`.
# Inside the frozen binary PyInstaller overwrites __file__ to point at the
# extracted bundle under sys._MEIPASS, so the relative path still resolves
# correctly — we just need to make sure the UI assets are present in the
# bundle (see `datas` below).

a = Analysis(
    ['kubby/app.py'],
    pathex=[],
    binaries=[],
    datas=[
        # (source, destination-in-bundle)
        ('kubby/ui/index.html', 'kubby/ui'),
        ('kubby/ui/app.js',     'kubby/ui'),
        ('kubby/ui/style.css',  'kubby/ui'),
    ],
    hiddenimports=[
        # pywebview loads its platform backend dynamically via
        # `webview.guilib.import_gtk()`. PyInstaller's static analysis
        # can't see that import, so we force it in. Linux-only.
        'webview.platforms.gtk',
        # PyGObject introspection: gi is the Python wrapper, gi.repository
        # is where the dynamic native bindings live. PyInstaller's
        # introspection hooks (in the pyinstaller-hooks-contrib package)
        # usually catch these, but listing them here makes the dependency
        # explicit and survives hook-path changes.
        'gi',
        'gi.repository',
        'gi.repository.Gtk',
        'gi.repository.WebKit2',
        'gi.repository.GLib',
        'gi.repository.GObject',
        'gi.repository.Pango',
        'gi.repository.Gdk',
        'gi.repository.Gio',

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
    # Keep the console attached. kubby is a GUI app but it also supports
    # `kubby --check` (print tool status) and the missing-GUI-deps hint
    # needs to land in the terminal. A windowed (console=False) build
    # would swallow that output on Windows / macOS.
    console=True,
    disable_windowed_traceback=False,
)
