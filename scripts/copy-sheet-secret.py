"""Copy the pending Apps Script signing secret from the OS vault to clipboard."""
import subprocess

from jarvis.secrets import SecretVault

secret = SecretVault().get("sheets_signing_secret_pending")
if not secret:
    raise SystemExit("No pending signing secret is stored")
subprocess.run([r"C:\Windows\System32\clip.exe"], input=secret.encode("utf-8"), check=True)
print("Signing secret copied to clipboard; value not printed.")
