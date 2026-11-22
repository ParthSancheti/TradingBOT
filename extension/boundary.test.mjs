import test from "node:test";
import assert from "node:assert/strict";
import {validDocumentSender,validHello} from "./boundary.mjs";

test("renderer observations bind origin, tab, top frame and document", () => {
  const binding={tabId:8,documentId:"current"};
  const sender={tab:{id:8},documentId:"current",frameId:0,url:"https://web.whatsapp.com/"};
  assert.equal(validDocumentSender(sender,binding),true);
  for (const patch of [{url:"https://evil.example"},{url:"https://web.whatsapp.com.evil.example/"},{frameId:2},{documentId:"old"},{tab:{id:9}}]) assert.equal(validDocumentSender({...sender,...patch},binding),false);
});
test("native handshake must bind session and never claim send capability",()=>{
  const result={protocol:"jarvis.bridge.v1",operation:"hello_result",session_id:"session",state:"PAIRING_REQUIRED",can_send:false,can_read_chats:false};
  assert.equal(validHello(result,"session"),true);
  assert.equal(validHello(result,"other"),false);
  assert.equal(validHello({...result,can_send:true},"session"),false);
});
