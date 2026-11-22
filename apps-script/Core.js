/* Pure transaction engine. Shared by Apps Script and Node failure-injection tests. */
const Collections = ["configuration","sources","contacts","messages","offers","requests","inventory","tasks","quotes","actions","fx","news","audit"];

function recover(store) {
  for (const op of store.prepared()) {
    for (const change of op.changes) {
      const current=store.get(change.collection,change.id);
      if (current && current.revision===change.record.revision && JSON.stringify(current)===JSON.stringify(change.record)) continue;
      if ((current?.revision || 0)!==change.expected_revision) throw new Error("RECOVERY_CONFLICT");
      store.set(change.collection,change.id,change.record);
    }
    store.commit(op.id);
  }
}

function execute(store, request, payloadHash, now) {
  recover(store);
  if (request.action==="health") return {schema_version:1,storage:"google_sheets",collections:Collections};
  const payload=request.payload || {};
  if (request.action==="get" || request.action==="list") {
    if (!Collections.includes(payload.collection)) throw new Error("UNKNOWN_COLLECTION");
    let reconcileError = null;
    if (payload.collection==="inventory") {
      try { reconcileHumanInventory_(store); } catch (err) {
        reconcileError = String(err && err.message ? err.message : err);
      }
    }
    if (request.action==="get") return {record:store.get(payload.collection,payload.id)};
    const limit=Math.max(1,Math.min(Number(payload.limit)||100,500));
    const offset=Math.max(0,Number(payload.offset)||0);
    const all=store.list(payload.collection).sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
    const slice=all.slice(offset,offset+limit);
    const result={
      records: slice,
      total: all.length,
      has_more: (offset + slice.length) < all.length,
      next_offset: offset + slice.length
    };
    if (reconcileError) result.reconciliation_error = reconcileError;
    return result;
  }
  if (request.action!=="mutate") throw new Error("UNKNOWN_ACTION");
  const previous=store.operation(request.request_id);
  if (previous) {
    if (previous.hash!==payloadHash) throw new Error("IDEMPOTENCY_CONFLICT");
    return {records:previous.changes.map(c=>c.record)};
  }
  if (!Array.isArray(payload.changes) || !payload.changes.length || payload.changes.length>20) throw new Error("INVALID_BATCH");
  const seen=new Set();
  const changes=payload.changes.map(c=>{
    if (!Collections.includes(c.collection) || !/^[a-zA-Z0-9_:+.\-]{1,150}$/.test(c.id) || !Number.isInteger(c.expected_revision) || c.expected_revision<0 || !c.data || typeof c.data!=="object" || Array.isArray(c.data)) throw new Error("INVALID_CHANGE");
    const key=c.collection+":"+c.id;
    if (seen.has(key)) throw new Error("DUPLICATE_BATCH_KEY"); seen.add(key);
    const current=store.get(c.collection,c.id);
    if ((current?.revision || 0)!==c.expected_revision) throw new Error("REVISION_CONFLICT");
    if (JSON.stringify(c.data).length>16000) throw new Error("RECORD_TOO_LARGE");
    return {collection:c.collection,id:c.id,expected_revision:c.expected_revision,record:{id:c.id,revision:c.expected_revision+1,updated_at:now,data:c.data}};
  });
  if(JSON.stringify(changes).length>40000)throw new Error("BATCH_TOO_LARGE");
  // Write-ahead intent is durable before projections. Every request recovers under a script lock.
  store.prepare({id:request.request_id,hash:payloadHash,changes,status:"PREPARED"});
  recover(store);
  if (typeof store.compactOperations === "function") {
    store.compactOperations(200);
  }
  return {records:changes.map(c=>c.record)};
}

function reconcileHumanInventory_(store) {
  const sheet = typeof store.book?.getSheetByName === "function" ? store.book.getSheetByName("Live_Inventory") : (store.sheets && store.sheets["Live_Inventory"]);
  if (!sheet) return;
  const values = typeof sheet.getValues === "function" ? sheet.getValues() : (sheet.values || []);
  const rows = values.slice(1);
  const nowIso = new Date().toISOString();
  for (const r of rows) {
    const id = String(r[0] || "").trim();
    const device = String(r[1] || "").trim();
    if (!id && !device) continue;
    const variant = String(r[2] || "").trim();
    const condition = String(r[3] || "NON_ACTIVE").trim().toUpperCase();
    const lock_status = String(r[4] || "UNLOCKED").trim().toUpperCase();
    const region_spec = String(r[5] || "GLOBAL").trim().toUpperCase();
    const rawColors = String(r[6] || "").trim();
    const qty = Number(r[7]);
    const cost = Number(r[8]);
    const currency = String(r[9] || "INR").trim().toUpperCase();

    let color_breakdown = {};
    if (rawColors) {
      rawColors.split(",").forEach(part => {
        const [k, v] = part.split(":").map(s => s.trim());
        if (k && !isNaN(Number(v))) color_breakdown[k] = Number(v);
      });
    }

    const rowId = id || ("inv-manual-" + Math.abs(device.split("").reduce((a, b) => ((a << 5) - a) + b.charCodeAt(0), 0)).toString(16));
    const existing = store.get("inventory", rowId);
    if (!existing) {
      const record = {
        id: rowId,
        revision: 1,
        updated_at: nowIso,
        data: {
          device,
          variant,
          condition: condition || "NON_ACTIVE",
          lock_status: lock_status || "UNLOCKED",
          region_spec: region_spec || "GLOBAL",
          quantity: isNaN(qty) ? 0 : qty,
          unit_cost: isNaN(cost) ? null : cost,
          currency: currency || "INR",
          color_breakdown: Object.keys(color_breakdown).length ? color_breakdown : undefined,
          source: "manual_sheet_entry",
        },
      };
      store.set("inventory", rowId, record);
    } else {
      const d = existing.data || {};
      const changed = (!isNaN(qty) && qty !== d.quantity) ||
                      (!isNaN(cost) && cost !== d.unit_cost) ||
                      (currency && currency !== d.currency) ||
                      (condition && condition !== d.condition) ||
                      (lock_status && lock_status !== d.lock_status) ||
                      (device && device !== (d.device || d.model)) ||
                      (variant && variant !== d.variant);
      if (changed) {
        existing.revision += 1;
        existing.updated_at = nowIso;
        existing.data = {
          ...d,
          device: device || d.device || d.model,
          variant: variant || d.variant,
          condition: condition || d.condition,
          lock_status: lock_status || d.lock_status,
          region_spec: region_spec || d.region_spec,
          quantity: isNaN(qty) ? d.quantity : qty,
          unit_cost: isNaN(cost) ? d.unit_cost : cost,
          currency: currency || d.currency,
          color_breakdown: Object.keys(color_breakdown).length ? color_breakdown : d.color_breakdown,
        };
        store.set("inventory", rowId, existing);
      }
    }
  }
}

const LEDGER_SPECS_ = {
  inventory: {
    tab: "Live_Inventory",
    headers: ["ID", "Brand/Device", "Variant", "Condition", "Lock", "Region", "Colors", "Qty", "Cost", "Currency", "Updated"],
    row: (r) => {
      const d = r.data || {};
      const colors = d.color_breakdown ? Object.entries(d.color_breakdown).map(([k, v]) => `${k}:${v}`).join(", ") : "";
      return [r.id, d.device || d.model || "", d.variant || "", d.condition || "NON_ACTIVE", d.lock_status || "UNLOCKED", d.region_spec || "GLOBAL", colors, d.quantity ?? 0, d.unit_cost ?? "", d.currency || "INR", r.updated_at];
    },
  },
  requests: {
    tab: "Buy_Requests_WTB",
    headers: ["ID", "Buyer Phone", "Device / Part", "Variant / Specs", "Qty", "Target Price", "Currency", "Status", "Raw Message", "Updated"],
    rows: (r) => {
      const d = r.data || {};
      const items = (Array.isArray(d.items) && d.items.length) ? d.items : [{}];
      return items.map((item, idx) => {
        const rowId = r.id + (items.length > 1 ? "#" + (idx + 1) : "");
        const dev = item.part_number || item.device || item.model || d.device || "";
        const spec = item.specs || item.variant || item.packaging || d.variant || "";
        const qty = item.quantity ?? d.quantity ?? "";
        const price = item.unit_price ?? item.target_price ?? "";
        const curr = item.currency || d.reply_currency || d.currency || "USD";
        return [rowId, d.phone || "", dev, spec, qty, price, curr, d.state || "OPEN", (d.raw_text || "").slice(0, 240), r.updated_at];
      });
    },
  },
  offers: {
    tab: "Supplier_Offers_WTS",
    headers: ["ID", "Supplier Phone", "Device / Part", "Variant / Specs", "Qty", "Ask Price", "Currency", "Status", "Raw Message", "Updated"],
    rows: (r) => {
      const d = r.data || {};
      const items = (Array.isArray(d.items) && d.items.length) ? d.items : [{}];
      return items.map((item, idx) => {
        const rowId = r.id + (items.length > 1 ? "#" + (idx + 1) : "");
        const dev = item.part_number || item.device || item.model || d.device || "";
        const spec = item.specs || item.variant || item.packaging || d.variant || "";
        const qty = item.quantity ?? d.quantity ?? "";
        const price = item.unit_price ?? item.unit_cost ?? "";
        const curr = item.currency || d.currency || "INR";
        return [rowId, d.phone || "", dev, spec, qty, price, curr, d.state || "OPEN", (d.raw_text || "").slice(0, 240), r.updated_at];
      });
    },
  },
};

if (typeof module!=="undefined") module.exports={Collections,recover,execute,reconcileHumanInventory_,LEDGER_SPECS_};
