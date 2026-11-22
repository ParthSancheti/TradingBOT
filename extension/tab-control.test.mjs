import test from "node:test";
import assert from "node:assert/strict";
import {selectWhatsAppTab} from "./tab-control.mjs";

function browser(tabs,ownedTab){
  const storage={ownedTab};
  const created=[],focused=[];
  return {storage,created,focused,chrome:{
    storage:{local:{get:async()=>storage,set:async value=>Object.assign(storage,value)}},
    tabs:{
      query:async()=>tabs,
      create:async value=>{created.push(value);return {id:99,windowId:4,...value};},
      update:async(id,options)=>focused.push({id,...options}),
      remove:async ids=>{for(let i=tabs.length-1;i>=0;i--)if(ids.includes(tabs[i].id))tabs.splice(i,1);},
    },
    windows:{update:async(id,options)=>focused.push({window:id,...options})},
  }};
}

test("dedupe mode removes extra duplicate WhatsApp tabs leaving one working page",async()=>{
  const tabs=[{id:7,windowId:2,active:false},{id:8,windowId:3,active:true}];
  const b=browser(tabs,7);
  const chosen=await selectWhatsAppTab(b.chrome,{dedupe:true});
  assert.equal(chosen.id,7);
  assert.deepEqual(tabs.map(t=>t.id),[7]);
});

test("scanning, setup and opening retain the chosen tab despite another active tab",async()=>{
  const b=browser([{id:7,windowId:2,active:false},{id:8,windowId:3,active:true}],7);
  assert.equal((await selectWhatsAppTab(b.chrome)).id,7);
  assert.equal((await selectWhatsAppTab(b.chrome,{focus:true})).id,7);
  assert.equal(b.created.length,0);
  assert.equal(b.storage.tabDecision.duplicates,1);
  assert.deepEqual(b.focused,[{id:7,active:true},{window:2,focused:true}]);
});

test("closed selected tab recovers to an existing WhatsApp tab",async()=>{
  const b=browser([{id:8,windowId:3,active:true}],7);
  assert.equal((await selectWhatsAppTab(b.chrome)).id,8);
  assert.equal(b.created.length,0);
});

test("create one WhatsApp tab only when no existing WhatsApp tab is available",async()=>{
  const tabs=[];
  const b=browser(tabs,7);
  const created=await selectWhatsAppTab(b.chrome);
  tabs.push(created);
  assert.equal((await selectWhatsAppTab(b.chrome)).id,99);
  assert.equal(b.created.length,1);
});
