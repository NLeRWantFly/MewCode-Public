# PyInstaller one-dir sidecar. Build from the repository root:
# pyinstaller client/mewcode-backend.spec --distpath client/dist
from pathlib import Path
import subprocess
import sys
from PyInstaller.utils.hooks import collect_all

spec_location = Path(SPECPATH)
spec_dir = spec_location if spec_location.is_dir() else spec_location.parent
root = spec_dir.parent

# All sidecar entry points, including direct PyInstaller builds, run this gate
# before analysis or output generation. It needs development dependencies.
subprocess.run([sys.executable, str(root / "scripts" / "check_startup.py")],
               cwd=root, check=True)

pdf_data, pdf_binaries, pdf_imports = collect_all("pypdfium2")
raw_data, raw_binaries, raw_imports = collect_all("pypdfium2_raw")

a = Analysis(
    [str(root / "frontend" / "server.py")],
    pathex=[str(root)],
    binaries=pdf_binaries + raw_binaries,
    datas=[
        (str(root / "frontend" / "static"), "frontend/static"),
        # importlib.resources needs both the package marker and Markdown
        # definitions in frozen builds.  Keeping the whole directory also
        # makes newly added built-ins available without changing this spec.
        (str(root / "mewcode" / "agents" / "builtins"), "mewcode/agents/builtins"),
        (str(root / "mewcode" / "skills" / "builtins"), "mewcode/skills/builtins"),
    ] + pdf_data + raw_data,
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "PIL.Image", "pypdf",
        "docx", "openpyxl",
    ] + pdf_imports + raw_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mewcode-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Electron launches the process with windowsHide=true, while keeping
    # stdout available for the machine-readable ready line and diagnostics.
    console=True,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name="backend")
