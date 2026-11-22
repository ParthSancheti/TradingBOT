"""Phase 1 Messaging Vertical Slice Verification.

Validates:
1. Composer outbound creation, deduplication by client_request_id, and SQLite persistence.
2. Manual sending policy: allows non-source buyers while keeping autonomous quotes restricted to allowed_phones.
3. Zero double-send mutex: atomic claim leases prevent concurrent extension and direct transports from double-dispatching.
4. Batch intake: wa_sync processes message batches beyond the legacy 20-limit.
5. Multi-item quotation arithmetic: computes total price based on requested quantity, not warehouse total.
6. Owner self-command binding: @Jarvis self-chat responses are bound to owner account and recipient.
"""

import time
import pytest
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.desktop_service import DesktopService
from jarvis.runner import Runner
from jarvis.trading_engine import compose_multi_item_quote, handle_agent_command


class MockSheets:
    def __init__(self):
        self.ops = []

    def prepare(self, doc_id, rev, ops):
        return {"status": "ok", "prepared": True}

    def commit(self, doc_id, rev, token):
        return {"status": "ok", "committed": True}

    def pull(self, doc_id, rev):
        return {"records": [], "revision": rev}


class MockVault:
    def __init__(self):
        self.secrets = {"bridge_secret": "test-bridge-secret"}

    def get(self, key):
        return self.secrets.get(key)

    def set(self, key, val):
        self.secrets[key] = val


@pytest.fixture
def repo(tmp_path):
    db_path = tmp_path / "test_slice.db"
    return SQLiteSyncedRepository(MockSheets(), db_path)


@pytest.fixture
def app(tmp_path, repo):
    vault = MockVault()
    runner = Runner(vault, repo)
    service = DesktopService(vault, repo, runner, cache_path=tmp_path / "cache.json")
    return service


@pytest.mark.asyncio
async def test_composer_create_dedupe_and_idempotency(app, repo):
    owner = "+919876543210"
    buyer = "+919123456789"
    await app.save_setup({"owner_phone": owner, "stock_phones": ["+919405615135"]})
    await app.start({})
    app.runner.autonomy_mode = "send"

    # 1. First submission with client_request_id
    res1 = await app.outbound_create({
        "recipient_phone": buyer,
        "text": "Hello buyer, stock is confirmed.",
        "approve_now": True,
        "client_request_id": "req-12345",
    })
    assert res1["approved"] is True
    assert res1["status"] == "READY"
    oid1 = res1["id"]

    # 2. Duplicate submission with same client_request_id
    res2 = await app.outbound_create({
        "recipient_phone": buyer,
        "text": "Hello buyer, stock is confirmed.",
        "approve_now": True,
        "client_request_id": "req-12345",
    })
    assert res2["id"] == oid1

    # Verify only 1 outbound item exists in database
    ready_items = repo.list_outbound(["READY"])
    assert len(ready_items) == 1
    assert ready_items[0]["id"] == oid1


@pytest.mark.asyncio
async def test_manual_send_allows_non_source_while_auto_restricts(repo):
    owner = "+919876543210"
    source_supplier = "+919405615135"
    buyer = "+919999888877"  # Not a supplier
    allowed_sources = [source_supplier]
    sess = "test_worker_session_012345"

    # 1. Manual message to non-source buyer
    manual_oid = repo.queue_outbound(
        buyer[1:] + "@c.us",
        ["Manual quote for customer"],
        kind="manual",
        recipient_phone=buyer,
        account_phone=owner,
        status="DRAFT",
    )
    # Approve manual message
    err = repo.approve_outbound(manual_oid, None, owner, allowed_phones=None, manual_only=True)
    assert err is None

    # Worker claim: manual message can be claimed and authorized even when allowed_phones excludes buyer
    claim = repo.dispatch_outbound("dispatch_claim", {"id": manual_oid, "session_id": sess}, owner, allowed_sources, True, manual_only=True)
    assert "item" in claim
    assert claim["item"]["id"] == manual_oid
    token = claim["item"]["claim_token"]

    auth = repo.dispatch_outbound("dispatch_authorize", {"id": manual_oid, "claim_token": token, "session_id": sess}, owner, allowed_sources, True)
    assert auth.get("ok") is True

    # 2. Autonomous quote to non-source buyer
    auto_oid = repo.queue_outbound(
        buyer[1:] + "@c.us",
        ["Autonomous AI quote"],
        kind="quote",
        recipient_phone=buyer,
        account_phone=owner,
        status="READY",
    )
    # Worker claim for autonomous quote: must fail because buyer is not in allowed_sources
    claim_auto = repo.dispatch_outbound("dispatch_claim", {"id": auto_oid, "session_id": sess}, owner, allowed_sources, True, manual_only=False)
    assert claim_auto.get("error") == "RECIPIENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_dual_worker_claim_mutex(repo):
    """Ensures two workers (e.g. extension and Baileys) cannot claim the same message concurrently."""
    owner = "+919876543210"
    buyer = "+919123456789"
    oid = repo.queue_outbound(
        buyer[1:] + "@c.us",
        ["Single delivery test"],
        kind="manual",
        recipient_phone=buyer,
        account_phone=owner,
        status="READY",
    )
    sess1 = "extension_worker_session_111"
    sess2 = "direct_baileys_session_222"

    # Worker 1 (Extension) claims
    claim1 = repo.dispatch_outbound("dispatch_claim", {"id": oid, "session_id": sess1}, owner, None, True)
    assert "item" in claim1
    assert claim1["item"]["id"] == oid
    token1 = claim1["item"]["claim_token"]

    # Worker 2 (Direct Baileys) attempts to claim concurrently
    claim2 = repo.dispatch_outbound("dispatch_claim", {"id": oid, "session_id": sess2}, owner, None, True)
    assert claim2.get("error") == "APPROVAL_UNAVAILABLE"

    # Worker 1 authorizes and completes send
    auth = repo.dispatch_outbound("dispatch_authorize", {"id": oid, "claim_token": token1, "session_id": sess1}, owner, None, True)
    assert auth.get("ok") is True

    result = repo.dispatch_outbound(
        "dispatch_result",
        {"id": oid, "outcome": "ACCEPTED", "claim_token": token1, "session_id": sess1, "sent_ids": ["wpp_msg_999"]},
        owner, None, True
    )
    assert result.get("ok") is True

    # State in DB is now ACCEPTED
    row = repo.list_outbound(["ACCEPTED"])[0]
    assert row["id"] == oid
    assert row["meta"]["sent_ids"] == ["wpp_msg_999"]


@pytest.mark.asyncio
async def test_direct_intake_no_truncation(app, repo):
    """Batch intake via wa_sync handles >20 events without truncation."""
    owner = "+919876543210"
    await app.save_setup({"owner_phone": owner, "stock_phones": ["+919405615135"]})

    events = []
    for i in range(35):
        events.append({
            "message_id": f"evt-{i}",
            "conversation_id": f"+9198000000{i:02d}@c.us",
            "phone": f"+9198000000{i:02d}",
            "text": f"Looking for 10 pcs iPhone 15 Pro #{i}",
            "timestamp": time.time() + i,
        })

    res = await app.wa_sync({"events": events})
    assert res.get("ok") is True
    assert len(res.get("results", [])) == 35


def test_multi_item_quote_uses_requested_quantity_not_warehouse_stock():
    """Arithmetical correctness: total USD must multiply requested quantity, not available quantity."""
    evaluations = [
        {
            "buy_item": {"device": "iPhone 15 Pro", "variant": "256GB", "quantity": 5},
            "match": {
                "item": {"device": "iPhone 15 Pro", "variant": "256GB", "quantity": 500, "condition": "NON_ACTIVE"},
                "is_exact": True,
                "is_partial": False,
            },
            "pricing": {"quote_usd": "1000.00"},
        },
        {
            "buy_item": {"device": "MacBook Pro M3", "variant": "16/512", "quantity": 2},
            "match": {
                "item": {"device": "MacBook Pro M3", "variant": "16/512", "quantity": 50, "condition": "NON_ACTIVE"},
                "is_exact": True,
                "is_partial": False,
            },
            "pricing": {"quote_usd": "1500.00"},
        },
    ]

    parts = compose_multi_item_quote(evaluations)
    # Expected total: (5 * 1000) + (2 * 1500) = 5000 + 3000 = $8,000.00
    # Prior bug used (500 * 1000) + (50 * 1500) = $575,000.00
    assert len(parts) >= 2
    assert "Total: $8,000.00 USD" in parts[1]
    assert "5 pcs ready" in parts[0]
    assert "2 pcs ready" in parts[0]


@pytest.mark.asyncio
async def test_owner_self_command_binding(repo, tmp_path):
    """Self-chat commands (@Jarvis ...) are queued with owner account and recipient, ready for dispatch."""
    owner = "+919876543210"
    repo.set_local_state("owner_profile", {"owner_phone": owner, "phone": owner, "name": "Boss"})
    vault = MockVault()

    # Create dummy banner broadcast pending
    await repo.put("tasks", "pending-banner-campaign", {
        "state": "AWAITING_CONFIRM",
        "recipients": [{"conversation_id": "buyer1@c.us", "phone": "+919999111122"}],
        "caption": "Special Diwali Deal",
        "filename": "deal.png",
        "data_url": "data:image/png;base64,QUJD",
    }, 0)

    res = await handle_agent_command(repo, vault, "@Jarvis SEND", f"{owner[1:]}@c.us", runtime_dir=tmp_path)
    assert res["handled"] == "broadcast_send"

    ready = repo.ready_outbound_ids(owner)
    # Both the campaign broadcast recipient and the self-reply must be ready and claimable for owner
    assert len(ready) >= 2
    full_rows = [repo.get_outbound(r["id"]) for r in ready]
    assert any(row["kind"] == "agent_reply" for row in full_rows)
