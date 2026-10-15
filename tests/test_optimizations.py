import asyncio
import hashlib
import json
import gzip
import os
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

from regnetwork_mcp.downloads import OfficialDownloadError, OfficialFileDownloader, SEVEN_ZIP_MAGIC
from regnetwork_mcp.fileio import async_file_lock, file_lock, publish_pair, recover_pair, run_sync, stage_bytes
from regnetwork_mcp.cache import CacheError, ResultCache
from regnetwork_mcp.analysis import extract_induced, select_interactions
from regnetwork_mcp.records import parse_interaction
from regnetwork_mcp.service import ResearchService
from regnetwork_mcp.upstream import OnlineAdapter, ScanResult, UpstreamError, UpstreamFormatError


def row(index=0):
    return '\t'.join(['TP53','NCBI:7157',f'GENE{index}',f'NCBI:{index+100}',
                     'TF','Gene','core','High','1','TRRUST','0.8','123(Experimental Research)'])


@pytest.mark.asyncio
async def test_failed_manifest_commit_does_not_leave_an_orphan_download(tmp_path, monkeypatch):
    import regnetwork_mcp.downloads as downloads
    original = downloads.os.replace

    def fail_manifest(source, destination):
        if str(destination).endswith('.7z.manifest.json'):
            raise PermissionError('injected manifest failure')
        return original(source, destination)

    monkeypatch.setattr(downloads.os, 'replace', fail_manifest)
    downloader = OfficialFileDownloader(tmp_path, interval=0, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=SEVEN_ZIP_MAGIC+b'fixture',
                                       headers={'Content-Type': 'application/octet-stream'})))
    try:
        with pytest.raises(OfficialDownloadError):
            await downloader.download('human', 'core', 'tf_target')
        assert not (tmp_path/'human_core_TF_Target.7z').exists()
        assert not (tmp_path/'human_core_TF_Target.7z.manifest.json').exists()
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_independent_downloaders_cannot_overwrite_without_permission(tmp_path):
    class SlowArchive(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield SEVEN_ZIP_MAGIC+b'fixture'

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=SlowArchive(), headers={'Content-Type': 'application/octet-stream'})

    clients = [OfficialFileDownloader(tmp_path, interval=0, transport=httpx.MockTransport(handler)) for _ in range(2)]
    try:
        results = await asyncio.gather(*(client.download('human', 'core', 'tf_target') for client in clients), return_exceptions=True)
        failures = [value for value in results if isinstance(value, OfficialDownloadError)]
        assert len(failures) == 1 and failures[0].code == 'FILE_ALREADY_EXISTS'
        assert len(calls) == 1
        path = tmp_path/'human_core_TF_Target.7z'
        manifest = json.loads(path.with_name(path.name+'.manifest.json').read_text())
        assert manifest['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        for client in clients:
            await client.close()


def test_failed_overwrite_restores_the_old_pair(tmp_path, monkeypatch):
    import regnetwork_mcp.fileio as fileio
    data, manifest = tmp_path/'archive.7z', tmp_path/'archive.manifest.json'
    data.write_bytes(b'old data')
    manifest.write_bytes(b'old manifest')
    data_stage = stage_bytes(data, b'new data')
    manifest_stage = stage_bytes(manifest, b'new manifest')
    original = os.replace

    def fail_second_stage(source, destination):
        if Path(source) == manifest_stage:
            raise PermissionError('injected second stage failure')
        return original(source, destination)

    monkeypatch.setattr(fileio.os, 'replace', fail_second_stage)
    with file_lock(data), pytest.raises(PermissionError):
        publish_pair(data_stage, manifest_stage, data, manifest)
    assert data.read_bytes() == b'old data'
    assert manifest.read_bytes() == b'old manifest'
    assert not list(tmp_path.glob('*.partial'))
    assert not list(tmp_path.glob('*.rollback'))


@pytest.mark.parametrize('existing', [False, True])
def test_process_crash_between_renames_is_recoverable(tmp_path, existing):
    code = '''
import os, sys
from pathlib import Path
from regnetwork_mcp.fileio import file_lock, stage_bytes, publish_pair
root = Path(sys.argv[1])
data, manifest = root/'file.7z', root/'file.manifest.json'
ds, ms = stage_bytes(data, b'new data'), stage_bytes(manifest, b'new manifest')
original = os.replace
def crash(source, destination):
    if Path(source) == ms:
        os._exit(73)
    return original(source, destination)
os.replace = crash
with file_lock(data):
    publish_pair(ds, ms, data, manifest)
'''
    data, manifest = tmp_path/'file.7z', tmp_path/'file.manifest.json'
    if existing:
        data.write_bytes(b'old data')
        manifest.write_bytes(b'old manifest')
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], timeout=15, capture_output=True)
    assert result.returncode == 73, result.stderr.decode(errors='replace')
    with file_lock(data):
        recover_pair(data, manifest)
    assert data.exists() is existing and manifest.exists() is existing
    if existing:
        assert data.read_bytes() == b'old data' and manifest.read_bytes() == b'old manifest'
    assert not list(tmp_path.glob('*.transaction.json'))
    assert not list(tmp_path.glob('*.partial'))


def test_file_lock_excludes_another_process_and_releases(tmp_path):
    target = tmp_path/'file'
    code = '''
import sys
from pathlib import Path
from regnetwork_mcp.fileio import file_lock
try:
    with file_lock(Path(sys.argv[1]), timeout=0.1):
        pass
except TimeoutError:
    sys.exit(9)
'''
    with file_lock(target):
        blocked = subprocess.run([sys.executable, '-c', code, str(target)], timeout=15, capture_output=True)
        assert blocked.returncode == 9, blocked.stderr.decode(errors='replace')
    released = subprocess.run([sys.executable, '-c', code, str(target)], timeout=15, capture_output=True)
    assert released.returncode == 0, released.stderr.decode(errors='replace')


@pytest.mark.asyncio
async def test_different_official_files_can_stream_concurrently(tmp_path):
    entered = 0
    both = asyncio.Event()

    class Archive(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal entered
            entered += 1
            if entered == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 3)
            yield SEVEN_ZIP_MAGIC+b'fixture'

    client = OfficialFileDownloader(tmp_path, interval=0, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Archive(), headers={'Content-Type':'application/octet-stream'})))
    try:
        results = await asyncio.gather(*(client.download('human','core',category) for category in ['tf_target','mirna_target']))
        assert len(results) == 2 and entered == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_download_cleans_stages_and_releases_lock(tmp_path):
    entered = asyncio.Event()

    class Archive(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b''

    client = OfficialFileDownloader(tmp_path, interval=0, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Archive(), headers={'Content-Type':'application/octet-stream'})))
    try:
        task = asyncio.create_task(client.download('human','core','tf_target'))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(tmp_path.glob('*.partial'))
        assert not list(tmp_path.glob('*.7z'))
        with file_lock(tmp_path/'human_core_TF_Target.7z', timeout=0.1):
            pass
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_length_large_response_stops_before_reading_rest(tmp_path):
    reads = []
    closed = []

    class HugeResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(3):
                reads.append(i)
                yield b'x'*(64*1024)

        async def aclose(self):
            closed.append(True)

    cache = ResultCache(tmp_path/'cache.sqlite')
    async with OnlineAdapter(interval=0, cache=cache, max_response_bytes=64*1024,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=HugeResponse()))) as adapter:
        with pytest.raises(UpstreamError) as error:
            await adapter.mapping('human','core',['TP53'])
        assert error.value.code == 'UPSTREAM_SIZE_LIMIT'
    assert reads == [0,1] and closed
    assert cache.statistics()['entries'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('empty', [False, True])
async def test_compressed_response_is_decoded_once_and_bounded(empty):
    body = json.dumps({'result':[] if empty else [row()], 'totalResults':0 if empty else 1}).encode()
    payload = gzip.compress(body)

    class Compressed(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Compressed(),
        headers={'Content-Encoding':'gzip','Content-Type':'application/json','Content-Length':str(len(payload))}))
    async with OnlineAdapter(interval=0, transport=transport, max_response_bytes=len(body)) as adapter:
        scan = await adapter.scan('human','core','TP53')
        assert scan.complete and (not scan.records if empty else scan.records[0].regulator.symbol == 'TP53')
    async with OnlineAdapter(interval=0, transport=transport, max_response_bytes=len(body)-1) as adapter:
        with pytest.raises(UpstreamError) as error:
            await adapter.scan('human','core','TP53')
        assert error.value.code == 'UPSTREAM_SIZE_LIMIT'


@pytest.mark.asyncio
async def test_mapping_record_budget_is_explicit_and_does_not_cache_partial(tmp_path):
    cache = ResultCache(tmp_path/'cache.sqlite')
    async with OnlineAdapter(interval=0, cache=cache, max_mapping_records=1,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=row()+'\n'+row(1)))) as adapter:
        with pytest.raises(UpstreamFormatError) as error:
            await adapter.mapping('human','core',['TP53'])
        assert error.value.code == 'UPSTREAM_RECORD_LIMIT'
    assert cache.statistics()['entries'] == 0


def test_cache_batch_rolls_back_and_retention_is_checked_without_cleanup(tmp_path):
    now = [0]
    cache = ResultCache(tmp_path/'cache.sqlite', ttl=5, retention=10, clock=lambda:now[0])
    cache.put('kept', {'v':1})
    with pytest.raises(RuntimeError), cache.batch():
        cache.put('not_committed', {'v':2})
        raise RuntimeError('injected interruption')
    assert cache.get('not_committed') is None
    now[0] = 11
    assert cache.get('kept', allow_expired=True) is None


@pytest.mark.asyncio
async def test_slow_cache_does_not_block_event_loop_and_error_is_structured(tmp_path, monkeypatch):
    service = ResearchService(tmp_path, adapter=OnlineAdapter(interval=0, transport=httpx.MockTransport(lambda request:httpx.Response(500))))
    network = extract_induced([parse_interaction(row(), 'human')], ['TP53','GENE0'])
    network['provenance'] = {}
    identifier = 'subnetwork_'+'0'*64
    service.cache.put(ResultCache.key('artifact', identifier), network)
    entered, release = threading.Event(), threading.Event()
    original = service.cache.get

    def slow_get(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError('event loop did not release worker')
        return original(*args, **kwargs)

    monkeypatch.setattr(service.cache, 'get', slow_get)
    task = asyncio.create_task(service.summarize(identifier))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        await task

    def broken_get(*args, **kwargs):
        raise CacheError('injected cache fault')

    monkeypatch.setattr(service.cache, 'get', broken_get)
    result = await service.guard('summarize_network', {}, lambda:service.summarize(identifier))
    assert result.status == 'error' and result.error['code'] == 'LOCAL_CACHE_ERROR'
    await service.close()


def test_internal_record_reuse_does_not_change_public_output():
    records = [parse_interaction(row(), 'human')]*2
    scan = ScanResult(records=records, upstream_total_candidates=2, complete=True)
    retained = []
    first = select_interactions(scan, symbol='TP53')
    second = select_interactions(scan, symbol='TP53', selected_records=retained)
    assert first == second and all(value is records[0] for value in retained) and len(retained) == 2
    retained.clear()
    first = extract_induced(records, ['TP53','GENE0'])
    second = extract_induced(records, ['TP53','GENE0'], selected_records=retained)
    assert first == second and len(retained) == 2


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_lock_until_worker_stops(tmp_path):
    entered, release = threading.Event(), threading.Event()
    target = tmp_path/'artifact'

    def worker():
        entered.set()
        assert release.wait(5)

    async def work():
        async with async_file_lock(target):
            await run_sync(worker)

    task = asyncio.create_task(work())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError), file_lock(target, timeout=0):
            pass
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with file_lock(target, timeout=0):
        pass


def test_corrupt_sqlite_is_an_explicit_cache_error(tmp_path):
    path = tmp_path/'corrupt.sqlite'
    path.write_bytes(b'not a sqlite database')
    with pytest.raises(CacheError):
        ResultCache(path)


@pytest.mark.asyncio
async def test_download_checks_disk_space_without_content_length(tmp_path, monkeypatch):
    import regnetwork_mcp.downloads as downloads
    from types import SimpleNamespace

    class Archive(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield SEVEN_ZIP_MAGIC+b'fixture'

    monkeypatch.setattr(downloads.shutil, 'disk_usage', lambda path:SimpleNamespace(free=0))
    client = OfficialFileDownloader(tmp_path, interval=0, transport=httpx.MockTransport(
        lambda request:httpx.Response(200, stream=Archive(), headers={'Content-Type':'application/octet-stream'})))
    try:
        with pytest.raises(OfficialDownloadError) as error:
            await client.download('human','core','tf_target')
        assert error.value.code == 'INSUFFICIENT_DISK_SPACE'
        assert not list(tmp_path.glob('*.partial'))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cached_response_also_respects_current_byte_budget(tmp_path):
    cache = ResultCache(tmp_path/'cache.sqlite')
    async with OnlineAdapter(interval=0, cache=cache, transport=httpx.MockTransport(
            lambda request:httpx.Response(200, json={'result':[row()], 'totalResults':1}))) as adapter:
        await adapter.scan('human','core','TP53')
    async with OnlineAdapter(interval=0, cache=cache, max_response_bytes=1, transport=httpx.MockTransport(
            lambda request:pytest.fail('must use the cached response'))) as adapter:
        with pytest.raises(UpstreamError) as error:
            await adapter.scan('human','core','TP53')
        assert error.value.code == 'UPSTREAM_SIZE_LIMIT'


def test_export_manifest_failure_rolls_back_without_partial_file(tmp_path, monkeypatch):
    import regnetwork_mcp.fileio as fileio
    from regnetwork_mcp.exports import export_network
    original = os.replace

    def fail_manifest(source, destination):
        if str(destination).endswith('.manifest.json'):
            raise PermissionError('injected export manifest failure')
        return original(source, destination)

    monkeypatch.setattr(fileio.os, 'replace', fail_manifest)
    network = extract_induced([parse_interaction(row(), 'human')], ['TP53','GENE0'])
    with pytest.raises(PermissionError):
        export_network(network,tmp_path,'json')
    assert not list(tmp_path.glob('regnetwork-*'))
    assert not list(tmp_path.glob('*.partial'))
