// End-to-end: real extension worker (background.js + scanner.mjs) -> real
// native host process -> real desktop LocalBridge/Runner/pipeline/SQLite.
// Only WhatsApp itself is simulated (a WPP store object with chats/messages).
// Usage: node scripts/e2e/run_e2e.mjs   (needs python with backend deps)
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import {spawn} from "node:child_process";
import {webcrypto} from "node:crypto";
import {fileURLToPath, pathToFileURL} from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");
const ext = path.join(root, "extension");
const python = process.env.PYTHON || (process.platform === "win32" ? path.join(root, ".venv", "Scripts", "python.exe") : "python3");
const runtime = fs.mkdtempSync(path.join(os.tmpdir(), "jarvis-e2e-"));
const env = {...process.env, LOCALAPPDATA: runtime, HOME: runtime};
const EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop";
const results = [];
const check = (name, ok, detail = "") => { results.push({name, ok, detail}); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- 1. real desktop side -------------------------------------------------
const backend = spawn(python, [path.join(here, "backend_server.py")], {env, cwd: here, stdio: ["pipe", "pipe", "inherit"]});
let backendOut = "";
backend.stdout.on("data", d => { backendOut += d; });
for (let i = 0; i < 100 && !backendOut.includes("READY"); i++) await sleep(100);
if (!backendOut.includes("READY")) { console.error("backend did not start"); process.exit(1); }

// ---- 2. simulated WhatsApp Web page (WPP store) ---------------------------
const now = Math.floor(Date.now() / 1000);
const SUPPLIER = "971500000002@c.us";
const BUYER = "447700900003@c.us";
const ARCHIVED = "14155550104@c.us";
const OWNER_SELF = "919800000001@c.us";
const msg = (id, body, t, extra = {}) => ({id: {id, _serialized: `false_x_${id}`}, type: "chat", body, t, ...extra});
const chats = new Map([
  [SUPPLIER, {id: SUPPLIER, t: now - 50, unreadCount: 1, formattedTitle: "Dubai supplier", messages: [
    msg("s1", "Ready stock iPhone 16 Pro 256GB 40 pcs USD 905", now - 50)]}],
  [BUYER, {id: BUYER, t: now - 40, unreadCount: 0, formattedTitle: "London buyer", messages: [
    msg("b1", "WTB Samsung S25 Ultra 512GB 20 pcs", now - 40)]}],
  [OWNER_SELF, {id: OWNER_SELF, t: now - 30, unreadCount: 0, formattedTitle: "You", isMe: true, messages: [
    {id: {id: "c1", fromMe: true, _serialized: `true_${OWNER_SELF}_c1`}, type: "chat", body: "@Jarvis how many S25 do we have?", t: now - 30}]}],
]);
const archivedChat = {id: ARCHIVED, t: now - 5, unreadCount: 3, formattedTitle: "Archived dealer", messages: []};
let newMessageHandler = null;
const chatModel = c => ({...c, msgs: {getModelsArray: () => c.messages.slice(-1)}});
globalThis.window = {
  WPP: {
    isReady: true,
    on: (name, fn) => { if (name === "chat.new_message") newMessageHandler = fn; },
    conn: {isAuthenticated: () => true, getMyUserWid: () => ({_serialized: OWNER_SELF, user: "919800000001"})},
    chat: {
      list: async () => [...chats.values()].map(chatModel),
      get: async id => (id === ARCHIVED ? chatModel(archivedChat) : chats.has(id) ? chatModel(chats.get(id)) : null),
      getMessages: async id => (id === ARCHIVED ? archivedChat : chats.get(id))?.messages || [],
    },
  },
};
globalThis.document = {dispatchEvent: () => {}, querySelector: () => null, getElementById: () => ({})};

// ---- 3. real extension worker with a real native port ---------------------
function frame(obj) { const b = Buffer.from(JSON.stringify(obj)); const h = Buffer.alloc(4); h.writeUInt32LE(b.length); return Buffer.concat([h, b]); }
function connectNative() {
  const host = spawn(python, [path.join(here, "host_wrapper.py"), `chrome-extension://${EXTENSION_ID}/`], {env, cwd: here, stdio: ["pipe", "pipe", "inherit"]});
  const msgL = [], discL = [];
  let buf = Buffer.alloc(0);
  host.stdout.on("data", d => {
    buf = Buffer.concat([buf, d]);
    while (buf.length >= 4 && buf.length >= 4 + buf.readUInt32LE(0)) {
      const n = buf.readUInt32LE(0); const body = JSON.parse(buf.subarray(4, 4 + n)); buf = buf.subarray(4 + n);
      msgL.forEach(f => f(body));
    }
  });
  host.on("exit", () => discL.forEach(f => f()));
  return {onMessage: {addListener: f => msgL.push(f)}, onDisconnect: {addListener: f => discL.push(f)},
    postMessage: m => host.stdin.write(frame(m)), disconnect: () => host.kill()};
}
const store = {}, session = {};
const chrome = {
  runtime: {connectNative, onStartup: {addListener() {}}, onInstalled: {addListener() {}}, onMessage: {addListener() {}},
    getURL: p => `chrome-extension://${EXTENSION_ID}/${p}`, id: EXTENSION_ID, lastError: null},
  storage: {
    local: {get: async k => typeof k === "string" ? {[k]: store[k]} : Object.fromEntries(k.map(x => [x, store[x]])), set: async d => Object.assign(store, d)},
    session: {get: async k => ({[k]: session[k]}), set: async d => Object.assign(session, d)},
  },
  tabs: {get: async () => ({id: 7, windowId: 1, active: false, status: "complete", url: "https://web.whatsapp.com/"}),
    query: async () => [{id: 7, windowId: 1, active: false, status: "complete", url: "https://web.whatsapp.com/"}],
    update: async () => {}, create: async () => ({id: 7}), sendMessage: async () => {}},
  windows: {get: async () => ({focused: false}), update: async () => {}},
  action: {setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {}},
  scripting: {executeScript: async args => {
    if (args.files) return [{result: undefined}];
    const r = await args.func(...(args.args || []));
    return [{documentId: "doc", result: r}];
  }},
  alarms: {onAlarm: {addListener() {}}, create: async () => {}},
};
const scanner = await import(pathToFileURL(path.join(ext, "scanner.mjs")).href);
const actions = await import(pathToFileURL(path.join(ext, "actions.mjs")).href);
const {validHello} = await import(pathToFileURL(path.join(ext, "boundary.mjs")).href);
const {selectWhatsAppTab} = await import(pathToFileURL(path.join(ext, "tab-control.mjs")).href);
const context = vm.createContext({chrome, TextEncoder, crypto: webcrypto, setTimeout, clearTimeout, console, window: globalThis.window,
  validHello, selectWhatsAppTab, ...scanner, ...actions});
vm.runInContext(fs.readFileSync(path.join(ext, "background.js"), "utf8").replace(/^import .*;$/gm, ""), context);
const scan = () => vm.runInContext("scan(true)", context);

// ---- 4. scenario -----------------------------------------------------------
await scan();
console.log("store after scan:", JSON.stringify({lastError: store.lastError, state: store.state, activity: store.activity}));
check("handshake + poll_config through real native host", store.lastError === null && store.state?.startsWith("Connected"), store.state || store.lastError);
check("WPP engine used (no DOM chat clicking)", store.engine === "wpp", `engine=${store.engine} reason=${store.wppReason}`);

// A new message arrives in an archived chat that is not in the chat list.
archivedChat.messages = [
  msg("a1", "WTS Pixel 9 Pro 128GB 15 pcs", now - 20),
  msg("a2", "Ready stock iPad Air 11 256GB 30 pcs", now - 10),
  msg("a3", "Price USD 540 each", now - 5),
];
newMessageHandler?.({id: {remote: ARCHIVED}});
await scan();
await scan(); // repeated scan must not duplicate anything
backend.stdin.write("dump\n");
for (let i = 0; i < 100 && !backendOut.includes("{\"messages\""); i++) await sleep(100);
const dump = JSON.parse(backendOut.slice(backendOut.indexOf("{\"messages\"")).split("\n")[0]);
const texts = dump.messages.map(m => m.text);
check("supplier stock message reached the app", texts.includes("Ready stock iPhone 16 Pro 256GB 40 pcs USD 905"));
check("buyer WTB message reached the app", texts.includes("WTB Samsung S25 Ultra 512GB 20 pcs"));
check("all 3 messages from archived chat (live event) reached the app",
  ["WTS Pixel 9 Pro 128GB 15 pcs", "Ready stock iPad Air 11 256GB 30 pcs", "Price USD 540 each"].every(t => texts.includes(t)));
check("no duplicates after repeated scans", new Set(texts).size === texts.length, `${texts.length} records`);
check("@Jarvis WhatsApp command forwarded to desktop once", JSON.stringify(dump.agent_commands) === JSON.stringify(["@Jarvis how many S25 do we have?"]), JSON.stringify(dump.agent_commands));
check("messages synced to the Sheet", dump.sheet_rows.length === dump.messages.length && dump.sheet_pending.pending === 0, `${dump.sheet_rows.length} rows, pending=${dump.sheet_pending.pending}`);
check("runner reports extension active", dump.runner.state === "ACTIVE_READ_ONLY", dump.runner.state);
console.log("\nCaptured records:");
for (const m of dump.messages) console.log(`  [${m.state}] ${m.chat || m.phone}: ${m.text}`);
fs.writeFileSync(path.join(runtime, "e2e-result.json"), JSON.stringify({results, dump}, null, 2));
backend.kill();
vm.runInContext("port?.disconnect?.()", context);
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
