"""Test 3: clipboard_files puts a real file reference on the general pasteboard. The previous clipboard is restored."""
import asyncio, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import call, session

SNAPSHOT = """
ObjC.import('AppKit');
function run() { const pb = $.NSPasteboard.generalPasteboard; const out = [];
  const items = pb.pasteboardItems;
  for (let i = 0; i < items.count; i++) { const it = items.objectAtIndex(i); const d = {};
    const types = it.types; for (let j = 0; j < types.count; j++) { const t = types.objectAtIndex(j);
      const data = it.dataForType(t); if (data) d[ObjC.unwrap(t)] = ObjC.unwrap(data.base64EncodedStringWithOptions(0)); }
    out.push(d); }
  return JSON.stringify(out); }
"""
RESTORE = """
ObjC.import('AppKit');
function run(argv) { const pb = $.NSPasteboard.generalPasteboard; const items = JSON.parse(argv[0]); pb.clearContents;
  const objs = $.NSMutableArray.alloc.init;
  items.forEach(d => { const it = $.NSPasteboardItem.alloc.init;
    for (const t in d) it.setDataForType($.NSData.alloc.initWithBase64EncodedStringOptions(d[t], 0), t); objs.addObject(it); });
  if (items.length) pb.writeObjects(objs); return 'restored ' + items.length; }
"""


def osa(script, *args):
    return subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, *args], capture_output=True, text=True).stdout.strip()


async def main():
    snap = osa(SNAPSHOT)
    test_file = Path(__file__).parent / "_work" / "clip-test.txt"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("filebridge clipboard test\n")
    try:
        async with session() as s:
            r = await call(s, "clipboard_files", paths=[str(test_file), str(test_file.parent / "." / "clip-test.txt")])
            print("tool result:", r)
            # independent verification, outside the server code path
            print("independent read:", osa("""ObjC.import('AppKit'); const pb=$.NSPasteboard.generalPasteboard;
              JSON.stringify({types: ObjC.deepUnwrap(pb.types), plain: ObjC.unwrap(pb.stringForType('NSFilenamesPboardType')), fileurl: ObjC.unwrap(pb.stringForType('public.file-url'))})"""))
            try:
                await call(s, "clipboard_files", paths=["/nonexistent/file"])
            except RuntimeError as e:
                print("missing file ->", str(e)[:120])
            try:
                await call(s, "clipboard_files", paths=["~/.ssh"])
            except RuntimeError as e:
                print("credential path ->", str(e)[:140])
    finally:
        print("restore:", osa(RESTORE, snap))
        test_file.unlink()

asyncio.run(main())
