"""One live mapping, then a network-disabled cache replay and three exports."""
import asyncio
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import httpx
import networkx as nx

from regnetwork_mcp.analysis import extract_induced
from regnetwork_mcp.cache import ResultCache
from regnetwork_mcp.exports import export_network
from regnetwork_mcp.upstream import OnlineAdapter


async def main():
    genes=['TP53','BAX','CDKN1A','MDM2','MYC']
    database=Path('.runtime/verification/s06-cache.sqlite')
    cache=ResultCache(database)
    async with OnlineAdapter(cache=cache) as adapter:
        original,observation=await adapter.mapping('human','core',genes,refresh=True)
    def blocked_network(request):
        raise AssertionError('cache replay must not call the network')
    async with OnlineAdapter(cache=ResultCache(database),transport=httpx.MockTransport(blocked_network)) as adapter:
        replay,replay_observation=await adapter.mapping('human','core',genes)
    assert replay_observation.data_origin=='cache'
    assert observation.sha256==replay_observation.sha256 and observation.retrieved_at==replay_observation.retrieved_at
    network=extract_induced(replay,genes)
    network['provenance']={'observations':[asdict(observation)],'query_parameters':{'species':'human','dataset':'core','symbols':genes}}
    artifacts=[]
    for format_name in ['tsv','json','graphml']:
        artifact=export_network(network,Path('exports/s06-verification'),format_name)
        path=Path(artifact['path'])
        assert hashlib.sha256(path.read_bytes()).hexdigest()==artifact['sha256']
        if format_name=='tsv':
            with path.open(encoding='utf-8',newline='') as stream:
                rows=len(list(csv.DictReader(stream,delimiter='\t')))
        elif format_name=='json':
            rows=len(json.loads(path.read_text(encoding='utf-8'))['records'])
        else:
            graph=nx.read_graphml(path,force_multigraph=True)
            assert graph.is_directed()
            rows=graph.number_of_edges()
        assert rows==len(original)
        artifact['readback_rows']=rows
        artifacts.append(artifact)
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'kind':'live_cache_and_export_not_mcp_acceptance',
            'original_observation':asdict(observation),'replay_observation':asdict(replay_observation),
            'replay_network_disabled':True,'cache_statistics':cache.statistics(),'artifacts':artifacts}
    path=Path('.runtime/verification/s06-live-cache-exports.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'cached_origin':replay_observation.data_origin,'preserved_source_time':observation.retrieved_at==replay_observation.retrieved_at,
                      'exports':[{'format':a['format'],'readback_rows':a['readback_rows']} for a in artifacts], 'report':str(path)}))


if __name__=='__main__':
    asyncio.run(main())
