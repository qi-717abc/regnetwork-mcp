"""Verify the official-file download tool through a real MCP stdio client."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--species", choices=["human", "mouse"], default="human")
    parser.add_argument("--dataset", choices=["core", "complete"], default="core")
    parser.add_argument("--file-category", default="tf_target")
    parser.add_argument("--data-root", type=Path, default=root / ".runtime/verification/s17-live-data")
    parser.add_argument("--report", type=Path, default=root / ".runtime/verification/s17-mcp-download.json")
    parser.add_argument("--cwd", type=Path, default=root / ".runtime/verification/s17-server-cwd")
    parser.add_argument("--command", default=sys.executable)
    parser.add_argument("--server-arg", action="append", dest="server_args")
    args = parser.parse_args()
    args.data_root = args.data_root.resolve()
    args.cwd.mkdir(parents=True, exist_ok=True)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "kind": "official_file_download_actual_mcp_stdio",
        "species": args.species,
        "dataset": args.dataset,
        "file_category": args.file_category,
        "data_root": str(args.data_root),
        "server_cwd": str(args.cwd.resolve()),
        "command": args.command,
        "server_args": args.server_args or ["-m", "regnetwork_mcp"],
    }
    env = os.environ.copy()
    env["REGNETWORK_MCP_ROOT"] = str(args.data_root)
    try:
        async with Client(StdioServerParameters(command=args.command,
                args=args.server_args or ["-m", "regnetwork_mcp"], cwd=args.cwd, env=env)) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "download_official_file" in names
            result = await client.call_tool("download_official_file", {
                "species": args.species,
                "dataset": args.dataset,
                "file_category": args.file_category,
            })
            assert not result.is_error, result.structured_content
            payload = result.structured_content
            assert payload and payload["status"] == "ok", payload
            path = Path(payload["data"]["file_path"])
            manifest_path = Path(payload["data"]["manifest_path"])
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert digest == payload["data"]["sha256"] == manifest["sha256"]
            assert manifest["upstream_checksum_provided"] is False
            assert content.startswith(b"7z\xbc\xaf'\x1c")
            assert payload["provenance"]["official_download_page"] == "https://www.zpliulab.cn/RegNetwork/download"
            report.update({
                "tools_listed": sorted(names),
                "result": payload,
                "archive_bytes": len(content),
                "archive_sha256_readback": digest,
                "manifest_readback": True,
                "archive_signature_valid": True,
                "passed": True,
            })
            print(json.dumps({"passed": True, "filename": path.name,
                              "bytes": len(content), "sha256": digest}, ensure_ascii=False), flush=True)
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
