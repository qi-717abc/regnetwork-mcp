"""Parse observed online records without inventing biological annotations."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Any


class UpstreamFormatError(ValueError):
    """A successful HTTP response no longer matches the observed data format."""


MISSING = {"", "NA", "N/A", "nan", "None"}
SPECIES = {"human", "mouse"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Entity:
    entity_id: str
    species: str
    symbol: str
    raw_identifier: str
    namespace: str | None
    accession: str | None
    source_type: str
    identity_state: str


@dataclass(frozen=True)
class Reference:
    raw: str
    pmid: str | None
    method: str | None
    parse_state: str
    occurrences: int


@dataclass(frozen=True)
class Interaction:
    interaction_id: str
    regulator: Entity
    target: Entity
    membership_raw: str
    confidence_raw: str | None
    source_count_inferred: int | None
    source_databases_raw: str
    score: float | None
    references: tuple[Reference, ...]
    references_raw: str
    raw_record: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_entity(species: str, symbol: str, identifier: str, source_type: str) -> Entity:
    if species not in SPECIES:
        raise ValueError("species must be human or mouse")
    symbol, identifier, source_type = symbol.strip(), identifier.strip(), source_type.strip()
    namespace = accession = None
    if identifier in MISSING:
        state = "missing_id"
    elif ";" in identifier or "/" in symbol:
        state = "grouped_source"
    elif ":" in identifier:
        namespace, accession = identifier.split(":", 1)
        if not namespace or accession in MISSING:
            namespace = accession = None
            state = "namespace_unresolved"
        else:
            state = "declared_namespaced"
    else:
        state = "namespace_unresolved"
    # Source-declared identity, not an independent authority verification.
    key = [species, namespace, accession] if state == "declared_namespaced" else [species, symbol, identifier, source_type]
    return Entity("entity_" + _digest(key), species, symbol, identifier, namespace, accession, source_type, state)


def parse_references(raw: str) -> tuple[Reference, ...]:
    if raw.strip() in MISSING:
        return ()
    entries: dict[str, Reference] = {}
    for token in raw.split(";"):
        token = token.strip()
        if not token:
            continue
        previous = entries.get(token)
        if previous:
            entries[token] = Reference(token, previous.pmid, previous.method, previous.parse_state, previous.occurrences + 1)
            continue
        match = re.fullmatch(r"([1-9]\d*)(?:\(([^()]*)\))?", token)
        entries[token] = Reference(token, match[1] if match else None, match[2] if match else None,
                                   "parsed" if match else "unparsed", 1)
    return tuple(entries.values())


def parse_interaction(raw: str, species: str) -> Interaction:
    if not isinstance(raw, str):
        raise UpstreamFormatError("interaction is not a tab-separated string")
    columns = raw.rstrip("\r\n").split("\t")
    if len(columns) != 12:
        raise UpstreamFormatError(f"expected 12 online fields, received {len(columns)}")
    rs, ri, ts, ti, rt, tt, membership, confidence, count, databases, score_text, references = columns
    regulator, target = parse_entity(species, rs, ri, rt), parse_entity(species, ts, ti, tt)
    warnings: list[str] = []
    try:
        score = None if score_text.strip() in MISSING else float(score_text)
        if score is not None and (not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("invalid score range")
        source_count = None if count.strip() in MISSING else int(count)
        if source_count is not None and source_count < 0:
            raise ValueError("negative source count")
    except ValueError as exc:
        raise UpstreamFormatError("invalid numeric annotation") from exc
    for role, entity in [("regulator", regulator), ("target", target)]:
        if entity.identity_state != "declared_namespaced":
            warnings.append(f"{role}:{entity.identity_state}")
        allowed = {"TF", "miRNA", "lncRNA", "circRNA"} | ({"Gene"} if role == "target" else set())
        if entity.source_type not in allowed:
            warnings.append(f"{role}:unrecognised_type:{entity.source_type}")
        opposite_prefix = "mmu_circ" if species == "human" else "hsa_circ"
        if opposite_prefix in entity.raw_identifier:
            warnings.append(f"{role}:species_identifier_conflict")
    if references.strip() in MISSING:
        warnings.append("references:missing_in_upstream")
    return Interaction("interaction_" + _digest([species, raw]), regulator, target, membership,
                       None if confidence.strip() in MISSING else confidence, source_count, databases,
                       score, parse_references(references), references, raw, tuple(warnings))


@dataclass(frozen=True)
class QueryPage:
    records: tuple[Interaction, ...]
    upstream_total_candidates: int


def parse_query_page(payload: Any, species: str) -> QueryPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        raise UpstreamFormatError("expected a query object containing result list")
    count = payload.get("totalResults")
    if type(count) is not int or count < 0:
        raise UpstreamFormatError("expected nonnegative integer totalResults")
    records = tuple(parse_interaction(row, species) for row in payload["result"])
    if len(records) > count:
        raise UpstreamFormatError("page length exceeds upstream candidate count")
    return QueryPage(records, count)


def parse_mapping_list(text: str, species: str, *, max_records: int = 100_000) -> tuple[Interaction, ...]:
    if not isinstance(text, str):
        raise UpstreamFormatError("expected text mapping result")
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(records) >= max_records:
            error = UpstreamFormatError('mapping exceeds the response record budget')
            error.code = 'UPSTREAM_RECORD_LIMIT'
            raise error
        records.append(parse_interaction(line, species))
    return tuple(records)
