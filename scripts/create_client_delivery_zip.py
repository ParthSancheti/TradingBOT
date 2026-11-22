import os
import zipfile
from pathlib import Path

def create_client_delivery_zip():
    root = Path(__file__).resolve().parent.parent
    zip_name = "Jarvis-Trade-Client-Release.zip"
    zip_path = root / zip_name

    # Folders to completely exclude for client delivery
    ignore_dirs = {
        ".agent",
        ".planning",
        ".venv",
        "node_modules",
        "artifacts",
        ".runtime",
        ".pytest_cache",
        "__pycache__",
        "backups",
        ".git"
    }

    # Internal blueprint / working docs to exclude
    internal_docs = {
        "JARVIS_TRADE_BLUEPRINT.md",
        "JARVIS_TRADE_MASTER_PROMPT.md",
        "MASTER_BLUEPRINT_V3.md",
        "MASTER_BLUEPRINT_V4.md",
        "EXECUTION_ARCHITECTURE_V2.md",
        "GEMINI_BLUEPRINT_REVIEW.md",
        "PILOT_ACCEPTANCE.md",
        "OWNER_ACCEPTANCE.md",
        "ACCEPTANCE_AND_ROLLOUT.md",
        "RELEASE_VERIFICATION.md",
        "BUILD_STATUS.md",
        "VERIFICATION.md",
        ".env",
        "out.log",
        "err.log",
        "core_logic_audit.txt",
        "bridge_and_ext_audit.txt",
    }
    ignore_exts = {".pyc", ".pyo", ".pyd", ".zip"}

    print(f"Creating clean client delivery zip: {zip_path}")
    count = 0
    total_uncompressed = 0

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".tmp")]
            rel_dir = Path(dirpath).relative_to(root)
            if any(p in ignore_dirs for p in rel_dir.parts):
                continue

            for fname in filenames:
                if fname in internal_docs:
                    continue
                ext = Path(fname).suffix.lower()
                if ext in ignore_exts:
                    continue

                full_path = Path(dirpath) / fname
                rel_path = full_path.relative_to(root)
                if full_path.resolve() == zip_path.resolve():
                    continue

                zf.write(full_path, arcname=str(rel_path))
                count += 1
                total_uncompressed += full_path.stat().st_size

    size = zip_path.stat().st_size
    print(f"Client Release zip created!")
    print(f" -> Total Files: {count}")
    print(f" -> Uncompressed Size: {total_uncompressed / (1024 * 1024):.2f} MB")
    print(f" -> Compressed Size: {size / (1024 * 1024):.2f} MB")
    print(f" -> Path: {zip_path}")

if __name__ == "__main__":
    create_client_delivery_zip()
