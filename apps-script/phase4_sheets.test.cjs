const test = require("node:test");
const assert = require("node:assert/strict");
const { Collections, recover, execute, reconcileHumanInventory_, LEDGER_SPECS_ } = require("./Core.js");

class MemoryStore {
  constructor() {
    this.records = {};
    this.ops = {};
    this.sheets = {
      Live_Inventory: {
        values: [
          ["ID", "Brand/Device", "Variant", "Condition", "Lock", "Region", "Colors", "Qty", "Cost", "Currency", "Updated"],
        ]
      }
    };
  }
  get(c, id) { return structuredClone(this.records[c + ":" + id] || null); }
  list(c) { return Object.entries(this.records).filter(([k]) => k.startsWith(c + ":")).map(([, v]) => structuredClone(v)); }
  set(c, id, r) { this.records[c + ":" + id] = structuredClone(r); }
  operation(id) { return this.ops[id] ? structuredClone(this.ops[id]) : null; }
  prepared() { return Object.values(this.ops).filter(x => x.status === "PREPARED").map(x => structuredClone(x)); }
  prepare(op) { this.ops[op.id] = structuredClone(op); }
  commit(id) { if (this.ops[id]) this.ops[id].status = "COMMITTED"; }
  compactOperations(maxRows = 200) {
    const committed = Object.entries(this.ops).filter(([, op]) => op.status === "COMMITTED");
    if (committed.length > maxRows) {
      const pruneCount = committed.length - maxRows;
      for (let i = 0; i < pruneCount; i++) {
        delete this.ops[committed[i][0]];
      }
    }
  }
}

test("reconcileHumanInventory_ detects edited quantity and cost in Live_Inventory", () => {
  const store = new MemoryStore();
  // Existing inventory record in the store
  store.set("inventory", "inv-1", {
    id: "inv-1",
    revision: 2,
    updated_at: "2026-11-19T00:00:00Z",
    data: {
      device: "iPhone 16 Pro Max",
      variant: "256GB",
      condition: "NON_ACTIVE",
      lock_status: "UNLOCKED",
      region_spec: "GLOBAL",
      quantity: 50,
      unit_cost: 1100,
      currency: "USD"
    }
  });

  // Human staff edits the quantity to 42 and cost to 1080 in Live_Inventory sheet
  store.sheets.Live_Inventory.values = [
    ["ID", "Brand/Device", "Variant", "Condition", "Lock", "Region", "Colors", "Qty", "Cost", "Currency", "Updated"],
    ["inv-1", "iPhone 16 Pro Max", "256GB", "NON_ACTIVE", "UNLOCKED", "GLOBAL", "Natural:42", 42, 1080, "USD", "2026-11-19T01:00:00Z"]
  ];

  reconcileHumanInventory_(store);

  const updated = store.get("inventory", "inv-1");
  assert.equal(updated.revision, 3, "Revision must bump when human edits stock");
  assert.equal(updated.data.quantity, 42, "Quantity must reflect human edit");
  assert.equal(updated.data.unit_cost, 1080, "Unit cost must reflect human edit");
  assert.equal(updated.data.color_breakdown.Natural, 42);
});

test("reconcileHumanInventory_ adds new manual rows added by staff in Google Sheets", () => {
  const store = new MemoryStore();
  // Human enters a new component line item directly in the sheet without ID
  store.sheets.Live_Inventory.values = [
    ["ID", "Brand/Device", "Variant", "Condition", "Lock", "Region", "Colors", "Qty", "Cost", "Currency", "Updated"],
    ["", "STM32F407VGT6", "LQFP-100", "NEW", "UNLOCKED", "GLOBAL", "", 500, 4.25, "USD", ""]
  ];

  reconcileHumanInventory_(store);

  const allInv = store.list("inventory");
  assert.equal(allInv.length, 1);
  const created = allInv[0];
  assert.match(created.id, /^inv-manual-/, "Auto-generates stable ID for manual row");
  assert.equal(created.revision, 1);
  assert.equal(created.data.device, "STM32F407VGT6");
  assert.equal(created.data.quantity, 500);
  assert.equal(created.data.unit_cost, 4.25);
  assert.equal(created.data.source, "manual_sheet_entry");
});

test("Multi-item WTB inquiry projects all BOM items into separate sheet rows", () => {
  const record = {
    id: "req-bom-101",
    revision: 1,
    updated_at: "2026-11-19T10:00:00Z",
    data: {
      phone: "+919876543210",
      state: "OPEN",
      raw_text: "WTB components: STM32 500pcs, ESP32 200pcs, LM2596 1000pcs",
      items: [
        { part_number: "STM32F407VGT6", specs: "LQFP-100", quantity: 500, target_price: 4.20, currency: "USD" },
        { part_number: "ESP32-WROOM-32D", specs: "16MB", quantity: 200, target_price: 2.10, currency: "USD" },
        { part_number: "LM2596S-5.0", specs: "TO-263-5", quantity: 1000, target_price: 0.65, currency: "USD" }
      ]
    }
  };

  const rows = LEDGER_SPECS_.requests.rows(record);
  assert.equal(rows.length, 3, "Must project all 3 BOM items");
  assert.equal(rows[0][0], "req-bom-101#1");
  assert.equal(rows[0][2], "STM32F407VGT6");
  assert.equal(rows[0][4], 500);
  assert.equal(rows[1][0], "req-bom-101#2");
  assert.equal(rows[1][2], "ESP32-WROOM-32D");
  assert.equal(rows[1][4], 200);
  assert.equal(rows[2][0], "req-bom-101#3");
  assert.equal(rows[2][2], "LM2596S-5.0");
  assert.equal(rows[2][4], 1000);
});

test("Multi-item WTS supplier offer projects all offered items into separate sheet rows", () => {
  const record = {
    id: "off-multi-202",
    revision: 1,
    updated_at: "2026-11-19T11:00:00Z",
    data: {
      phone: "+971501234567",
      state: "ACTIVE",
      raw_text: "WTS iPhone 17 256GB 50pcs $920, iPhone 17 Pro 256GB 30pcs $1120",
      items: [
        { device: "iPhone 17", variant: "256GB", quantity: 50, unit_price: 920, currency: "USD" },
        { device: "iPhone 17 Pro", variant: "256GB", quantity: 30, unit_price: 1120, currency: "USD" }
      ]
    }
  };

  const rows = LEDGER_SPECS_.offers.rows(record);
  assert.equal(rows.length, 2, "Must project both supplier offer items");
  assert.equal(rows[0][0], "off-multi-202#1");
  assert.equal(rows[0][2], "iPhone 17");
  assert.equal(rows[0][4], 50);
  assert.equal(rows[0][5], 920);
  assert.equal(rows[1][0], "off-multi-202#2");
  assert.equal(rows[1][2], "iPhone 17 Pro");
  assert.equal(rows[1][4], 30);
  assert.equal(rows[1][5], 1120);
});

test("compactOperations prunes older COMMITTED entries while preserving PREPARED entries", () => {
  const store = new MemoryStore();
  // Fill 250 committed operations
  for (let i = 1; i <= 250; i++) {
    store.ops["op-" + i] = { id: "op-" + i, status: "COMMITTED" };
  }
  // Add 1 PREPARED operation
  store.ops["op-uncommitted"] = { id: "op-uncommitted", status: "PREPARED" };

  store.compactOperations(200);

  const totalOps = Object.keys(store.ops).length;
  assert.equal(totalOps, 201, "Should keep 200 committed ops + 1 prepared op");
  assert.ok(store.ops["op-uncommitted"], "PREPARED op must NEVER be pruned");
  assert.ok(!store.ops["op-1"], "Oldest op-1 should be pruned");
  assert.ok(store.ops["op-250"], "Newest op-250 should be preserved");
});
