const test=require("node:test");const assert=require("node:assert/strict");const vm=require("node:vm");const fs=require("node:fs");const crypto=require("node:crypto");
const secret="test-signing-secret-never-live-000000000000";
function digest(value){return crypto.createHmac("sha256",secret).update(value).digest("hex");}
function harness(){
  let accesses=0;
  const context=vm.createContext({
    Utilities:{Charset:{UTF_8:"UTF-8"},DigestAlgorithm:{SHA_256:"sha256"},computeHmacSha256Signature:(v,s)=>Array.from(crypto.createHmac("sha256",s).update(v).digest()),computeDigest:(_,v)=>Array.from(crypto.createHash("sha256").update(v).digest()),base64Encode:v=>Buffer.from(v).toString("base64"),base64Decode:v=>Array.from(Buffer.from(v,"base64")),newBlob:b=>({getDataAsString:()=>Buffer.from(b).toString("utf8")})},
    PropertiesService:{getScriptProperties:()=>({getProperty:k=>k==="JARVIS_SIGNING_SECRET"?secret:"book-id"})},
    LockService:{getScriptLock:()=>({tryLock:()=>true,hasLock:()=>true,releaseLock:()=>{}})},
    SpreadsheetApp:{openById:()=>{accesses++;return {getUrl:()=>"https://docs.google.com/spreadsheets/d/test-book/edit"}; }},
    ContentService:{MimeType:{JSON:"json"},createTextOutput:text=>({text,setMimeType(){return this;}})},
    execute:(_,request)=>({margin:request.payload.margin,label:request.payload.label}),
  });
  vm.runInContext(fs.readFileSync(__dirname+"/Code.gs","utf8"),context);
  return {post:wrapper=>context.doPost({postData:{contents:JSON.stringify(wrapper)}}),accesses:()=>accesses};
}
function envelope(overrides={}){
  const body=Buffer.from(JSON.stringify({version:1,request_id:"a".repeat(32),nonce:"b".repeat(32),timestamp:Math.floor(Date.now()/1000),action:"health",payload:{margin:5.0,label:"₹ stock 🌸"},...overrides})).toString("base64");
  return {body,signature:digest(body)};
}
test("Apps Script verifies the same raw wire bytes as Python, including Unicode/floats",()=>{
  const h=harness(),wire=JSON.parse(h.post(envelope()).text);
  assert.equal(wire.signature,digest(wire.body));const answer=JSON.parse(Buffer.from(wire.body,"base64"));
  assert.equal(answer.result.margin,5);assert.equal(answer.result.label,"₹ stock 🌸");assert.equal(h.accesses(),1);
});
test("forgery and stale replay never touch workbook",()=>{
  const h=harness(),forged=envelope();forged.signature="0".repeat(64);
  assert.equal(JSON.parse(h.post(forged).text).code,"NOT_AUTHORIZED");
  assert.equal(JSON.parse(h.post(envelope({timestamp:1})).text).code,"NOT_AUTHORIZED");assert.equal(h.accesses(),0);
});
