"""Run the existing offline suites and save reproducible results. No live accounts.

Usage: .venv/Scripts/python scripts/test-project.py [--advisories]
The optional advisory check queries public package advisory services.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advisories", action="store_true", help="Also run pip-audit (requires internet)")
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    report_dir = ROOT / ".runtime" / "test-reports" / started.strftime("%Y%m%dT%H%M%S%fZ")
    report_dir.mkdir(parents=True, exist_ok=False)

    # App imports must not initialize the operator's deployed security database or
    # adopt live OIDC settings from .env. Tests supply their own fake repositories.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_")}
    environment.update({
        "JARVIS_ENVIRONMENT": "development",
        "JARVIS_PUBLIC_ORIGIN": "http://127.0.0.1:8765",
        "JARVIS_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "JARVIS_DATABASE_AUTO_CREATE": "true",
        "JARVIS_OIDC_ISSUER": "", "JARVIS_OIDC_CLIENT_ID": "",
        "JARVIS_OIDC_CLIENT_SECRET": "", "JARVIS_ADMIN_SUBJECT": "",
        "JARVIS_ACCEPTED_ACR": "[]",
    })
    node = shutil.which("node")
    node_tests = sorted([*ROOT.glob("apps-script/*.test.cjs"), *ROOT.glob("extension/*.test.mjs"), *ROOT.glob("desktop/*.test.cjs")])
    stages = [("python-tests", [sys.executable, "-m", "pytest", "-q", f"--junitxml={report_dir / 'python-tests.xml'}"])]
    stages.append(("node-tests", [node or "node", "--test", *[str(p.relative_to(ROOT)) for p in node_tests]]))
    for name in ("backend/jarvis/web/app.js", "backend/jarvis/web/onboarding.js", "backend/jarvis/web/intake.js", "extension/background.js", "extension/status.js", "extension/scanner.mjs", "extension/boundary.mjs", "extension/actions.mjs", "desktop/main.cjs", "desktop/renderer/views.js", "desktop/scripts/package.cjs"):
        stages.append(("syntax-" + Path(name).name.replace(".", "-"), [node or "node", "--check", name]))
    stages.append(("handoff-links", [sys.executable, "scripts/verify-research-pack.py"]))
    stages.append(("whatsapp-simulation", [sys.executable, "scripts/simulate-whatsapp.py"]))
    # Real extension worker -> real native host -> real desktop bridge/pipeline/SQLite.
    if os.name != "nt":
        stages.append(("bridge-e2e", [node or "node", "scripts/e2e/run_e2e.mjs"]))
    if args.advisories:
        stages.append(("dependency-advisories", [sys.executable, "-m", "pip_audit"]))

    results = []
    for name, command in stages:
        before = time.monotonic()
        print(f"Running {name}...", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=180, encoding="utf-8", errors="replace")
            code, output = result.returncode, result.stdout
        except FileNotFoundError:
            code, output = 127, "Required executable was not found. Install the documented prerequisites.\n"
        except subprocess.TimeoutExpired as error:
            code = 124
            captured = error.stdout or ""
            output = (captured.decode("utf-8", errors="replace") if isinstance(captured, bytes) else captured) + "\nTest stage exceeded 180 seconds.\n"
        (report_dir / f"{name}.log").write_text(output, encoding="utf-8")
        state = "PASS" if code == 0 else "FAIL"
        results.append({"name": name, "status": state, "exit_code": code,
            "elapsed_seconds": round(time.monotonic() - before, 3), "log": f"{name}.log", "command": command})
        print(f"{state}: {name}", flush=True)

    success = all(stage["status"] == "PASS" for stage in results)
    summary = {"scope": "offline suites; includes explicit test doubles; not a production certification",
        "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0], "platform": sys.platform,
        "result": "PASS" if success else "FAIL", "live_account_tests": "NOT_RUN",
        "stages": results}
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nOffline result: {summary['result']}\nReports: {report_dir}\nLive-account tests: NOT RUN", flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
