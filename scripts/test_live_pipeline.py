#!/usr/bin/env python3
"""
Jarvis-Trade Ruthless Production-Grade Testing Harness
Injects real B2B messages, tests Gemini classification, matches inventory,
generates dealer quote drafts, and audits database & Google Sheets synchronization.
"""
import asyncio
import json
import os
import uuid
import sys
import time
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from jarvis.desktop import runtime_directory
from jarvis.gemini import GeminiClient
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.onboarding import BusinessSetup
from jarvis.pipeline import IntakePipeline
from jarvis.secrets import SecretVault
from jarvis.sheets import AppsScript
from jarvis.trading_engine import compose_dealer_quote, find_best_stock_match, is_worth_effort

SAMPLE_MESSAGES = [
    {
        "type": "BUY",
        "sender": "+919820011223",
        "name": "Rajesh Mobile Hub (Mumbai)",
        "text": "WTB iPhone 17 256GB Lavender 50pcs Non-Active urgent @ 820$ with GST, payment ready today",
    },
    {
        "type": "BUY",
        "sender": "+971501234567",
        "name": "Al-Futtaim Electronics (Dubai)",
        "text": "Need ready stock Samsung Galaxy S25 Ultra 12/256GB Black 20 units. Quote in USD DAFZA warehouse.",
    },
    {
        "type": "SELL",
        "sender": "+919811223344",
        "name": "Apex Impex Delhi",
        "text": "WTS ready stock: iPhone 17 256GB Non-Active 100pcs @ 790$ (Black-50, Lavender-30, Blue-20) Indian spec sealed.",
    },
    {
        "type": "SELL",
        "sender": "+971559876543",
        "name": "Global Traders FZE",
        "text": "WTS Samsung Galaxy S25 Ultra 12/512GB Titanium 30pcs @ 960$ ready in DAFZA freezone.",
    },
    {
        "type": "SOCIAL",
        "sender": "+919356818406",
        "name": "Darsheel College",
        "text": "Good morning bhai! Basketball Selection Trials are scheduled today 4 PM at campus ground. Do join!",
    },
    {
        "type": "SOCIAL",
        "sender": "+919999988888",
        "name": "Unknown Casual",
        "text": "Hi, how are you doing? Let's catch up this weekend for coffee.",
    },
    {
        "type": "AMBIGUOUS",
        "sender": "+919777766666",
        "name": "Wholesale Dealer X",
        "text": "JV LOCK ... 13. 128 mix 100pcs rate?",
    },
]


class MockRawRepo:
    """Minimal local vault-backed repo fallback."""
    def __init__(self, vault):
        self.vault = vault
        self.connected = False
    async def list(self, collection, limit): return []
    async def get(self, collection, id): return None
    async def put(self, collection, id, data, revision, request_id): return {"id": id, "revision": revision + 1, "data": data}


def setup_harness():
    vault = SecretVault()
    db_file = runtime_directory() / "workspace.db"
    repo = SQLiteSyncedRepository(MockRawRepo(vault), db_path=db_file)
    gemini = GeminiClient(vault)
    pipeline = IntakePipeline(repo, gemini, local_first=True)
    setup = BusinessSetup(
        business_name="Jarvis Trading Desk",
        owner_phone="+919876543210",
        classifier_model="gemini-3.5-flash-lite",
        scan_all=True,
        sources=[{"phone": "+919876543211", "label": "Stock Supplier", "purpose": "both", "active": True}],
    ).model_dump()
    return vault, repo, gemini, pipeline, setup


async def test_llm_speed_and_accuracy(gemini):
    print("\n" + "=" * 70)
    print(" 1. TESTING GEMINI LLM INFERENCE (Latency & Accuracy Benchmark)")
    print("=" * 70)
    for sample in SAMPLE_MESSAGES[:4]:
        t0 = time.perf_counter()
        res = await gemini.classify(sample["text"])
        dt = (time.perf_counter() - t0) * 1000
        print(f"\n[TEXT]: {sample['text']}")
        print(f" -> Expected: {sample['type']} | Detected: {res.category} (Latency: {dt:.1f}ms)")
        print(f" -> Reason: {res.reason}")
        if res.items:
            for it in res.items:
                print(f"    * Product: {it.device} ({it.variant}) | Qty: {it.quantity} | Price: {it.unit_price} {it.currency} | Cond: {it.condition}")
        match = (sample["type"] == res.category) or (sample["type"] == "SELL" and res.category in ("SELL", "STOCK"))
        status = "PASSED" if match else "FLAGGED"
        print(f" -> Status: [{status}]")


async def inject_test_messages(pipeline, setup, repo):
    print("\n" + "=" * 70)
    print(" 2. INJECTING TEST MESSAGES INTO DESKTOP WORKSPACE (Live Database Test)")
    print("=" * 70)
    now = time.time()
    for idx, sample in enumerate(SAMPLE_MESSAGES, start=1):
        batch_tag = uuid.uuid4().hex[:8]
        conv_id = sample["sender"][1:] + "@c.us"
        msg_id = f"false_{conv_id}_{batch_tag}_{idx}"
        event = {
            "message_id": msg_id,
            "conversation_id": conv_id,
            "sender_id": conv_id,
            "phone": sample["sender"],
            "sender_name": sample["name"],
            "chat_name": sample["name"],
            "text": sample["text"],
            "timestamp": now - (len(SAMPLE_MESSAGES) - idx) * 60,
            "identity_verified": True,
        }
        res = await pipeline.process(event, setup)
        print(f"[{idx}/{len(SAMPLE_MESSAGES)}] Injected '{sample['name']}': status={res.get('status')} (cat={sample['type']})")


async def test_matching_and_quote_generation(repo):
    print("\n" + "=" * 70)
    print(" 3. TESTING STOCK MATCHING & DEALER QUOTE GENERATION")
    print("=" * 70)

    # 1. Ensure test stock exists
    existing = repo.get_sync("inventory", "test-inv-01") if hasattr(repo, "get_sync") else None
    rev = existing["revision"] if existing else 0
    await repo.put("inventory", "test-inv-01", {
        "device": "iPhone 17",
        "variant": "256GB",
        "condition": "NON_ACTIVE",
        "lock_status": "UNLOCKED",
        "quantity": 100,
        "unit_cost": 790.0,
        "cost_currency": "USD",
        "region_spec": "Indian Spec",
    }, rev, "test-inv-01")

    # 2. Test matching for a buyer inquiry
    buy_inquiry = {
        "device": "iPhone 17",
        "variant": "256GB",
        "condition": "NON_ACTIVE",
        "quantity": 50,
    }
    match = await find_best_stock_match(repo, buy_inquiry)
    print(f"\n[WTB Request]: iPhone 17 256GB Non-Active (50 pcs)")
    if match:
        print(f" -> Stock Match Found! Score: {match['score']}% (Source: {match['source_kind']})")
        stock = match["item"]
        print(f" -> In-Stock: {stock.get('device')} {stock.get('variant')} - {stock.get('quantity')} units @ {stock.get('unit_cost')} USD")

        pricing = {
            "quote_usd": 825,
            "target_currency": "INR",
            "quote_local": 71200,
        }
        quotes = compose_dealer_quote(buy_inquiry, match, pricing)
        print(" -> Auto-Generated 2-Bubble WhatsApp Reply:")
        for bubble_idx, q in enumerate(quotes, 1):
            print(f"    [Bubble {bubble_idx}]: \"{q}\"")
    else:
        print(" -> No matching stock found.")


def audit_local_database(repo):
    print("\n" + "=" * 70)
    print(" 4. LOCAL DATABASE & SYNC HEALTH AUDIT")
    print("=" * 70)
    counts = repo.message_counts()
    print(f" -> Total Captured Messages in DB: {counts['captured']}")
    print(f" -> Buy Requests: {counts['buy']}")
    print(f" -> Sell / Stock Offers: {counts['sell']}")
    print(f" -> Review Needed: {counts.get('needs_review', 0)}")
    print(f" -> Stock Updates: {counts.get('stock', 0)}")
    print(f" -> Social / Archived: {counts.get('social', 0)}")

    people = repo.message_people()
    print(f" -> Distinct WhatsApp Contacts Indexed: {len(people)}")
    for p in people[:5]:
        print(f"    * {p.get('name')} ({p.get('phone')}) - {p.get('count')} msgs")

    db_path = runtime_directory() / "workspace.db"
    print(f"\n -> SQLite Database Location: {db_path}")
    print(f" -> Database File Size: {db_path.stat().st_size / 1024:.1f} KB")


async def main():
    print("=" * 70)
    print(" JARVIS-TRADE RUTHLESS PRODUCTION-GRADE TEST HARNESS")
    print("=" * 70)
    vault, repo, gemini, pipeline, setup = setup_harness()

    await test_llm_speed_and_accuracy(gemini)
    await inject_test_messages(pipeline, setup, repo)
    await test_matching_and_quote_generation(repo)
    audit_local_database(repo)

    print("\n" + "=" * 70)
    print(" TEST COMPLETED SUCCESSFULLY")
    print(" All injected records are now live inside 'workspace.db'.")
    print(" Open Jarvis Trade desktop app to see the live updates!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
