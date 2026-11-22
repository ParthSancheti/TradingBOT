import {chromium} from "/opt/npm-tools/node_modules/playwright/index.mjs";
const base = process.argv[2] || "http://127.0.0.1:8790";
const out = process.argv[3] || "/home/claude/shots";
const shim = `window.jarvis = Object.freeze({
  call: async (method, params = {}) => {
    const r = await fetch('/call', {method: 'POST', body: JSON.stringify({id: 'r' + Math.random().toString(36).slice(2, 10), method, params})});
    const j = await r.json();
    if (j.error) { const e = new Error(j.error.message); e.code = j.error.code; throw e; }
    return j.result;
  },
  onServiceState: cb => { setTimeout(() => cb({state: 'ready'}), 150); return () => {}; },
  onEvent: cb => { const t = setInterval(async () => { try { (await (await fetch('/events')).json()).forEach(cb); } catch {} }, 600); return () => clearInterval(t); },
});`;
const browser = await chromium.launch({executablePath: "/usr/bin/google-chrome", args: ["--no-sandbox"]});
const page = await browser.newPage({viewport: {width: 1440, height: 900}});
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("requestfailed", r => errors.push("REQ " + r.url() + " " + r.failure()?.errorText + " " + (r.postData() || "").slice(0, 60)));
await page.addInitScript(shim);
// Electron uses IPC, so the page CSP blocks fetch; allow same-origin fetch for the HTTP shim only.
await page.route("**/index.html", async route => {
  const res = await route.fetch();
  const body = (await res.text()).replace(/connect-src [^;"]*/, "connect-src 'self'");
  await route.fulfill({response: res, body});
});
await page.goto(base + "/index.html");
await page.waitForTimeout(6000);
for (const v of ["desk", "activity", "stock", "connections", "assistant"]) {
  await page.evaluate(view => document.querySelector(`[data-view="${view}"]`)?.click(), v);
  await page.waitForTimeout(1800);
  await page.screenshot({path: `${out}/app-${v}.png`});
  console.log("shot", v);
}
console.log("page errors:", errors.length ? errors.slice(0, 5) : "none");
await browser.close();
