// Isolated content script: local activity only. No page commands or secrets.
const host=document.createElement('div');host.id='jarvis-workspace-indicator';
Object.assign(host.style,{position:'fixed',bottom:'14px',left:'14px',zIndex:'2147483646'});
const root=host.attachShadow({mode:'closed'});
root.innerHTML=`<style>:host{all:initial}*{box-sizing:border-box}section{width:304px;background:#fff;color:#263149;font:12px/1.6 system-ui;border:1px solid #dfe3ee;border-radius:12px;box-shadow:0 8px 35px #1c2a4533;overflow:hidden}header{display:flex;align-items:center;gap:10px;background:#273149;color:white;padding:12px 14px}header b{font-size:13px}header small{display:block;color:#b6c2d9;font-size:9px}button{margin-left:auto;background:transparent;border:0;color:#b6c2d9;font-size:18px;cursor:pointer}main{padding:16px}p{margin:0 0 15px;overflow-wrap:anywhere}ol{list-style:none;padding:0;margin:0;display:flex;gap:6px}li{background:#f1f2f8;border-radius:6px;padding:9px 7px;flex:1;font-size:10px;color:#8b95a9}li.active{background:#ece8fb;color:#6b55b2}footer{border-top:1px solid #edf0f5;padding-top:12px;margin-top:13px;color:#8c96aa;font-size:9px}.compact main{display:none}.compact{width:200px}img{width:26px;height:26px}</style><section><header><img alt="Jarvis"><div><b>Jarvis workspace</b><small>WhatsApp activity</small></div><button title="Collapse activity panel">−</button></header><main><p id="detail">Waiting for your desktop app.</p><ol><li id="capture">Scan<br>WhatsApp</li><li id="storage">Save<br>Locally</li><li id="queue">Review<br>Trade inbox</li></ol><footer id="coverage">Start / Stop from the desktop app. Incoming messages and direct replies.</footer></main></section>`;
root.querySelector('img').src=chrome.runtime.getURL('icons/jarvis-48.png');
root.querySelector('button').onclick=()=>{const compact=root.querySelector('section').classList.toggle('compact');root.querySelector('button').textContent=compact?'+':'−';};
document.documentElement.append(host);
function paint(a){if(a?.agentName)root.querySelector('header b').textContent=`${a.agentName} workspace`;root.querySelector('#detail').textContent=a.detail;for(const id of ['capture','storage','queue'])root.querySelector('#'+id).classList.remove('active');const stage=a.phase==='scanning'?'capture':a.phase==='saving'?'storage':a.phase==='waiting'?'queue':null;if(stage)root.querySelector('#'+stage).classList.add('active');}
function coverage(s){if(s?.agentName)root.querySelector('header b').textContent=`${s.agentName} workspace`;if(s.coverage)root.querySelector('#coverage').textContent=`${s.coverage.scanned} chat scanned of ${s.coverage.discovered} rendered. Coverage is partial. Last scan ${s.lastScan?new Date(s.lastScan).toLocaleTimeString():'pending'}.`;}
const listeners = new AbortController();
let wakeTimer;
let retryTimer;
function stop() {
  listeners.abort();
  clearTimeout(wakeTimer);
  clearInterval(retryTimer);
  host.remove();
}
async function useExtension(operation) {
  if (listeners.signal.aborted) return;
  try {
    if (!chrome.runtime?.id) {
      stop();
      return;
    }
    await operation();
  } catch (error) {
    if (!chrome.runtime?.id || /extension context invalidated/i.test(String(error))) stop();
  }
}
const triggerWake = () => useExtension(() => chrome.runtime.sendMessage({operation:'wake'}));
const loadCoverage = () => useExtension(async () => {
  const state = await chrome.storage.local.get(['activity','coverage','lastScan','agentName']);
  if (state.activity) paint({...state.activity,agentName:state.agentName});
  coverage(state);
});
chrome.runtime.onMessage.addListener((message,sender) => {
  if (sender.id === chrome.runtime.id && message.operation === 'jarvis_activity') paint(message);
});
chrome.storage.onChanged.addListener((changes,area) => {
  if (area === 'local' && ['activity','coverage','lastScan','agentName'].some(key => key in changes)) void loadCoverage();
});
function scheduleWake(delay) {
  clearTimeout(wakeTimer);
  wakeTimer = setTimeout(triggerWake, delay);
}
const options = {signal:listeners.signal};
let nextMessageWake = 0;
document.addEventListener('jarvis-whatsapp-message', () => {
  if (Date.now() < nextMessageWake) return;
  nextMessageWake = Date.now() + 750;
  scheduleWake(100);
}, options);
window.addEventListener('blur',() => scheduleWake(600),options);
window.addEventListener('focus',() => scheduleWake(250),options);
document.addEventListener('visibilitychange',() => {
  if (document.visibilityState === 'visible') scheduleWake(300);
},options);
retryTimer = setInterval(() => {
  if (!chrome.runtime?.id) {
    stop();
    return;
  }
  const detail = root.querySelector('#detail')?.textContent || '';
  if (/connect|disconnect|retry|paused|timed out/i.test(detail)) void triggerWake();
},15000);
void triggerWake();
void loadCoverage();
