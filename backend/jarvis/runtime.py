"""Bounded, read-only adaptive episodes. Not enabled as a live task runner yet."""
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["read", "finish", "clarify"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=1000)


class Episode(TypedDict):
    goal: str
    observations: list[dict]
    steps: int
    status: str
    decision: dict


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ReadTool:
    invoke: Callable[[dict], Awaitable[dict]]
    verify: Callable[[dict], bool]
    argument_model: type[BaseModel] = EmptyArguments


def build_read_agent(planner, tools: dict[str, ReadTool], checkpointer):
    if checkpointer is None:
        raise ValueError("An explicit checkpointer is required")

    async def plan(state):
        if state["steps"] >= 6:
            return {"status":"BUDGET_EXHAUSTED"}
        try:
            value = await asyncio.wait_for(planner(state["goal"], tuple(state["observations"])), timeout=15)
            decision = Decision.model_validate(value)
        except Exception:
            return {"status":"PLANNER_UNAVAILABLE"}
        return {"decision":decision.model_dump(), "steps":state["steps"]+1, "status":"PLANNED"}

    async def act(state):
        d = Decision.model_validate(state["decision"])
        if d.kind == "clarify":
            return {"status":"NEEDS_CLARIFICATION"}
        if d.kind == "finish":
            # A model's completion assertion does not prove a commercial outcome.
            return {"status":"ANALYSIS_READY_FOR_REVIEW"}
        if d.tool not in tools:
            return {"status":"CAPABILITY_DENIED"}
        try:
            tool = tools[d.tool]
            arguments = tool.argument_model.model_validate(d.arguments).model_dump()
            result = await asyncio.wait_for(tool.invoke(arguments), timeout=10)
            if not isinstance(result, dict) or len(json.dumps(result).encode("utf-8")) > 65536:
                raise ValueError("Tool result exceeds permitted schema/size")
            observation = {"tool":d.tool,"verified":bool(tool.verify(result)),"result":result}
        except Exception:
            observation = {"tool":d.tool,"verified":False,"error":"Tool did not return a verified result"}
        return {"observations":[*state["observations"],observation], "status":"OBSERVED"}

    graph = StateGraph(Episode)
    graph.add_node("plan", plan)
    graph.add_node("act", act)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", lambda s: "act" if s["status"] == "PLANNED" else END)
    graph.add_conditional_edges("act", lambda s: "plan" if s["status"] == "OBSERVED" else END)
    return graph.compile(checkpointer=checkpointer)
