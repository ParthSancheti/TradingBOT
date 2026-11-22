for(const [id,operation] of [["check","connection_proof"],["sync","sync"],["probe","probe_dom"]]){
  const el=document.getElementById(id);
  if(!el)continue;
  el.onclick=async()=>{
    const button=document.getElementById(id);button.disabled=true;
    try{const answer=await chrome.runtime.sendMessage({operation});document.getElementById("state").textContent=answer?.text || answer?.error || "Desktop unavailable.";await refresh();}
    catch{const s=await chrome.storage.local.get(["agentName"]);document.getElementById("state").textContent=`Open ${s.agentName||"Jarvis"} on this computer to reconnect.`;}
    finally{button.disabled=false;}
  };
}
async function refresh(){
  const state=await chrome.storage.local.get(["lastScan","lastError","coverage","activity","agentName"]);
  const name=state.agentName||"Jarvis";
  document.title=`${name} connection`;
  const h1=document.querySelector("header h1");if(h1)h1.textContent=name;
  document.getElementById("phase").textContent=state.activity?.phase||"Waiting";
  document.getElementById("state").textContent=state.lastError||state.activity?.detail||"Open the desktop app to connect.";
  document.getElementById("pulse").classList.toggle("working",["scanning","saving"].includes(state.activity?.phase));
  document.getElementById("coverage").textContent=state.lastError || (state.lastScan?"Last scan: "+new Date(state.lastScan).toLocaleString():"No verified scan yet.");
  document.getElementById("scope").textContent=state.coverage?`Rendered chats found: ${state.coverage.discovered}. Scanned this cycle: ${state.coverage.scanned}. Hidden history and media are not included.`:"Recent rendered chats are scanned in rotation. Coverage is shown after the first scan.";
}
refresh();
setInterval(refresh,1500);
