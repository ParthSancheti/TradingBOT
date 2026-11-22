"""Shared pieces for the end-to-end bridge harness (no live accounts)."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"


class FileVault:
    """Stands in for the OS keychain; same get/set contract as SecretVault."""
    def __init__(self):
        self.path = Path(os.environ["LOCALAPPDATA"]) / "e2e-vault.json"

    def _load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, name):
        return self._load().get(name)

    def set(self, name, value):
        data = self._load(); data[name] = value
        self.path.write_text(json.dumps(data))
