import os
import sys
from pathlib import Path

def audit_encoding():
    root = Path(__file__).resolve().parent.parent
    text_exts = {'.py', '.js', '.cjs', '.mjs', '.html', '.css', '.gs', '.json', '.md'}
    ignore_dirs = {'.venv', 'node_modules', '__pycache__', '.pytest_cache', '.git', 'artifacts', '.runtime', 'backups'}

    # Common mojibake signatures resulting from double UTF-8 decoding or cp1252/latin1 decoding of UTF-8
    mojibake_tokens = [
        'Â·',             # Double-encoded middle dot
        'â‚¹',            # Double-encoded Rupee sign
        'Â',              # Strayed lone Latin-1 capital A with circumflex
        'â€”',            # Double-encoded em-dash
        'â€œ',            # Double-encoded left curly quote
        'â€\x9d',         # Double-encoded right curly quote
        'â€™',            # Double-encoded apostrophe
        'Ã¢',             # Double-encoded Latin a circumflex
        'Ã©',             # Double-encoded e acute
        'ï»¿',            # Stray UTF-8 BOM
    ]

    findings = []
    scanned_files = 0
    this_script = Path(__file__).resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith('.tmp')]
        for f in filenames:
            p = Path(dirpath) / f
            if p.resolve() == this_script:
                continue
            if p.suffix.lower() in text_exts:
                scanned_files += 1
                try:
                    content = p.read_text(encoding='utf-8')
                    for tok in mojibake_tokens:
                        if tok in content:
                            findings.append((str(p.relative_to(root)), tok))
                except UnicodeDecodeError as e:
                    findings.append((str(p.relative_to(root)), f"NOT_VALID_UTF8: {e}"))

    print(f"Scanned {scanned_files} files across repository.")
    if findings:
        print(f"FAILED: Found {len(findings)} encoding/mojibake issues:")
        for path, tok in findings:
            print(f"  {path}: {repr(tok)}")
        sys.exit(1)
    else:
        print("PASSED: 100% clean UTF-8. Zero mojibake or double-encoded characters found.")
        sys.exit(0)

if __name__ == "__main__":
    audit_encoding()
