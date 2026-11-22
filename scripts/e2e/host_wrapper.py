"""Runs the real native-messaging host (jarvis.bridge.main) with a file vault."""
import common  # noqa: F401  (sets sys.path)
from common import FileVault
import jarvis.secrets
jarvis.secrets.SecretVault = FileVault
from jarvis.bridge import main

main()
