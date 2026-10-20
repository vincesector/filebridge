"""Tiny stdio MCP client helper for the tests (spawns the real server)."""
import json
import shutil
from pathlib import Path
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command=shutil.which("filebridge") or str(Path(__file__).parents[1] / ".venv" / "bin" / "filebridge"), args=["mcp"])


@asynccontextmanager
async def session():
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


async def call(s, name, **args):
    res = await s.call_tool(name, args)
    if res.is_error:
        raise RuntimeError(f"{name} -> {res.content[0].text}")
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)
