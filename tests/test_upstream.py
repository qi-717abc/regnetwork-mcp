import json
import httpx
import pytest

from regnetwork_mcp.records import UpstreamFormatError
from regnetwork_mcp.upstream import OnlineAdapter, UpstreamError
from regnetwork_mcp.cache import ResultCache


def record(index=0):
    return "\t".join(["TP53", "NCBI:7157", f"TEST_{index}", f"NCBI:{index+100}", "TF", "Gene", "core", "High", "1", "TRRUST", "0.8", "N/A"])


@pytest.mark.asyncio
async def test_paging_complete_and_complete_dataset_parameter():
    calls = []
    def handler(request):
        params = json.loads(request.content)
        calls.append(params)
        offset = 0 if params["page"] == 1 else 150
        length = 150 if offset == 0 else 1
        return httpx.Response(200, json={"result": [record(i) for i in range(offset, offset + length)], "totalResults": 151})
    async with OnlineAdapter(transport=httpx.MockTransport(handler), interval=0) as adapter:
        result = await adapter.scan("human", "complete", "TP53")
    assert result.complete and len(result.records) == 151
    assert result.scanned_pages == 2
    assert [c["page"] for c in calls] == [1, 2]
    assert all(c["dbType"] == "all" and c["rowsPerPage"] == 150 for c in calls)
    assert all(o.sha256 and o.retrieved_at for o in result.observations)


@pytest.mark.asyncio
async def test_page_cap_does_not_claim_completeness():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"result": [record()], "totalResults": 1000}))
    async with OnlineAdapter(transport=transport, interval=0) as adapter:
        result = await adapter.scan("human", "core", "TP53", max_pages=1)
    assert not result.complete
    assert result.upstream_total_candidates == 1000
    assert result.warnings == ["PAGE_LIMIT_REACHED"]


@pytest.mark.asyncio
async def test_later_http_failure_keeps_partial_and_failure_reason():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"result": [record()], "totalResults": 2}) if calls == 1 else httpx.Response(500)
    async with OnlineAdapter(transport=httpx.MockTransport(handler), interval=0) as adapter:
        result = await adapter.scan("human", "core", "TP53")
    assert not result.complete and len(result.records) == 1
    assert "UPSTREAM_HTTP_ERROR" in result.warnings[0]


@pytest.mark.asyncio
async def test_first_http_failure_is_error_not_empty():
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(500)), interval=0) as adapter:
        with pytest.raises(UpstreamError) as caught:
            await adapter.scan("human", "core", "TP53")
    assert caught.value.status_code == 500


@pytest.mark.asyncio
async def test_successful_zero_result_is_complete():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"result": [], "totalResults": 0}))
    async with OnlineAdapter(transport=transport, interval=0) as adapter:
        result = await adapter.scan("human", "core", "NO_SUCH_SYMBOL")
    assert result.complete and result.records == []


@pytest.mark.asyncio
async def test_early_empty_page_is_incomplete():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"result": [], "totalResults": 12}))
    async with OnlineAdapter(transport=transport, interval=0) as adapter:
        result = await adapter.scan("human", "core", "TP53")
    assert not result.complete
    assert "UPSTREAM_EARLY_EMPTY_PAGE" in result.warnings[0]


@pytest.mark.asyncio
async def test_changing_counts_cannot_claim_atomic_snapshot():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"result": [record(calls)], "totalResults": 10 + calls})
    async with OnlineAdapter(transport=httpx.MockTransport(handler), interval=0) as adapter:
        result = await adapter.scan("human", "core", "TP53")
    assert not result.complete and "UPSTREAM_COUNT_CHANGED" in result.warnings[0]


@pytest.mark.asyncio
async def test_html_success_is_format_error_not_zero_results():
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>error</html>")), interval=0) as adapter:
        with pytest.raises(UpstreamFormatError):
            await adapter.scan("human", "core", "TP53")


@pytest.mark.asyncio
async def test_retry_after_exceeding_budget_is_not_ignored():
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "100"}))
    async with OnlineAdapter(transport=transport, interval=0) as adapter:
        with pytest.raises(UpstreamError) as caught:
            await adapter.scan("human", "core", "TP53", budget_seconds=1)
    assert caught.value.code == "OPERATION_TIMEOUT"


@pytest.mark.asyncio
async def test_retryable_response_can_recover_without_whole_database_query():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "0"}) if calls == 1 else httpx.Response(200, json={"result": [], "totalResults": 0})
    async with OnlineAdapter(transport=httpx.MockTransport(handler), interval=0) as adapter:
        result = await adapter.scan("human", "core", "TP53")
    assert result.complete and calls == 2


@pytest.mark.asyncio
async def test_request_timeout_is_explicit():
    def handler(request):
        raise httpx.ReadTimeout("simulated", request=request)
    async with OnlineAdapter(transport=httpx.MockTransport(handler), interval=0) as adapter:
        with pytest.raises(UpstreamError) as caught:
            await adapter.scan("human", "core", "TP53")
    assert caught.value.code == "UPSTREAM_TIMEOUT"


@pytest.mark.asyncio
async def test_mapping_uses_list_and_preserves_annotated_edges():
    def handler(request):
        body = json.loads(request.content)
        assert body["format"] == "list" and body["genes"] == ["TP53", "BAX"]
        return httpx.Response(200, text=record(), headers={"Content-Type": "text/plain; charset=utf-8"})
    async with OnlineAdapter(transport=httpx.MockTransport(handler), interval=0) as adapter:
        records, observation = await adapter.mapping("human", "core", ["TP53", "BAX", "TP53"])
    assert len(records) == 1 and records[0].score == 0.8 and observation.sha256


@pytest.mark.asyncio
async def test_unknown_route_and_unbounded_search_rejected_before_network():
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r: pytest.fail("network should not be called")), interval=0) as adapter:
        with pytest.raises(ValueError):
            await adapter.scan("human", "core", "")
        with pytest.raises(ValueError):
            await adapter._read("https://untrusted.invalid", None, adapter.clock() + 60)


@pytest.mark.asyncio
async def test_statistics_checked_instead_of_silently_accepting_error_json():
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "failed"})), interval=0) as adapter:
        with pytest.raises(UpstreamFormatError):
            await adapter.statistics()


@pytest.mark.asyncio
async def test_cache_hit_preserves_time_hash_and_explicit_refresh_reads_online(tmp_path):
    cache = ResultCache(tmp_path/'cache.sqlite')
    calls=0
    def handler(request):
        nonlocal calls
        calls+=1
        return httpx.Response(200,json={'result':[record()], 'totalResults':1})
    async with OnlineAdapter(transport=httpx.MockTransport(handler),interval=0,cache=cache) as adapter:
        original=await adapter.scan('human','core','TP53')
        cached=await adapter.scan('human','core','TP53')
        assert calls==1 and cached.observations[0].data_origin=='cache'
        assert cached.observations[0].retrieved_at==original.observations[0].retrieved_at
        assert cached.observations[0].sha256==original.observations[0].sha256
        await adapter.scan('human','core','TP53',refresh=True)
    assert calls==2


@pytest.mark.asyncio
async def test_expired_cache_is_not_used_when_upstream_fails(tmp_path):
    now=[0.0]
    cache=ResultCache(tmp_path/'cache.sqlite',ttl=10,retention=20,clock=lambda:now[0])
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'result':[], 'totalResults':0})),interval=0,cache=cache) as adapter:
        await adapter.scan('human','core','TP53')
    now[0]=11
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r:httpx.Response(500)),interval=0,cache=cache) as adapter:
        with pytest.raises(UpstreamError):
            await adapter.scan('human','core','TP53')


@pytest.mark.asyncio
async def test_malformed_success_response_is_not_cached(tmp_path):
    cache=ResultCache(tmp_path/'cache.sqlite')
    async with OnlineAdapter(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'error':'bad'})),interval=0,cache=cache) as adapter:
        with pytest.raises(UpstreamFormatError):
            await adapter.scan('human','core','TP53')
    assert cache.statistics()['entries']==0
