export async function selectWhatsAppTab(chrome, {focus=false, dedupe=false}={}) {
  const {ownedTab}=await chrome.storage.local.get(["ownedTab"]);
  const tabs=await chrome.tabs.query({url:"https://web.whatsapp.com/*"});
  // Keep the same chosen tab even when another WhatsApp tab becomes active.
  let tab=tabs.find(candidate=>candidate.id===ownedTab);
  if(!tab)tab=tabs.find(candidate=>candidate.active)||tabs[0];
  if(!tab)tab=await chrome.tabs.create({url:"https://web.whatsapp.com/",active:focus});
  if(dedupe && tabs.length>1 && typeof chrome.tabs.remove==="function"){
    const extras=tabs.filter(c=>c.id!==tab.id).map(c=>c.id);
    if(extras.length) await chrome.tabs.remove(extras).catch(()=>{});
  }
  await chrome.storage.local.set({ownedTab:tab.id,tabDecision:{
    tabId:tab.id,action:tabs.some(candidate=>candidate.id===tab.id)?"reused":"created",
    duplicates:Math.max(0,tabs.length-1),at:Date.now(),
  }});
  if(focus){
    await chrome.tabs.update(tab.id,{active:true});
    await chrome.windows.update(tab.windowId,{focused:true});
  }
  return tab;
}
