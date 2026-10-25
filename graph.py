from __future__ import annotations

from datetime import datetime
from typing import Annotated, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .agents import INTAKE_PROMPT, REGISTRAR_PROMPT, ToolAgent
from .models import IntakeResult, Item, RegistrarResult
from .storage import journal_from_result, pending_items, pending_query, registrar_tools, render_reply


class WarehouseState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    pending: list[dict]
    items: list[dict]
    item_record_ids: list[str | None]
    question: str | None
    session_id: str
    event_id: str
    user_message: str
    receipt: dict
    reply: str


def build_graph(settings, checkpointer, mcp, intake_model, registrar_model, trace_factory):
    intake = ToolAgent("intake_agent", INTAKE_PROMPT, intake_model)
    registrar = ToolAgent("registrar_agent", REGISTRAR_PROMPT, registrar_model)

    async def intake_node(state: WarehouseState):
        trace = trace_factory()
        saved = journal_from_result(await mcp.call("read_query", {
            "query": pending_query(state["session_id"], settings.warehouse_id)}, trace))
        pending = pending_items(saved, state["session_id"], settings.warehouse_id)
        history = [{"role": m.type, "text": m.content} for m in state.get("messages", [])[-10:]]
        trace.emit("memory_loaded", session_id=state["session_id"],
                   pending=pending, history_messages=len(history))
        result, _ = await intake.run({"user_message": state["user_message"],
            "pending": pending, "history": history}, [], trace)
        parsed = IntakeResult.model_validate(result)
        known = {entry["pending_id"]: entry for entry in pending}
        items, references = [], []
        for entry in parsed.items:
            data = entry.model_dump(exclude={"pending_id"})
            if entry.pending_id:
                if entry.pending_id not in known or "новая заявка" in " ".join(state["user_message"].casefold().split()):
                    raise ValueError("Уточнение не связано с незавершённой заявкой этой сессии")
                previous = known[entry.pending_id]
                for field in Item.model_fields:
                    old, new = previous[field], data[field]
                    if old is not None:
                        normalize = lambda value: " ".join(str(value).split()).casefold()
                        if new is not None and normalize(old) != normalize(new):
                            raise ValueError("Уточнение противоречит ранее сохранённым данным товара")
                        data[field] = old
            items.append(Item.model_validate(data).model_dump())
            references.append(entry.pending_id)
        return {"items": items, "item_record_ids": references, "question": parsed.question, "pending": pending}

    async def registrar_node(state: WarehouseState):
        trace = trace_factory()
        items = [Item.model_validate(i) for i in state["items"]]
        tools, receipt = registrar_tools(items, state["session_id"], state["event_id"],
            settings, mcp, trace, datetime.now(ZoneInfo(settings.timezone)), state.get("item_record_ids"))
        output, used = await registrar.run({"items": state["items"]}, tools, trace)
        RegistrarResult.model_validate(output)
        if not receipt["ready"] or "register_discrepancies" not in used:
            raise RuntimeError("Агент не выполнил обязательную проверку и регистрацию")
        # User-visible success is based on tool receipts, never just on model prose.
        reply = render_reply(receipt, state.get("question"))
        return {"receipt": receipt, "reply": reply, "pending": receipt["pending"],
                "messages": [AIMessage(content=reply)]}

    graph = StateGraph(WarehouseState)
    graph.add_node("intake_agent", intake_node)
    graph.add_node("registrar_agent", registrar_node)
    graph.add_edge(START, "intake_agent")
    graph.add_edge("intake_agent", "registrar_agent")
    graph.add_edge("registrar_agent", END)
    return graph.compile(checkpointer=checkpointer)

