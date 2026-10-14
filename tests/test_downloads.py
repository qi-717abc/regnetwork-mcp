import hashlib
import json

import httpx
import pytest

from regnetwork_mcp.downloads import (
    FILE_LABELS,
    SEVEN_ZIP_MAGIC,
    OfficialDownloadError,
    OfficialFileDownloader,
    official_file_url,
)


@pytest.mark.parametrize("species", ["human", "mouse"])
@pytest.mark.parametrize("dataset", ["core", "complete"])
def test_catalog_only_resolves_the_forty_official_file_entries(species, dataset):
    urls = [official_file_url(species, dataset, category)[0] for category in FILE_LABELS]
    assert len(urls) == 10
    assert len(set(urls)) == 10
    assert all(url.startswith("https://www.zpliulab.cn/RegNetwork/static/data/") and url.endswith(".7z") for url in urls)


@pytest.mark.parametrize("species,dataset,category,filename", [
    ("human", "core", "tf_target", "human_core_TF_Target.7z"),
    ("human", "complete", "mirna_target", "human_miRNA_Target.7z"),
    ("mouse", "core", "regulator_gene", "mouse_core_Regulator_Gene.7z"),
    ("mouse", "complete", "regulator_target", "mouse.7z"),
])
def test_catalog_file_names_match_official_page_patterns(species, dataset, category, filename):
    _, actual = official_file_url(species, dataset, category)
    assert actual == filename


def test_catalog_rejects_unlisted_scopes_and_path_or_url_injection():
    for args in [("rat", "core", "tf_target"), ("human", "latest", "tf_target"),
                 ("human", "core", "https://attacker.invalid/file.7z"),
                 ("human", "core", "../secret")]:
        with pytest.raises(ValueError):
            official_file_url(*args)


@pytest.mark.asyncio
async def test_download_saves_archive_and_source_manifest_with_local_hash(tmp_path):
    archive = SEVEN_ZIP_MAGIC + b"fixture archive bytes"

    def transport(request):
        assert request.method == "GET"
        assert request.url.host == "www.zpliulab.cn"
        assert request.url.path == "/RegNetwork/static/data/human_core_TF_Target.7z"
        return httpx.Response(200, content=archive, headers={"Content-Type": "application/x-7z-compressed"})

    downloader = OfficialFileDownloader(tmp_path / "downloads", transport=httpx.MockTransport(transport), interval=0)
    try:
        receipt = await downloader.download("human", "core", "tf_target")
        saved = tmp_path / "downloads" / "human_core_TF_Target.7z"
        manifest_path = tmp_path / "downloads" / "human_core_TF_Target.7z.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert saved.read_bytes() == archive
        assert receipt.file_path == str(saved)
        assert receipt.manifest_path == str(manifest_path)
        assert receipt.size_bytes == len(archive)
        assert receipt.sha256 == hashlib.sha256(archive).hexdigest()
        assert manifest["source_url"] == receipt.source_url
        assert manifest["official_download_page"] == "https://www.zpliulab.cn/RegNetwork/download"
        assert manifest["sha256"] == receipt.sha256
        assert manifest["upstream_checksum_provided"] is False
        assert manifest["archive_extracted"] is False
        assert not list((tmp_path / "downloads").glob("*.partial"))
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_download_requires_explicit_overwrite_for_existing_file(tmp_path):
    archive = SEVEN_ZIP_MAGIC + b"fixture"
    payload = [archive]
    downloader = OfficialFileDownloader(tmp_path, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload[0], headers={"Content-Type": "application/octet-stream"})), interval=0)
    try:
        await downloader.download("human", "core", "tf_target")
        with pytest.raises(OfficialDownloadError, match="overwrite=true") as error:
            await downloader.download("human", "core", "tf_target")
        assert error.value.code == "FILE_ALREADY_EXISTS"
        updated = SEVEN_ZIP_MAGIC + b"new fixture"
        payload[0] = updated
        receipt = await downloader.download("human", "core", "tf_target", overwrite=True)
        assert (tmp_path / receipt.filename).read_bytes() == updated
        assert receipt.sha256 == hashlib.sha256(updated).hexdigest()
    finally:
        await downloader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://attacker.invalid/file.7z"}),
    httpx.Response(404, text="missing"),
    httpx.Response(200, text="not an archive", headers={"Content-Type": "text/html"}),
    httpx.Response(200, content=b"not 7z", headers={"Content-Type": "application/octet-stream"}),
])
async def test_download_rejects_redirects_http_errors_and_invalid_archive(tmp_path, response):
    downloader = OfficialFileDownloader(tmp_path, transport=httpx.MockTransport(lambda request: response), interval=0)
    try:
        with pytest.raises(OfficialDownloadError):
            await downloader.download("human", "core", "tf_target")
        assert not list(tmp_path.glob("*.7z"))
        assert not list(tmp_path.glob("*.partial"))
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_download_enforces_byte_limit_and_removes_partial_file(tmp_path):
    archive = SEVEN_ZIP_MAGIC + b"123456789"
    downloader = OfficialFileDownloader(tmp_path, max_bytes=8, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=archive, headers={"Content-Type": "application/octet-stream"})), interval=0)
    try:
        with pytest.raises(OfficialDownloadError) as error:
            await downloader.download("human", "core", "tf_target")
        assert error.value.code == "DOWNLOAD_SIZE_LIMIT"
        assert not list(tmp_path.glob("*.partial"))
        assert not list(tmp_path.glob("*.7z"))
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_download_detects_truncated_content_length(tmp_path):
    downloader = OfficialFileDownloader(tmp_path, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=SEVEN_ZIP_MAGIC + b"short",
                                       headers={"Content-Type": "application/octet-stream", "Content-Length": "100"})), interval=0)
    try:
        with pytest.raises(OfficialDownloadError) as error:
            await downloader.download("human", "core", "tf_target")
        assert error.value.code == "INCOMPLETE_DOWNLOAD"
        assert not list(tmp_path.glob("*.partial"))
    finally:
        await downloader.close()
