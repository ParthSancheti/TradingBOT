"""Package source and specifications without credentials, runtime data or installed libraries."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=ROOT / "Jarvis-Trade-Pilot-Source.zip")
OUTPUT = parser.parse_args().output
files = [*ROOT.glob("*.md"), *ROOT.glob("Start Jarvis*.cmd"), ROOT/".env.example", ROOT/".gitignore", ROOT/"pyproject.toml", ROOT/"requirements.lock"]
ignored = {"__pycache__", "node_modules", ".runtime", ".venv", ".git"}
for name in ("backend/jarvis", "backend/tests", "desktop", "extension", "apps-script", "research", "specs", "scripts"):
    for folder, directories, filenames in (ROOT / name).walk():
        directories[:] = [directory for directory in directories if directory not in ignored]
        files.extend(folder / file for file in filenames
                     if not file.startswith(".env") and Path(file).suffix.lower() not in
                     {".pyc", ".db", ".sqlite3", ".log", ".pem", ".key", ".exe", ".zip"})
with zipfile.ZipFile(OUTPUT,"w",zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        archive.write(path, path.relative_to(ROOT).as_posix())
with zipfile.ZipFile(OUTPUT) as archive:
    assert archive.testzip() is None
    assert not any(p.startswith((".venv/", ".runtime/")) or p == ".env" or p.endswith((".pem", ".key", ".sqlite3")) for p in archive.namelist())
    print(f"Verified source archive: {len(archive.namelist())} files; no runtime secrets/data included")
