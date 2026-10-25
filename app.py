from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from filelock import FileLock
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .agents import make_model
from .graph import build_graph
from .mcp_client import connect_sqlite
from .storage import initialize_database
from .trace import Trace


class WarehouseApp:
    def __init__(self, settings, graph, verbose=False):
        self.settings, self.graph, self.verbose = settings, graph, verbose
        self.trace = None
        self._async_lock = asyncio.Lock()

    async def process(self, text: str, session_id: str, event_id: str | None = None) -> dict:
        if not text.strip() or len(text) > 12000:
            raise ValueError("Сообщение должно содержать от 1 до 12000 символов")
        if not session_id or len(session_id) > 200:
            raise ValueError("Нужен идентификатор сессии длиной до 200 символов")
        # All cooperating instances serialize the full read/modify/write cycle.
        async with self._async_lock:
            lock = FileLock(str(self.settings.data_dir / "processing.lock"), timeout=300, thread_local=False)
            await asyncio.to_thread(lock.acquire)
            try:
                self.trace = Trace(self.settings.data_dir / "traces", self.verbose)
                self.trace.emit("run_start", mode="live_llm", session_id=session_id)
                result = await self.graph.ainvoke({"user_message": text, "session_id": session_id,
                    "event_id": event_id or uuid4().hex, "messages": [HumanMessage(content=text)],
                    "items": [], "item_record_ids": [], "question": None, "receipt": {}, "reply": ""},
                    config={"configurable": {"thread_id": session_id}})
                self.trace.emit("run_end", reply=result["reply"])
                result["trace_path"] = str(self.trace.path)
                return result
            except Exception as exc:
                if self.trace:
                    self.trace.emit("run_error", error_type=type(exc).__name__)
                raise
            finally:
                await asyncio.to_thread(lock.release)


@asynccontextmanager
async def open_app(settings, verbose=False):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_lock = FileLock(str(settings.data_dir / "processing.lock"), timeout=300, thread_local=False)
    await asyncio.to_thread(bootstrap_lock.acquire)
    try:
        initialize_database(settings.data_dir / "warehouse.sqlite")
    finally:
        await asyncio.to_thread(bootstrap_lock.release)
    models = (make_model(settings), make_model(settings))
    async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "memory.sqlite")) as saver:
        async with connect_sqlite(settings.data_dir / "warehouse.sqlite") as mcp:
            app = WarehouseApp(settings, None, verbose)
            app.graph = build_graph(settings, saver, mcp, *models, lambda: app.trace)
            yield app