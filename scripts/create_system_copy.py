import os
import sys
import zipfile
from pathlib import Path

def create_system_copy():
    root = Path(__file__).resolve().parent.parent
    zip_name = "Jarvis-Trade-System-Copy.zip"
    zip_path = root / zip_name

    # Folders to completely ignore
    ignore_dirs = {
        ".venv",
        "node_modules",
        "artifacts",
        ".runtime",
        ".pytest_cache",
        "__pycache__",
        "backups",
        ".git"
    }

    # File extensions / names to ignore
    ignore_files = {
        ".env",       # Protect local secrets / API keys
        "out.log",
        "err.log",
    }
    ignore_exts = {".pyc", ".pyo", ".pyd", ".zip"}

    print(f"Creating system copy zip archive at: {zip_path}")
    added_count = 0
    total_uncompressed_bytes = 0

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories in-place so os.walk does not recurse into them
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".tmp")]

            rel_dir = Path(dirpath).relative_to(root)
            # Skip root itself if it matches any ignore
            parts = rel_dir.parts
            if any(p in ignore_dirs for p in parts):
                continue

            for fname in filenames:
                if fname in ignore_files:
                    continue
                ext = Path(fname).suffix.lower()
                if ext in ignore_exts:
                    continue

                full_path = Path(dirpath) / fname
                rel_path = full_path.relative_to(root)

                # Avoid adding the zip file itself if already created
                if full_path.resolve() == zip_path.resolve():
                    continue

                zf.write(full_path, arcname=str(rel_path))
                added_count += 1
                total_uncompressed_bytes += full_path.stat().st_size

    compressed_size = zip_path.stat().st_size
    print(f"Archive created successfully!")
    print(f" -> Total Files: {added_count}")
    print(f" -> Uncompressed Size: {total_uncompressed_bytes / (1024 * 1024):.2f} MB")
    print(f" -> Compressed Size: {compressed_size / (1024 * 1024):.2f} MB")
    print(f" -> Archive Path: {zip_path}")

if __name__ == "__main__":
    create_system_copy()
