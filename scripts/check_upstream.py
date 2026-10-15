"""Explicit small live probe, never a whole-database download."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from regnetwork_mcp.upstream import OnlineAdapter


async def main():
    async with OnlineAdapter() as adapter:
        statistics, statistics_observation = await adapter.statistics()
        page = await adapter.scan("human", "core", "TP53", max_pages=1)
        edges, mapping_observation = await adapter.mapping("human", "core", ["TP53", "BAX", "CDKN1A", "MDM2", "MYC"])
        empty = await adapter.scan("human", "core", "REGNETWORK_MCP_NO_SUCH_SYMBOL_20261013", max_pages=1)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "kind": "live_upstream_probe_not_mcp_acceptance",
              "statistics": statistics, "statistics_observation": asdict(statistics_observation),
              "query": {"records": len(page.records), "candidates": page.upstream_total_candidates,
                        "complete": page.complete, "warnings": page.warnings,
                        "observations": [asdict(o) for o in page.observations],
                        "first_records": [r.to_dict() for r in page.records[:3]]},
              "mapping": {"records": [r.to_dict() for r in edges], "observation": asdict(mapping_observation)},
              "empty_query": {"records": len(empty.records), "complete": empty.complete,
                              "observations": [asdict(o) for o in empty.observations]}}
    destination = Path(".runtime/verification/live-upstream.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"query_rows": len(page.records), "query_complete": page.complete,
                      "mapping_rows": len(edges), "empty_rows": len(empty.records),
                      "empty_complete": empty.complete, "report": str(destination)}))


if __name__ == "__main__":
    asyncio.run(main())
