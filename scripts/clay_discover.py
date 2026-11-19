"""Phase 2: connect to Clay MCP independently, run tools/list, write a capability report.
Read-only: no tool is called (allowlist is empty). Usage: python scripts/clay_discover.py [--reauth]
"""
import argparse, asyncio, json, logging, os, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from chanakya.clay.mcp_client import ClayMCPClient, CLAY_MCP_URL  # noqa: E402

DOCS = Path(__file__).resolve().parents[1] / "docs"


def render_md(server, tools):
    lines = [f"# Clay MCP capability report", "",
             f"- Endpoint: `{CLAY_MCP_URL}`", f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"- Server: {server}", f"- Tools discovered: **{len(tools)}**", "",
             "Discovered via `tools/list` only. No tool was invoked.", "",
             "| Tool | Read-only hint | Required args |", "|---|---|---|"]
    for t in tools:
        ro = (t.annotations or {}).get("readOnlyHint", (t.annotations or {}).get("read_only_hint", "n/a"))
        lines.append(f"| `{t.name}` | {ro} | {', '.join(t.input_schema.get('required', [])) or '-'} |")
    for t in tools:
        lines += ["", f"## `{t.name}`", "", t.description.strip() or "_(no description)_", "",
                  "```json", json.dumps(t.input_schema, indent=2), "```"]
        if t.annotations:
            lines += ["", f"Annotations: `{json.dumps(t.annotations)}`"]
    return "\n".join(lines) + "\n"


async def main(reauth: bool):
    client = ClayMCPClient(os.getenv("CLAY_MCP_URL") or CLAY_MCP_URL)
    if reauth:
        client.storage.clear()
    async with client:
        tools = await client.list_tools()
        si = getattr(client.server_info, "server_info", None) or getattr(client.server_info, "serverInfo", None)
        server = f"{getattr(si, 'name', '?')} {getattr(si, 'version', '')}".strip()
    DOCS.mkdir(exist_ok=True)
    (DOCS / "clay_capabilities.json").write_text(json.dumps(
        {"endpoint": CLAY_MCP_URL, "server": server,
         "tools": [t.__dict__ for t in tools]}, indent=2), encoding="utf-8")
    (DOCS / "clay_capabilities.md").write_text(render_md(server, tools), encoding="utf-8")
    print(f"Connected to {server}. {len(tools)} tools:")
    for t in tools:
        print(f"  - {t.name}: {t.description.strip().splitlines()[0][:100] if t.description else ''}")
    print("Report: docs/clay_capabilities.md")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--reauth", action="store_true")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("httpx2").setLevel(logging.WARNING)
    asyncio.run(main(ap.parse_args().reauth))
