"""Test 4: list tools over real MCP stdio and print each signature, annotations and required args."""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import session


def sig(schema):
    req = set(schema.get("required", []))
    parts = []
    for name, p in schema.get("properties", {}).items():
        t = p.get("type") or "|".join(x.get("type", "?") for x in p.get("anyOf", [])) or ("enum" if "enum" in p else "?")
        if "enum" in p:
            t = "/".join(map(str, p["enum"]))
        if t == "array":
            t = f"list[{p.get('items', {}).get('type', '?')}]"
        d = "" if name in req else f"={p.get('default')!r}"
        parts.append(f"{name}: {t}{d}")
    return ", ".join(parts)


async def main():
    async with session() as s:
        info = s.get_server_info() if hasattr(s, "get_server_info") else None
        tools = (await s.list_tools()).tools
        print(f"{len(tools)} tools over stdio" + (f" (server: {info})" if info else ""))
        for t in tools:
            a = t.annotations
            flags = f"ro={a.read_only_hint} destructive={a.destructive_hint} idem={a.idempotent_hint} open={a.open_world_hint}" if a else ""
            print(f"- {t.name}({sig(t.input_schema)})  [{flags}]")
            assert t.description, t.name
        names = {t.name for t in tools}
        spec = {"remotes", "transfer", "job_status", "jobs", "cancel", "ls", "link", "mkdir", "stat", "setup_instructions", "browser_launch", "browser_tabs",
                "browser_set_files", "browser_drop_files", "browser_click", "browser_wait", "local_copy", "clipboard_files", "reveal"}
        print("spec tools missing:", spec - names, "| extras:", names - spec)
        assert not {"delete", "rm", "remove", "purge"} & names
        print("no delete-type tool exposed: OK")

asyncio.run(main())
