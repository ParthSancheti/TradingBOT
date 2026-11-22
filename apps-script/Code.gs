/* Deploy as an Apps Script web app; all calls require an HMAC, no public data GET. */
function canonical_(value) {
  if (Array.isArray(value)) return "["+value.map(canonical_).join(",")+"]";
  if (value!==null && typeof value==="object") return "{"+Object.keys(value).sort().map(k=>JSON.stringify(k)+":"+canonical_(value[k])).join(",")+"}";
  return JSON.stringify(value);
}
function hex_(bytes) { return bytes.map(x=>(x&255).toString(16).padStart(2,"0")).join(""); }
function sign_(secret,value) { return hex_(Utilities.computeHmacSha256Signature(canonical_(value),secret,Utilities.Charset.UTF_8)); }
function equal_(a,b) { if(typeof a!=="string" || typeof b!=="string" || a.length!==b.length) return false; let x=0; for(let i=0;i<a.length;i++) x|=a.charCodeAt(i)^b.charCodeAt(i); return x===0; }
function json_(value) { return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON); }
function signed_(secret,value) { const body=Utilities.base64Encode(JSON.stringify(value),Utilities.Charset.UTF_8);return json_({body,signature:hex_(Utilities.computeHmacSha256Signature(body,secret,Utilities.Charset.UTF_8))}); }
function doGet() { return json_({ok:false,code:"SIGNED_POST_REQUIRED"}); }

function doPost(e) {
  let request,secret,lock,verified=false;
  try {
    if(!e || !e.postData || e.postData.contents.length>64000) throw new Error("INVALID_REQUEST");
    const envelope=JSON.parse(e.postData.contents);
    const properties=PropertiesService.getScriptProperties();
    secret=properties.getProperty("JARVIS_SIGNING_SECRET");
    if(!secret || secret.length<32) throw new Error("NOT_CONFIGURED");
    if(typeof envelope.body!=="string" || !equal_(envelope.signature,hex_(Utilities.computeHmacSha256Signature(envelope.body,secret,Utilities.Charset.UTF_8)))) throw new Error("NOT_AUTHORIZED");
    request=JSON.parse(Utilities.newBlob(Utilities.base64Decode(envelope.body)).getDataAsString("UTF-8"));
    if(request.version!==1 || !/^[a-zA-Z0-9_-]{16,100}$/.test(request.request_id) || !/^[a-zA-Z0-9_-]{16,100}$/.test(request.nonce) || !Number.isInteger(request.timestamp) || Math.abs(Date.now()/1000-request.timestamp)>300) throw new Error("NOT_AUTHORIZED");
    verified=true;
    lock=LockService.getScriptLock();
    if(!lock.tryLock(10000)) throw new Error("BUSY_RETRY");
    const book=SpreadsheetApp.openById(properties.getProperty("JARVIS_SPREADSHEET_ID"));
    const store=new GoogleStore_(book);
    const payloadHash=hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,canonical_({action:request.action,payload:request.payload}),Utilities.Charset.UTF_8));
    const result=execute(store,request,payloadHash,new Date().toISOString());
    if(request.action==="health")result.workbook_url=book.getUrl();
    if(request.action==="mutate"&&Array.isArray(request.payload?.changes)){
      const touched=request.payload.changes.map(c=>c.collection);
      const humanCols=touched.filter(c=>c in LEDGER_SPECS_);
      if(humanCols.length>0){
        try{syncHumanLedgers_(store,humanCols,true);}catch(_e){}
      }
    }
    const response={ok:true,request_id:request.request_id,nonce:request.nonce,result};
    return signed_(secret,response);
  } catch(error) {
    const safe=["UNKNOWN_COLLECTION","UNKNOWN_ACTION","IDEMPOTENCY_CONFLICT","INVALID_BATCH","INVALID_CHANGE","DUPLICATE_BATCH_KEY","REVISION_CONFLICT","RECORD_TOO_LARGE","RECOVERY_CONFLICT","BUSY_RETRY","BATCH_TOO_LARGE"];
    const response={ok:false,request_id:request?.request_id || "",nonce:request?.nonce || "",code:safe.includes(error.message)?error.message:"NOT_AUTHORIZED"};
    // Never sign an unverified request or return stack traces, workbook content or secrets.
    return verified?signed_(secret,response):json_({ok:false,code:"NOT_AUTHORIZED"});
  } finally { if(lock && lock.hasLock()) lock.releaseLock(); }
}

class GoogleStore_ {
  constructor(book) { this.book=book; this.cache={}; }
  sheet(name) {
    let sheet=this.book.getSheetByName(name);
    if(!sheet) {
      sheet=this.book.insertSheet(name);
      const headers=name==="_operations"?["id","hash","status","changes_json"]:["id","revision","updated_at","data_json"];
      sheet.getRange(1,1,1,headers.length).setValues([headers]).setFontWeight("bold").setBackground("#192f4b").setFontColor("#ffffff");
      sheet.setFrozenRows(1); sheet.setColumnWidth(1,260); sheet.setColumnWidth(headers.length,650);
      sheet.getRange(1,1,sheet.getMaxRows(),headers.length).setNumberFormat("@");
    }
    return sheet;
  }
  rows(name) {
    if(!this.cache[name]) { const s=this.sheet(name); this.cache[name]=s.getLastRow()>1?s.getRange(2,1,s.getLastRow()-1,4).getValues():[]; }
    return this.cache[name];
  }
  parse(row) { return {id:String(row[0]),revision:Number(row[1]),updated_at:String(row[2]),data:JSON.parse(row[3])}; }
  get(name,id) { const row=this.rows(name).find(r=>r[0]===id); return row?this.parse(row):null; }
  list(name) { return this.rows(name).filter(r=>r[0]).map(r=>this.parse(r)); }
  set(name,id,record) {
    const rows=this.rows(name); const index=rows.findIndex(r=>r[0]===id);
    const row=[id,record.revision,record.updated_at,JSON.stringify(record.data)];
    this.sheet(name).getRange(index<0?rows.length+2:index+2,1,1,4).setValues([row]);
    if(index<0) rows.push(row); else rows[index]=row;
    SpreadsheetApp.flush();
  }
  operation(id) { const row=this.rows("_operations").find(r=>r[0]===id); return row?{id:row[0],hash:row[1],status:row[2],changes:JSON.parse(row[3])}:null; }
  prepared() { return this.rows("_operations").filter(r=>r[2]==="PREPARED").map(r=>({id:r[0],hash:r[1],status:r[2],changes:JSON.parse(r[3])})); }
  prepare(op) { const rows=this.rows("_operations"); const row=[op.id,op.hash,"PREPARED",JSON.stringify(op.changes)]; this.sheet("_operations").getRange(rows.length+2,1,1,4).setValues([row]); rows.push(row); SpreadsheetApp.flush(); }
  commit(id) {
    const rows=this.rows("_operations"); const index=rows.findIndex(r=>r[0]===id);
    if(index<0) throw new Error("RECOVERY_CONFLICT");
    this.sheet("_operations").getRange(index+2,3).setValue("COMMITTED");
    rows[index][2]="COMMITTED";
    if(rows.length>350) {
      this.compactOperations(200);
    } else {
      SpreadsheetApp.flush();
    }
  }
  compactOperations(maxRows = 200) {
    const rows = this.rows("_operations");
    if (rows.length <= maxRows) return;
    const prepared = rows.filter(r => r[2] === "PREPARED");
    const committed = rows.filter(r => r[2] === "COMMITTED");
    if (committed.length <= maxRows) return;
    const retained = [...prepared, ...committed.slice(committed.length - maxRows)];
    this.cache["_operations"] = retained;
    const s = this.sheet("_operations");
    if (s.getLastRow() > 1) {
      s.getRange(2, 1, s.getLastRow() - 1, 4).clearContent();
    }
    if (retained.length) {
      s.getRange(2, 1, retained.length, 4).setValues(retained);
    }
    SpreadsheetApp.flush();
  }
}

function reconcileHumanInventory_(store) {
  const sheet = store.book.getSheetByName("Live_Inventory");
  if (!sheet || sheet.getLastRow() <= 1) return;
  const values = sheet.getRange(2, 1, sheet.getLastRow() - 1, 11).getValues();
  const nowIso = new Date().toISOString();
  for (const r of values) {
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

    const rowSig = `${device}::${variant}::${condition}::${lock_status}::${region_spec}`;
    const rowId = id || ("inv-manual-" + Math.abs(rowSig.split("").reduce((a, b) => ((a << 5) - a) + b.charCodeAt(0), 0)).toString(16));
    const existing = store.get("inventory", rowId);
    if (!existing) {
      // New row added by staff in Google Sheets
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
      // Existing row edited by staff
      const d = existing.data || {};
      const changed = (!isNaN(qty) && qty !== d.quantity) ||
                      (!isNaN(cost) && cost !== d.unit_cost) ||
                      (currency && currency !== d.currency) ||
                      (condition && condition !== d.condition) ||
                      (lock_status && lock_status !== d.lock_status) ||
                      (region_spec && region_spec !== d.region_spec) ||
                      (device && device !== (d.device || d.model)) ||
                      (variant && variant !== d.variant) ||
                      (rawColors !== (d._raw_colors || ""));
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
          _raw_colors: rawColors,
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
  tasks: {
    tab: "Trade_Alerts",
    headers: ["ID", "Category", "Title", "Summary", "State", "Updated"],
    filter: (r) => r.data && r.data.kind === "notification",
    row: (r) => {
      const d = r.data || {};
      return [r.id, d.category || "", d.title || "", d.body || "", d.state || "NEW", r.updated_at];
    },
  },
  fx: {
    tab: "Market_Watch",
    headers: ["ID", "Pair/Type", "Rate/Predicted", "Source", "Updated"],
    row: (r) => {
      const d = r.data || {};
      return [r.id, d.pair || d.model || "FX", d.rate ?? d.predicted_rate ?? "", d.source || d.basis || "", r.updated_at];
    },
  },
};

function syncHumanLedgers_(store, collections, skipReconcile = false) {
  const unique = [...new Set(collections || Object.keys(LEDGER_SPECS_))];
  if (unique.includes("inventory") && !skipReconcile) {
    try { reconcileHumanInventory_(store); } catch (_e) {
      console.warn("reconcileHumanInventory_ failed: " + (_e && _e.message ? _e.message : _e));
    }
  }
  for (const col of unique) {
    const spec = LEDGER_SPECS_[col];
    if (!spec) continue;
    let sheet = store.book.getSheetByName(spec.tab);
    if (!sheet) {
      sheet = store.book.insertSheet(spec.tab);
      sheet.getRange(1, 1, 1, spec.headers.length).setValues([spec.headers]).setFontWeight("bold").setBackground("#0f2942").setFontColor("#ffffff");
      sheet.setFrozenRows(1);
    }
    const items = store.list(col).filter((r) => !spec.filter || spec.filter(r)).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 300);
    const maxCols = spec.headers.length;
    let projectedRows = [];
    for (const record of items) {
      if (typeof spec.rows === "function") {
        projectedRows.push(...spec.rows(record));
      } else if (typeof spec.row === "function") {
        projectedRows.push(spec.row(record));
      }
    }
    projectedRows = projectedRows.slice(0, 500);

    if (sheet.getLastRow() > 1) {
      sheet.getRange(2, 1, sheet.getLastRow() - 1, maxCols).clearContent();
    }
    if (projectedRows.length) {
      sheet.getRange(2, 1, projectedRows.length, maxCols).setValues(projectedRows.map(row => row.map(value =>
        typeof value === "string" && value.startsWith("=") ? "'" + value : value
      )));
    }
  }
  SpreadsheetApp.flush();
}

function setupJarvisWorkbook() {
  // Run once in your own Apps Script editor after setting the two Script Properties.
  const p=PropertiesService.getScriptProperties();
  if((p.getProperty("JARVIS_SIGNING_SECRET") || "").length<32) throw new Error("Set a random signing secret in Script Properties first");
  let id=p.getProperty("JARVIS_SPREADSHEET_ID");
  if(!id) { const book=SpreadsheetApp.create("Jarvis-Trade business database"); id=book.getId(); p.setProperty("JARVIS_SPREADSHEET_ID",id); }
  const store=new GoogleStore_(SpreadsheetApp.openById(id));
  Collections.forEach(c=>store.sheet(c)); store.sheet("_operations");
  syncHumanLedgers_(store, Object.keys(LEDGER_SPECS_));
  return "https://docs.google.com/spreadsheets/d/"+id+"/edit";
}

const Collections = ["configuration","sources","contacts","messages","offers","requests","inventory","tasks","quotes","actions","fx","news","audit"];

