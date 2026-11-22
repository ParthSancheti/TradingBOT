const test=require("node:test");const assert=require("node:assert/strict");const {execute}=require("./Core.js");

test("10000 writes with duplicate delivery and 100 interrupted commits converge",()=>{
  const records=new Map(),operations=new Map();let crash=false,crashes=0;
  const clone=value=>structuredClone(value);
  const store={
    get:(c,id)=>clone(records.get(c+":"+id)||null),list:c=>[...records].filter(([key])=>key.startsWith(c+":")).map(([,value])=>clone(value)),
    set:(c,id,record)=>{records.set(c+":"+id,clone(record));if(crash){crash=false;crashes++;throw Error("INJECTED_CRASH");}},
    operation:id=>clone(operations.get(id)||null),prepared:()=>[...operations.values()].filter(o=>o.status==="PREPARED").map(clone),
    prepare:op=>operations.set(op.id,clone(op)),commit:id=>operations.get(id).status="COMMITTED",
  };
  for(let i=0;i<10000;i++){
    const id="source-"+(i%100),old=store.get("messages",id),revision=old?.revision||0;
    const request={request_id:"operation-"+i,action:"mutate",payload:{changes:[{collection:"messages",id,expected_revision:revision,data:{sequence:i,text:"WTB test fixture only"}}]}};
    crash=i%100===0;let answer;
    try{answer=execute(store,request,"hash-"+i,"2026-11-09T00:00:00Z");}
    catch(error){assert.equal(error.message,"INJECTED_CRASH");answer=execute(store,request,"hash-"+i,"2026-11-09T00:00:00Z");}
    assert.equal(answer.records[0].revision,revision+1);
    assert.deepEqual(execute(store,request,"hash-"+i,"2026-11-09T00:00:00Z"),answer);
  }
  assert.equal(crashes,100);assert.equal(store.prepared().length,0);assert.equal(records.size,100);
  assert.ok([...records.values()].every(r=>r.revision===100));
});
