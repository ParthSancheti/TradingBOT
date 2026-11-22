"""Build on the target OS. Windows produces an EXE; macOS produces a native binary."""
import argparse
import subprocess
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=root / "artifacts/native")
output = parser.parse_args().output.resolve()
subprocess.run([sys.executable,"-m","PyInstaller","--noconfirm","--onedir","--name","jarvis-native",
    "--distpath",str(output),"--workpath",str(root/".runtime/pyinstaller"),"--specpath",str(root/".runtime"),
    "--paths",str(root/"backend"),"--collect-submodules","keyring.backends",str(root/"backend/jarvis/native_entry.py")],cwd=root,check=True)
