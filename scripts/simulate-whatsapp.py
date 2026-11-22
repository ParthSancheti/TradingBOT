"""Run a synthetic WhatsApp intake and supervised-send walkthrough.

This simulator uses a temporary SQLite database, in-memory remote repository,
fake bridge inputs, and a fake provider receipt. It never opens WhatsApp or
contacts Google Sheets/a provider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.desktop_service import DesktopService
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.pipeline import IntakePipeline
from jarvis.providers import Triage
from jarvis.runner import Runner


OWNER = "+919876543210"
SOURCE = "+14155552671"
SESSION = "synthetic-session-0001"


class MemoryRemote:
    """A deliberately inert remote: only local SQLite is exercised."""

    async def get(self, collection, record_id):
        return None

    async def list(self, collection, limit=100):
        return []

    async def put(self, *args, **kwargs):
        raise AssertionError("simulator attempted remote write")

    async def batch(self, *args, **kwargs):
        raise AssertionError("simulator attempted remote batch")


class FakeBridge:
    ready = False
    last_seen = 0

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False


class FakeVault:
    def get(self, key):
        return "synthetic-bridge-secret" if key == "bridge_secret" else None

    def set(self, key, value):
        return None


class FakeClassifier:
    async def classify(self, text):
        return Triage(category="BUY", proceed=True, needs_review=False, reason="Synthetic WTB fixture")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def simulate(workdir: Path) -> list[dict]:
    stages: list[dict] = []
    db_path = workdir / "simulated-workspace.db"
    remote = MemoryRemote()
    sql = SQLiteSyncedRepository(remote, db_path)
    runner = Runner(None, sql)
    runner.bridge = FakeBridge()
    service = DesktopService(vault=FakeVault(),
                             repository=sql, runner=runner,
                             opener=lambda **_: None,
                             cache_path=workdir / "cache.json")
    try:
        # Allowlist-only scenario: capture configured sources only (lead capture off).
        await service.save_setup({"owner_phone": OWNER, "stock_phones": [SOURCE], "scan_all": False, "lead_capture": False})
        # Settings are needed to turn on the supervised draft dispatch path.
        await service.settings_save({"autonomy_mode": "draft"})
        await service.start({})
        require(runner.active and runner.can_send, "runner did not enter supervised draft mode")
        stages.append({"stage": "setup", "result": "synthetic account and source configured; runner active"})

        event = {
            "conversation_id": SOURCE[1:] + "@c.us",
            "sender_id": SOURCE[1:] + "@c.us",
            "phone": SOURCE,
            "sender_name": "Synthetic Supplier",
            "message_id": "false_" + SOURCE[1:] + "@c.us_SIM001",
            "text": "WTB Samsung S25 12/256",
            "identity_verified": True,
        }
        captured_at = time.perf_counter()
        observed = await runner._handle({"operation": "observations", "events": [event]})
        local_capture_ms = round((time.perf_counter() - captured_at) * 1000, 2)
        require(observed.get("results", [{}])[0].get("status") == "CLASSIFIED",
                "explicit WTB was not classified locally")
        duplicate = await runner._handle({"operation": "observations", "events": [event]})
        require(duplicate.get("results", [{}])[0].get("status") == "DUPLICATE",
                "repeat observation was not deduplicated")
        messages = await sql.list("messages", 10)
        require(len(messages) == 1 and messages[0]["data"]["classification_source"] == "local",
                "message deduplication or review isolation failed")
        require(await sql.list("offers", 10) == [], "unclassified intake created an offer")
        stages.append({"stage": "intake", "result": "captured and classified once in SQLite without a model request",
                       "local_capture_ms": local_capture_ms})
        outside = await runner._handle({"operation": "observations", "events": [{**event, "phone": OWNER}]})
        # The owner number is rejected even earlier (IGNORED_OWNER); both mean not admitted.
        require(outside["results"][0]["status"] in ("IGNORED_SOURCE", "IGNORED_OWNER"), "non-allowlisted event was admitted")
        ambiguous = {**event, "message_id": event["message_id"] + "B", "text": "WTB a replacement for that Samsung"}
        queued = await runner._handle({"operation": "observations", "events": [ambiguous]})
        require(queued["results"][0]["status"] == "CAPTURED_REVIEW", "ambiguous request did not remain in review")
        processed = await IntakePipeline(sql, FakeClassifier()).process(ambiguous, runner.cached_setup)
        require(processed["status"] == "CLASSIFIED", "ambiguous request did not reach the fallback classifier")
        require(len(await sql.list("requests", 10)) == 2, "each WTB should create one request")
        require(await sql.list("inventory", 10) == [], "synthetic WTB mutated stock")
        stages.append({"stage": "classification", "result": "local and fallback classification each produced one buy request; inventory stayed empty"})

        draft_result = await service.dispatch({"id": 1, "method": "outbound/create", "params": {
            "recipient_phone": SOURCE, "text": "Synthetic quote for review",
        }})
        outbound_id = draft_result["result"]["id"]
        before = sql.list_outbound(("DRAFT", "READY", "CLAIMED", "IN_FLIGHT", "ACCEPTED"), 10)
        require(any(row["id"] == outbound_id and row["status"] == "DRAFT" for row in before),
                "outbound draft was not isolated pending approval")
        draft = next(row for row in before if row["id"] == outbound_id)
        refusal = sql.dispatch_outbound("dispatch_claim", {
            "outbound_id": outbound_id, "session_id": SESSION, "account_phone": OWNER,
        }, OWNER, {SOURCE}, True)
        require(refusal.get("error") == "APPROVAL_UNAVAILABLE", "draft was claimable without explicit approval")
        approval = await service.dispatch({"id": 2, "method": "outbound/action", "params": {
            "id": outbound_id, "action": "approve", "expected_updated_at": draft["updated_at"],
        }})
        require("result" in approval, "explicit approval failed")
        wrong_account = await runner._handle({"operation": "dispatch_claim", "outbound_id": outbound_id,
                                              "session_id": SESSION, "account_phone": SOURCE})
        require(wrong_account.get("error") == "ACCOUNT_MISMATCH", "wrong account could claim the approved draft")
        poll = await runner._handle({"operation": "poll_config", "dispatch_version": 1})
        require(poll["outbound"] == [{"id": outbound_id}], "poll did not return the approved ID alone")
        stages.append({"stage": "approval", "result": "draft was not claimable before explicit owner approval"})

        claim = await runner._handle({"operation": "dispatch_claim", "outbound_id": outbound_id,
                                      "session_id": SESSION, "account_phone": OWNER})
        item = claim.get("item")
        require(item and item.get("claim_token"), "approved draft could not be claimed")
        auth_body = {"operation": "dispatch_authorize", "outbound_id": outbound_id,
                     "session_id": SESSION, "account_phone": OWNER,
                     "claim_token": item["claim_token"]}
        authorized = await runner._handle(auth_body)
        require(authorized.get("ok") is True, "claimed draft was not authorized")
        second_auth = await runner._handle(auth_body)
        require(second_auth.get("error") == "APPROVAL_UNAVAILABLE",
                "same claim received duplicate send authorization")
        stages.append({"stage": "dispatch", "result": "one claim authorized; repeated authorization rejected"})

        receipt = "synthetic-provider-message-001"
        result_body = {"operation": "dispatch_result", "outbound_id": outbound_id,
                       "session_id": SESSION, "claim_token": item["claim_token"],
                       "outcome": "ACCEPTED", "sent_ids": [receipt]}
        result = await runner._handle(result_body)
        require(result.get("ok") is True, "fake dispatch receipt was not accepted")
        repeated = await runner._handle(result_body)
        require(repeated.get("ok") is True, "idempotent repeated receipt was not accepted")
        final = sql.list_outbound(("ACCEPTED",), 10)
        require(len(final) == 1 and final[0]["id"] == outbound_id
                and final[0]["meta"].get("sent_ids") == [receipt],
                "receipt did not persist exactly once")
        stages.append({"stage": "receipt", "result": "synthetic provider receipt persisted exactly once"})

        stopped_id = (await service.outbound_create({"recipient_phone": SOURCE, "text": "Pending stop check"}))["id"]
        stopped_draft = next(row for row in sql.list_outbound(("DRAFT",)) if row["id"] == stopped_id)
        await service.outbound_action({"id": stopped_id, "action": "approve", "expected_updated_at": stopped_draft["updated_at"]})
        stopped_claim = await runner._handle({"operation": "dispatch_claim", "outbound_id": stopped_id,
                                             "session_id": SESSION, "account_phone": OWNER})
        await service.stop({})
        stopped = await runner._handle({**auth_body, "outbound_id": stopped_id,
                                       "claim_token": stopped_claim["item"]["claim_token"]})
        require(stopped.get("error") == "DISPATCH_DISABLED", "Stop did not revoke pending dispatch")
        stages.append({"stage": "stop", "result": "Stop revoked an unstarted dispatch"})

        await service.start({})
        uncertain_id = (await service.outbound_create({"recipient_phone": SOURCE, "text": "Unknown receipt check"}))["id"]
        uncertain_draft = next(row for row in sql.list_outbound(("DRAFT",)) if row["id"] == uncertain_id)
        await service.outbound_action({"id": uncertain_id, "action": "approve", "expected_updated_at": uncertain_draft["updated_at"]})
        uncertain_claim = await runner._handle({"operation": "dispatch_claim", "outbound_id": uncertain_id,
                                               "session_id": SESSION, "account_phone": OWNER})
        uncertain_args = {**auth_body, "outbound_id": uncertain_id,
                          "claim_token": uncertain_claim["item"]["claim_token"]}
        require((await runner._handle(uncertain_args)).get("ok") is True, "uncertain case did not authorize")
        unknown = await runner._handle({**uncertain_args, "operation": "dispatch_result", "outcome": "UNKNOWN"})
        require(unknown.get("ok") is True, "ambiguous result was not stored")
        require((await runner._handle(uncertain_args)).get("error") == "APPROVAL_UNAVAILABLE",
                "ambiguous result could be resent")
        require(any(row["id"] == uncertain_id for row in sql.list_outbound(("UNKNOWN",))),
                "ambiguous result did not persist")
        stages.append({"stage": "ambiguous receipt", "result": "UNKNOWN persisted and could not receive another send authorization"})

        require(db_path.exists() and db_path.parent == workdir,
                "SQLite database escaped simulator scratch directory")
        stages.append({"stage": "isolation", "result": "SQLite and cache confined to temporary simulator directory; no remote writes"})
        return stages
    finally:
        service.close()
        # DesktopService closes its cache wrapper; close the injected SQLite
        # repository explicitly so Windows can remove the temporary database.
        sql.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-dir", type=Path,
                        help="Use this scratch directory (it must be empty); default is a fresh temporary directory")
    args = parser.parse_args()
    if args.scratch_dir:
        scratch = args.scratch_dir.resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        if any(scratch.iterdir()):
            parser.error("--scratch-dir must be empty")
        stages = asyncio.run(simulate(scratch))
    else:
        with tempfile.TemporaryDirectory(prefix="jarvis-wa-sim-") as directory:
            stages = asyncio.run(simulate(Path(directory)))
    print(json.dumps({"simulation": "PASS", "stages": stages}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"simulation": "FAIL", "error": str(error)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
