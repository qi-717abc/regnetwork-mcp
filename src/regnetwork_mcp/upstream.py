"""Bounded read-only adapter for the three observed RegNetwork routes."""
import asyncio
from dataclasses import asdict, dataclass, field, replace
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import time
from typing import Any, Callable

import httpx

from .records import Interaction, UpstreamFormatError, parse_mapping_list, parse_query_page
from .cache import ResultCache

BASE_URL = "https://www.zpliulab.cn"
ROUTES = {"/RegNetwork/query", "/RegNetwork/get_statistics", "/RegNetwork/run_grn_mapping"}
RETRYABLE = {429, 502, 503, 504}


class UpstreamError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code, self.status_code = code, status_code


@dataclass(frozen=True)
class ReadObservation:
    route: str
    parameters: dict[str, Any]
    retrieved_at: str
    sha256: str
    status_code: int
    data_origin: str = "online"


@dataclass
class ScanResult:
    records: list[Interaction] = field(default_factory=list)
    upstream_total_candidates: int | None = None
    complete: bool = False
    scanned_pages: int = 0
    observations: list[ReadObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class OnlineAdapter:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 interval: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 cache: ResultCache | None = None):
        self.client = httpx.AsyncClient(base_url=BASE_URL, transport=transport,
                                       timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
        self.interval, self.clock = interval, clock
        self.cache = cache
        self._last_request = float("-inf")
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    @staticmethod
    def _scope(species: str, dataset: str) -> dict[str, str]:
        if species not in {"human", "mouse"} or dataset not in {"core", "complete"}:
            raise ValueError("invalid species or dataset")
        return {"species": species, "dbType": "core" if dataset == "core" else "all"}

    async def _read(self, route: str, parameters: dict[str, Any] | None,
                    deadline: float, *, refresh: bool = False) -> tuple[httpx.Response, ReadObservation]:
        if route not in ROUTES:
            raise ValueError("route is not an approved read-only endpoint")
        if self.cache is not None and not refresh:
            key = ResultCache.key("online-v1", {"route": route, "parameters": parameters})
            hit = self.cache.get(key)
            if hit is not None:
                try:
                    content = base64.b64decode(hit.payload["body"], validate=True)
                    observation = ReadObservation(**hit.payload["observation"])
                    if hashlib.sha256(content).hexdigest() != observation.sha256:
                        raise ValueError("response checksum does not match cached observation")
                    response = httpx.Response(200, content=content, headers={"Content-Type": hit.payload["content_type"]})
                except (KeyError, TypeError, ValueError) as exc:
                    raise UpstreamError("CACHE_INTEGRITY_ERROR", "cached response could not be verified") from exc
                return response, replace(observation, data_origin="cache")
        for attempt in range(3):
            async with self._lock:
                remaining = deadline - self.clock()
                wait = max(0.0, self.interval - (self.clock() - self._last_request))
                if remaining <= wait:
                    raise UpstreamError("OPERATION_TIMEOUT", "operation deadline reached")
                await asyncio.sleep(wait)
                self._last_request = self.clock()
                try:
                    async with asyncio.timeout(max(0.001, deadline - self.clock())):
                        if parameters is None:
                            response = await self.client.get(route)
                        else:
                            response = await self.client.post(route, json=parameters)
                except (TimeoutError, httpx.TimeoutException) as exc:
                    raise UpstreamError("UPSTREAM_TIMEOUT", "upstream read timed out") from exc
                except httpx.RequestError as exc:
                    raise UpstreamError("UPSTREAM_UNAVAILABLE", "upstream could not be reached") from exc
            if response.status_code in RETRYABLE and attempt < 2:
                retry_after = response.headers.get("Retry-After", "")
                wait = float(2 ** attempt)
                if retry_after.isdecimal():
                    wait = float(retry_after)
                elif retry_after:
                    try:
                        date = parsedate_to_datetime(retry_after)
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=timezone.utc)
                        wait = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
                if self.clock() + wait >= deadline:
                    raise UpstreamError("OPERATION_TIMEOUT", "retry would exceed operation deadline", response.status_code)
                await asyncio.sleep(wait)
                continue
            if response.status_code != 200:
                raise UpstreamError("UPSTREAM_HTTP_ERROR", f"upstream returned HTTP {response.status_code}", response.status_code)
            return response, ReadObservation(route, dict(parameters or {}), datetime.now(timezone.utc).isoformat(),
                                            hashlib.sha256(response.content).hexdigest(), response.status_code)
        raise AssertionError("retry loop exhausted")

    def _remember(self, response: httpx.Response, observation: ReadObservation):
        if self.cache is None or observation.data_origin != "online":
            return
        key = ResultCache.key("online-v1", {"route": observation.route, "parameters": observation.parameters or None})
        self.cache.put(key, {"body": base64.b64encode(response.content).decode("ascii"),
                             "content_type": response.headers.get("Content-Type", ""),
                             "observation": asdict(observation)}, namespace="online")

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise UpstreamFormatError("upstream response is not JSON") from exc

    async def scan(self, species: str, dataset: str, symbol: str, *, regulator_type: str = "all",
                   target_type: str = "all", confidence: str = "all", max_pages: int = 20,
                   budget_seconds: float = 60.0, refresh: bool = False) -> ScanResult:
        scope = self._scope(species, dataset)
        if not symbol.strip():
            raise ValueError("a symbol is required; unbounded whole-database query is disabled")
        if not 1 <= max_pages <= 20 or not 0 < budget_seconds <= 60:
            raise ValueError("invalid page or time budget")
        if regulator_type not in {"all", "TF", "miRNA", "lncRNA", "circRNA"}:
            raise ValueError("invalid regulator type")
        if target_type not in {"all", "TF", "miRNA", "lncRNA", "circRNA", "Gene"}:
            raise ValueError("invalid target type")
        if confidence not in {"all", "high", "medium", "low"}:
            raise ValueError("invalid confidence")
        result = ScanResult()
        deadline = self.clock() + budget_seconds
        for page_index in range(1, max_pages + 1):
            parameters = scope | {"type": regulator_type, "targetType": target_type, "confidence": confidence,
                                  "searchSymbol": symbol.strip(), "page": page_index, "rowsPerPage": 150}
            try:
                response, observation = await self._read("/RegNetwork/query", parameters, deadline, refresh=refresh)
                page = parse_query_page(self._json(response), species)
                self._remember(response, observation)
            except (UpstreamError, UpstreamFormatError) as exc:
                if not result.observations:
                    raise
                result.warnings.append(getattr(exc, "code", "UPSTREAM_FORMAT_CHANGED") + ":" + str(exc))
                return result
            result.observations.append(observation)
            result.scanned_pages += 1
            if result.upstream_total_candidates is not None and page.upstream_total_candidates != result.upstream_total_candidates:
                result.warnings.append("UPSTREAM_COUNT_CHANGED:pages are not an atomic snapshot")
                return result
            result.upstream_total_candidates = page.upstream_total_candidates
            result.records.extend(page.records)
            if len(result.records) >= page.upstream_total_candidates:
                result.complete = len(result.records) == page.upstream_total_candidates
                if not result.complete:
                    result.warnings.append("UPSTREAM_PAGE_OVERLAP:received more rows than reported")
                return result
            if not page.records:
                result.warnings.append("UPSTREAM_EARLY_EMPTY_PAGE:reported candidates remain")
                return result
        result.warnings.append("PAGE_LIMIT_REACHED")
        return result

    async def statistics(self, *, refresh: bool = False) -> tuple[dict[str, Any], ReadObservation]:
        response, observation = await self._read("/RegNetwork/get_statistics", None, self.clock() + 60, refresh=refresh)
        data = self._json(response)
        if not isinstance(data, dict) or any(not isinstance(data.get(s), dict) for s in ["human", "mouse"]):
            raise UpstreamFormatError("expected human and mouse statistics objects")
        for species in ["human", "mouse"]:
            values = data[species]
            categories = values.get("categories")
            if not isinstance(categories, list) or not all(isinstance(v, str) for v in categories):
                raise UpstreamFormatError("invalid statistics categories")
            for name in ["RegNetwork_v1", "RegNetwork_core_v2", "RegNetwork_v2"]:
                counts = values.get(name)
                if not isinstance(counts, list) or len(counts) != len(categories) or any(type(v) is not int or v < 0 for v in counts):
                    raise UpstreamFormatError("invalid statistics series")
        self._remember(response, observation)
        return data, observation

    async def mapping(self, species: str, dataset: str, symbols: list[str], *, refresh: bool = False) -> tuple[tuple[Interaction, ...], ReadObservation]:
        scope = self._scope(species, dataset)
        if not 1 <= len(symbols) <= 50 or any(not s.strip() for s in symbols):
            raise ValueError("mapping requires 1 to 50 nonempty symbols")
        parameters = scope | {"genes": list(dict.fromkeys(s.strip() for s in symbols)), "format": "list"}
        response, observation = await self._read("/RegNetwork/run_grn_mapping", parameters, self.clock() + 60, refresh=refresh)
        if "json" in response.headers.get("Content-Type", "").lower():
            raise UpstreamFormatError("mapping returned JSON rather than an edge list")
        records = parse_mapping_list(response.text, species)
        self._remember(response, observation)
        return records, observation
