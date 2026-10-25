from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class SqliteClient:
    def __init__(self, session: ClientSession):
        self.session = session

    async def call(self, name: str, arguments: dict, trace) -> str:
        query = arguments.get("query", "")
        shown = query if len(query) <= 240 else query[:240] + " ..."
        trace.emit("mcp_call", server="mcp-server-sqlite@2025.4.25", tool=name,
                   arguments={"query": shown}, query_truncated=len(query) > 240)
        result = await self.session.call_tool(name, arguments)
        content = "\n".join(block.text for block in result.content if block.type == "text")
        # This reference server can report SQL errors as text with isError=False.
        if result.isError or content.startswith(("Database error:", "Error:")):
            raise RuntimeError(f"MCP {name}: {content}")
        trace.emit("mcp_result", tool=name, success=True)
        return content


@asynccontextmanager
async def connect_sqlite(database: Path):
    if find_spec("mcp_server_sqlite") is None:
        raise RuntimeError("MCP-сервер не установлен. Запусти run.cmd для установки зависимостей.")
    # Invoke the published package's entry point; no local server is implemented.
    params = StdioServerParameters(command=sys.executable,
        args=["-c", "from mcp_server_sqlite import main; main()", "--db-path", str(database)],
        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = {tool.name for tool in (await session.list_tools()).tools}
            if not {"read_query", "write_query"} <= available:
                raise RuntimeError("MCP-сервер не предоставляет необходимые SQL-инструменты.")
            yield SqliteClient(session)
