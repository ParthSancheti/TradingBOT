from langgraph.checkpoint.memory import InMemorySaver

from jarvis.runtime import ReadTool, build_read_agent


async def test_dynamic_plan_recovers_from_unverified_read():
    async def plan(goal, observations):
        if not observations:
            return {"kind":"read","tool":"source_a","summary":"Read first permitted source"}
        if not observations[-1]["verified"]:
            return {"kind":"read","tool":"source_b","summary":"Try another permitted source"}
        return {"kind":"finish","summary":"Evidence prepared for review"}
    async def failed(arguments): return {"quantity":None}
    async def worked(arguments): return {"quantity":10}
    agent=build_read_agent(plan,{"source_a":ReadTool(failed,lambda x:x["quantity"] is not None),"source_b":ReadTool(worked,lambda x:x["quantity"]==10)},InMemorySaver())
    result=await agent.ainvoke({"goal":"Find stock","observations":[],"steps":0,"status":"NEW","decision":{}},config={"configurable":{"thread_id":"case-1"}})
    assert result["status"]=="ANALYSIS_READY_FOR_REVIEW"
    assert [o["verified"] for o in result["observations"]]==[False,True]


async def test_unregistered_tool_is_denied():
    async def plan(goal, observations): return {"kind":"read","tool":"send_money","summary":"Untrusted proposal"}
    agent=build_read_agent(plan,{},InMemorySaver())
    result=await agent.ainvoke({"goal":"Test","observations":[],"steps":0,"status":"NEW","decision":{}},config={"configurable":{"thread_id":"case-2"}})
    assert result["status"]=="CAPABILITY_DENIED"


async def test_extra_tool_arguments_do_not_reach_tool():
    called = False
    async def plan(goal, observations):
        if observations: return {"kind":"clarify","summary":"Need approved data"}
        return {"kind":"read","tool":"stock","arguments":{"sql":"DROP TABLE inventory"},"summary":"Untrusted arguments"}
    async def read(arguments):
        nonlocal called
        called=True
        return {}
    agent=build_read_agent(plan,{"stock":ReadTool(read,lambda x:True)},InMemorySaver())
    result=await agent.ainvoke({"goal":"Test","observations":[],"steps":0,"status":"NEW","decision":{}},config={"configurable":{"thread_id":"case-3"}})
    assert not called
    assert not result["observations"][0]["verified"]


async def test_repeated_failure_exhausts_budget():
    async def plan(goal, observations): return {"kind":"read","tool":"stock","summary":"Try permitted read"}
    async def read(arguments): return {"confirmed":False}
    agent=build_read_agent(plan,{"stock":ReadTool(read,lambda x:x["confirmed"])},InMemorySaver())
    result=await agent.ainvoke({"goal":"Test","observations":[],"steps":0,"status":"NEW","decision":{}},config={"configurable":{"thread_id":"case-4"}})
    assert result["status"]=="BUDGET_EXHAUSTED"
    assert len(result["observations"])==6
