"""Download only files explicitly listed on RegNetwork's official page."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Callable

import httpx

from . import __version__
from .fileio import async_file_lock, publish_pair, recover_pair, run_sync, stage_bytes

BASE_URL = "https://www.zpliulab.cn/RegNetwork"
OFFICIAL_DOWNLOAD_PAGE = f"{BASE_URL}/download"
MAX_DOWNLOAD_BYTES = 8 * 1024**3
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf'\x1c"

FILE_LABELS = {
    "tf_target": "TF-Target",
    "mirna_target": "miRNA-Target",
    "lncrna_target": "lncRNA-Target",
    "circrna_target": "circRNA-Target",
    "regulator_tf": "Regulator-TF",
    "regulator_mirna": "Regulator-miRNA",
    "regulator_lncrna": "Regulator-lncRNA",
    "regulator_circrna": "Regulator-circRNA",
    "regulator_gene": "Regulator-Gene",
    # The official page's final row is the archive for the selected group.
    "regulator_target": "Regulator-Target (group archive)",
}
FILE_STEMS = {
    "tf_target": "TF_Target",
    "mirna_target": "miRNA_Target",
    "lncrna_target": "lncRNA_Target",
    "circrna_target": "circRNA_Target",
    "regulator_tf": "Regulator_TF",
    "regulator_mirna": "Regulator_miRNA",
    "regulator_lncrna": "Regulator_lncRNA",
    "regulator_circrna": "Regulator_circRNA",
    "regulator_gene": "Regulator_Gene",
}


class OfficialDownloadError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class DownloadReceipt:
    species: str
    dataset: str
    file_category: str
    official_label: str
    filename: str
    source_url: str
    retrieved_at: str
    content_type: str
    size_bytes: int
    sha256: str
    file_path: str
    manifest_path: str
    upstream_checksum_provided: bool = False
    archive_extracted: bool = False


def official_file_url(species: str, dataset: str, file_category: str) -> tuple[str, str]:
    """Resolve one of the 40 official page entries; never accepts a URL or path."""
    if species not in {"human", "mouse"} or dataset not in {"core", "complete"}:
        raise ValueError("species must be human/mouse and dataset must be core/complete")
    if file_category not in FILE_LABELS:
        raise ValueError("file_category is not listed on the official RegNetwork download page")
    if file_category == "regulator_target":
        filename = f"{species}.core.7z" if dataset == "core" else f"{species}.7z"
    elif dataset == "core":
        filename = f"{species}_core_{FILE_STEMS[file_category]}.7z"
    else:
        filename = f"{species}_{FILE_STEMS[file_category]}.7z"
    return f"{BASE_URL}/static/data/{filename}", filename


class OfficialFileDownloader:
    def __init__(self, download_dir: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_bytes: int = MAX_DOWNLOAD_BYTES, interval: float = 1.0,
                 clock: Callable[[], float] = time.monotonic):
        if max_bytes <= 0 or max_bytes > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {MAX_DOWNLOAD_BYTES}")
        if interval < 0:
            raise ValueError("interval must be nonnegative")
        self.download_dir = Path(download_dir)
        self.max_bytes = max_bytes
        self.interval = interval
        self.clock = clock
        self._last_request = float("-inf")
        self.client = httpx.AsyncClient(transport=transport,
            timeout=httpx.Timeout(60, connect=10), follow_redirects=False,
            headers={"User-Agent": f"regnetwork-mcp/{__version__}",
                     "Accept": "application/x-7z-compressed, application/octet-stream",
                     "Accept-Encoding": "identity"})
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)

    async def close(self) -> None:
        await self.client.aclose()

    async def download(self, species: str, dataset: str, file_category: str, *, overwrite: bool = False) -> DownloadReceipt:
        try:
            source_url, filename = official_file_url(species, dataset, file_category)
        except ValueError as exc:
            raise OfficialDownloadError("INVALID_ARGUMENT", str(exc)) from exc

        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            destination = self.download_dir / filename
            manifest_path = self.download_dir / f"{filename}.manifest.json"
            async with self._slots, async_file_lock(destination):
                await run_sync(recover_pair, destination, manifest_path)
                if not overwrite and (destination.exists() or destination.is_symlink()):
                    raise OfficialDownloadError("FILE_ALREADY_EXISTS", "file already exists; set overwrite=true to replace it")

                data_fd, data_tmp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".partial", dir=self.download_dir)
                os.close(data_fd)
                data_tmp = Path(data_tmp_name)
                manifest_tmp: Path | None = None
                try:
                    async with self._lock:
                        wait = max(0.0, self.interval - (self.clock() - self._last_request))
                        if wait:
                            await asyncio.sleep(wait)
                        self._last_request = self.clock()
                    async with self.client.stream("GET", source_url) as response:
                        if response.status_code != 200:
                            raise OfficialDownloadError("UPSTREAM_HTTP_ERROR", f"official download returned HTTP {response.status_code}", response.status_code)
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if content_type not in {"application/x-7z-compressed", "application/octet-stream"}:
                            raise OfficialDownloadError("UPSTREAM_CONTENT_TYPE", f"unexpected official file content type: {content_type or 'missing'}")
                        length_header = response.headers.get("Content-Length")
                        expected_size = None
                        if length_header is not None:
                            try:
                                expected_size = int(length_header)
                            except ValueError as exc:
                                raise OfficialDownloadError("UPSTREAM_CONTENT_LENGTH", "invalid Content-Length from official file") from exc
                            if expected_size < 0 or expected_size > self.max_bytes:
                                raise OfficialDownloadError("DOWNLOAD_SIZE_LIMIT", f"official file exceeds the {self.max_bytes}-byte download limit")
                            if expected_size > shutil.disk_usage(self.download_dir).free:
                                raise OfficialDownloadError("INSUFFICIENT_DISK_SPACE", "not enough free space for the official file")

                        digest = hashlib.sha256()
                        size = 0
                        prefix = bytearray()
                        next_space_check = 0
                        with data_tmp.open("wb") as handle:
                            async for chunk in response.aiter_bytes(256 * 1024):
                                if not chunk:
                                    continue
                                size += len(chunk)
                                if size > self.max_bytes:
                                    raise OfficialDownloadError("DOWNLOAD_SIZE_LIMIT", f"official file exceeds the {self.max_bytes}-byte download limit")
                                if size >= next_space_check:
                                    free = (await run_sync(shutil.disk_usage, self.download_dir)).free
                                    if free < len(chunk):
                                        raise OfficialDownloadError("INSUFFICIENT_DISK_SPACE", "not enough free space for the official file")
                                    next_space_check = size + 16 * 1024**2
                                if len(prefix) < len(SEVEN_ZIP_MAGIC):
                                    prefix.extend(chunk[:len(SEVEN_ZIP_MAGIC) - len(prefix)])
                                digest.update(chunk)
                                await run_sync(handle.write, chunk)
                            await run_sync(handle.flush)
                            await run_sync(os.fsync, handle.fileno())
                        if expected_size is not None and size != expected_size:
                            raise OfficialDownloadError("INCOMPLETE_DOWNLOAD", f"expected {expected_size} bytes but received {size}")
                        if bytes(prefix) != SEVEN_ZIP_MAGIC:
                            raise OfficialDownloadError("INVALID_ARCHIVE", "download did not begin with the 7z archive signature")

                    retrieved_at = datetime.now(timezone.utc).isoformat()
                    sha256 = digest.hexdigest()
                    manifest = {
                        "schema_version": 1,
                        "provider": "RegNetwork",
                        "official_download_page": OFFICIAL_DOWNLOAD_PAGE,
                        "source_url": source_url,
                        "request": {"species": species, "dataset": dataset, "file_category": file_category,
                                    "official_label": FILE_LABELS[file_category], "filename": filename},
                        "retrieved_at": retrieved_at,
                        "http_status": 200,
                        "content_type": content_type,
                        "content_length_header": expected_size,
                        "size_bytes": size,
                        "sha256": sha256,
                        "upstream_checksum_provided": False,
                        "sha256_note": "computed locally; no upstream checksum was provided",
                        "archive_extracted": False,
                        "mcp_version": __version__,
                    }
                    manifest_tmp = await run_sync(stage_bytes, manifest_path,
                        (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
                    await run_sync(publish_pair, data_tmp, manifest_tmp, destination, manifest_path)
                    manifest_tmp = None
                    return DownloadReceipt(species, dataset, file_category, FILE_LABELS[file_category], filename,
                        source_url, retrieved_at, content_type, size, sha256, str(destination), str(manifest_path))
                finally:
                    data_tmp.unlink(missing_ok=True)
                    if manifest_tmp is not None:
                        manifest_tmp.unlink(missing_ok=True)
        except OfficialDownloadError:
            raise
        except httpx.TimeoutException as exc:
            raise OfficialDownloadError("UPSTREAM_TIMEOUT", "official file download timed out") from exc
        except httpx.RequestError as exc:
            raise OfficialDownloadError("UPSTREAM_UNAVAILABLE", "official file server could not be reached") from exc
        except OSError as exc:
            raise OfficialDownloadError("LOCAL_IO_ERROR", f"could not save the official file: {exc}") from exc
