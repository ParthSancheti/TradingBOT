"""Enroll a deployed Apps Script URL using the secret already in the OS vault.

The signing secret is never accepted on the command line or printed. Run this
only after the operator sets JARVIS_SIGNING_SECRET in Script Properties and
deploys the web app.
"""
import argparse
import asyncio
import json
import sqlite3

from jarvis.desktop import runtime_directory
from jarvis.secrets import SecretVault
from jarvis.sheets import AppsScript, validate_script_url


async def connect(url: str):
    vault = SecretVault()
    secret = vault.get("sheets_signing_secret_pending")
    if not secret:
        raise RuntimeError("No pending signing secret is stored in the OS credential manager")
    url = validate_script_url(url)
    current = json.loads(vault.get("sheets_connection") or "{}")
    if current.get("url") and current["url"] != url:
        raise RuntimeError("Use a separate workspace for a different workbook")
    database = runtime_directory() / "workspace.db"
    if not current.get("url") and database.is_file():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute("SELECT 1 FROM sheets_outbox LIMIT 1").fetchone():
                raise RuntimeError("Existing local writes need review before connecting a workbook")
    result = await AppsScript(url, secret).call("health")
    if result.get("schema_version") != 1 or result.get("storage") != "google_sheets":
        raise RuntimeError("The deployed workbook protocol did not verify")
    vault.set("sheets_connection", json.dumps({"url": url, "secret": secret}))
    print("Google Sheets connection verified and saved in the OS credential manager.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Deployed script.google.com/macros/s/.../exec URL")
    asyncio.run(connect(parser.parse_args().url))
