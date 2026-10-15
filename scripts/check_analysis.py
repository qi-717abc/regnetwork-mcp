"""Live bounded exact-symbol and induced-network acceptance, not MCP acceptance."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from regnetwork_mcp.analysis import extract_induced, resolve_scan, select_interactions, summarize_subnetwork
from regnetwork_mcp.upstream import OnlineAdapter


async def main():
    async with OnlineAdapter() as adapter:
        scan = await adapter.scan("human", "core", "TP53")
        edges, observation = await adapter.mapping("human", "core", ["TP53","BAX","CDKN1A","MDM2","MYC","REGNETWORK_MCP_NO_SUCH_SYMBOL_20261013"])
    selection = select_interactions(scan, symbol="TP53", limit=5)
    network = extract_induced(edges,["TP53","BAX","CDKN1A","MDM2","MYC","REGNETWORK_MCP_NO_SUCH_SYMBOL_20261013"])
    summary = summarize_subnetwork(network)
    report = {"checked_at":datetime.now(timezone.utc).isoformat(),"kind":"live_query_and_graph_not_mcp_acceptance",
              "candidate_scan_complete":scan.complete,"candidate_count":scan.upstream_total_candidates,
              "scanned_pages":scan.scanned_pages,"observations":[asdict(o) for o in scan.observations],
              "selection":selection,"resolution":resolve_scan(scan,"TP53"),
              "network":network,"summary":summary,"mapping_observation":asdict(observation)}
    path=Path('.runtime/verification/s04-s05-live-analysis.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({"candidate_complete":scan.complete,"pages":scan.scanned_pages,
                      "exact_matches":selection['matched_total'],"resolution":report['resolution']['state'],
                      "nodes":summary['node_count'],"unique_edges":summary['unique_endpoint_pair_count'],
                      "self_loops":summary['self_loop_count'],"unverified_input":network['input_states'][-1]['state'],
                      "report":str(path)}))


if __name__=='__main__':
    asyncio.run(main())
