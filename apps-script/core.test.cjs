const test=require("node:test"),assert=require("node:assert/strict");
const {execute}=require("./Core.js");
class Store {
  constructor(){this.records={};this.ops={};this.failAt=0;this.writes=0;}
  get(c,id){return structuredClone(this.records[c+":"+id] || null);}
  list(c){return Object.entries(this.records).filter(([k])=>k.startsWith(c+":")).map(([,v])=>structuredClone(v));}
  set(c,id,r){this.writes++;if(this.writes===this.failAt)throw Error("SIMULATED_CRASH");this.records[c+":"+id]=structuredClone(r);}
  operation(id){return this.ops[id]?structuredClone(this.ops[id]):null;}
  prepared(){return Object.values(this.ops).filter(x=>x.status==="PREPARED").map(x=>structuredClone(x));}
  prepare(op){this.ops[op.id]=structuredClone(op);}
  commit(id){this.ops[id].status="COMMITTED";}
}
function req(id,changes){return{request_id:id,action:"mutate",payload:{changes}};}
test("1000 repeated deliveries create one logical record/revision",()=>{const s=new Store();const r=req("repeat",[{collection:"messages",id:"m1",expected_revision:0,data:{text:"WTB"}}]);for(let i=0;i<1000;i++)execute(s,r,"hash","now");assert.equal(s.get("messages","m1").revision,1);assert.equal(Object.keys(s.ops).length,1);});
test("recover crash between batch writes before serving readers",()=>{const s=new Store();s.failAt=2;const r=req("batch",[{collection:"offers",id:"offer1",expected_revision:0,data:{quantity:10}},{collection:"tasks",id:"task1",expected_revision:0,data:{status:"REVIEW"}}]);assert.throws(()=>execute(s,r,"hash","now"),/CRASH/);s.failAt=0;const out=execute(s,{action:"list",payload:{collection:"offers"}},"read","later");assert.equal(out.records.length,1);assert.equal(s.get("tasks","task1").revision,1);assert.equal(s.ops.batch.status,"COMMITTED");});
test("stale revision and changed idempotency payload fail",()=>{const s=new Store();const r=req("a",[{collection:"configuration",id:"business",expected_revision:0,data:{margin:5}}]);execute(s,r,"first","now");assert.throws(()=>execute(s,r,"different","now"),/IDEMPOTENCY/);assert.throws(()=>execute(s,{...r,request_id:"b"},"new","now"),/REVISION/);});
test("manual conflicting edit quarantines incomplete transaction",()=>{const s=new Store();s.failAt=1;const r=req("a",[{collection:"offers",id:"o",expected_revision:0,data:{quantity:10}}]);assert.throws(()=>execute(s,r,"a","now"));s.records["offers:o"]={id:"o",revision:9,data:{quantity:999}};assert.throws(()=>execute(s,{action:"health"},"h","now"),/RECOVERY_CONFLICT/);});
