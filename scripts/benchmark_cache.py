"""Repeatable local fixture benchmark; results are engineering measurements only."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

import httpx

from regnetwork_mcp.cache import ResultCache
from regnetwork_mcp.records import parse_interaction
from regnetwork_mcp.service import ResearchService
from regnetwork_mcp.upstream import OnlineAdapter, ReadObservation


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    results = {'fixture_only': True, 'cache_get': []}
    for mib in [0, 10, 45]:
        cache = ResultCache(args.root/f'cache-{mib}.sqlite')
        for i in range(mib*2):
            cache.put(str(i), 'x'*(512*1024-2))
        cache.put('probe', {'value': 1})
        timings = []
        for _ in range(100):
            start = time.perf_counter()
            assert cache.get('probe').payload == {'value': 1}
            timings.append((time.perf_counter()-start)*1000)
        results['cache_get'].append({'payload_mib': mib, 'median_ms': statistics.median(timings),
                                     'p95_ms': sorted(timings)[94]})
    service = ResearchService(args.root/'service', adapter=OnlineAdapter(interval=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    records = [parse_interaction('\t'.join(['TP53','NCBI:7157',f'GENE{i}',f'NCBI:{i+100}',
                'TF','Gene','core','High','1','TRRUST','0.8','123(Experimental Research)']), 'human') for i in range(500)]
    observation = ReadObservation('/fixture', {}, '2026-10-15T00:00:00+00:00', '0'*64, 200)
    start = time.perf_counter()
    await asyncio.to_thread(service._remember_records, records, 'human', 'core', [observation])
    results['remember_500_records_ms'] = (time.perf_counter()-start)*1000
    results['service_cache'] = service.cache.statistics()
    await service.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results))


if __name__ == '__main__':
    asyncio.run(main())
