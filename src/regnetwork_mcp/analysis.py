"""Deterministic scientific operations on observed, bounded query results."""
from dataclasses import asdict
from typing import Any
import networkx as nx

from .records import Entity, Interaction
from .upstream import ScanResult


def _node(entity: Entity) -> dict[str, Any]:
    return {"entity_id": entity.entity_id, "species": entity.species, "symbol": entity.symbol,
            "raw_identifier": entity.raw_identifier,
            "symbols": [entity.symbol], "namespace": entity.namespace, "accession": entity.accession,
            "source_types": [entity.source_type], "identity_state": entity.identity_state}


def resolve_scan(scan: ScanResult, symbol: str) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    for record in scan.records:
        for entity in [record.regulator, record.target]:
            if entity.symbol != symbol:
                continue
            node = candidates.setdefault(entity.entity_id, _node(entity))
            node["source_types"] = sorted(set(node["source_types"]) | {entity.source_type})
    values = sorted(candidates.values(), key=lambda n: n["entity_id"])
    if len(values) > 1:
        state = "ambiguous"
    elif not scan.complete:
        state = "partial"
    elif not values:
        state = "not_observed_in_dataset"
    elif values[0]["identity_state"] != "declared_namespaced":
        state = values[0]["identity_state"]
    else:
        state = "resolved_from_source"
    return {"symbol": symbol, "state": state, "candidates": values, "candidate_scan_complete": scan.complete,
            "warnings": list(scan.warnings), "authority_verified": False}


def select_interactions(scan: ScanResult, *, symbol: str, direction: str = "both",
                        entity_id: str | None = None, counterpart_symbol: str | None = None,
                        counterpart_entity_id: str | None = None,
                        min_score: float | None = None, limit: int = 30, offset: int = 0,
                        selected_records: list[Interaction] | None = None) -> dict[str, Any]:
    if direction not in {"incoming", "outgoing", "both"}:
        raise ValueError("invalid direction")
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("invalid result bounds")
    if min_score is not None and not 0 <= min_score <= 1:
        raise ValueError("invalid min_score")
    selected: list[Interaction] = []
    missing_scores = 0
    warnings = list(scan.warnings)
    def matches(entity: Entity) -> bool:
        return entity.entity_id == entity_id if entity_id else entity.symbol == symbol
    def counterpart_matches(entity: Entity) -> bool:
        if counterpart_entity_id is not None:
            return entity.entity_id == counterpart_entity_id
        return counterpart_symbol is None or entity.symbol == counterpart_symbol
    for record in scan.records:
        outgoing = matches(record.regulator) and counterpart_matches(record.target)
        incoming = matches(record.target) and counterpart_matches(record.regulator)
        if not ((direction in {"outgoing", "both"} and outgoing) or (direction in {"incoming", "both"} and incoming)):
            continue
        if min_score is not None:
            if record.score is None:
                missing_scores += 1
                continue
            if record.score < min_score:
                continue
        selected.append(record)
    # Preserve upstream order and record multiplicity, not a fabricated canonical dedup.
    returned = selected[offset:offset + limit]
    if selected_records is not None:
        selected_records.extend(returned)
    fully_filtered = scan.complete and missing_scores == 0
    truncated = len(selected) > offset + len(returned) or not fully_filtered
    if missing_scores:
        warnings.append("MISSING_SCORE_FOR_FILTER")
    resolution = resolve_scan(scan, symbol)
    if entity_id is None and resolution["state"] == "ambiguous":
        warnings.append("AMBIGUOUS_ENTITY:records preserve all exact-symbol candidates")
    if truncated or warnings:
        status = "partial"
    else:
        status = "ok" if returned else "empty"
    return {"status": status, "records": [r.to_dict() for r in returned],
            "matched_total": len(selected) if fully_filtered else None,
            "exact_matches_observed": len(selected), "returned": len(returned), "truncated": truncated,
            "candidate_scan_complete": scan.complete, "upstream_total_candidates": scan.upstream_total_candidates,
            "missing_score_records_excluded": missing_scores, "resolution": resolution,
            "next_offset": offset + len(returned) if offset + len(returned) < len(selected) else None,
            "warnings": warnings, "search_scope": "observed candidates of the requested symbol query; aliases are not exhaustive"}


def extract_induced(records: list[Interaction] | tuple[Interaction, ...], symbols: list[str], *,
                    resolutions: dict[str, dict[str, Any]] | None = None, max_edges: int = 5000,
                    selected_records: list[Interaction] | None = None) -> dict[str, Any]:
    symbols = list(dict.fromkeys(s.strip() for s in symbols))
    if not 1 <= len(symbols) <= 50 or any(not s for s in symbols) or not 1 <= max_edges <= 5000:
        raise ValueError("invalid subnetwork bounds")
    names = set(symbols)
    qualified = [r for r in records if r.regulator.symbol in names and r.target.symbol in names]
    kept = qualified[:max_edges]
    if selected_records is not None:
        selected_records.extend(kept)
    nodes: dict[str, dict[str, Any]] = {}
    for record in kept:
        for entity in [record.regulator, record.target]:
            node = nodes.setdefault(entity.entity_id, _node(entity))
            node["symbols"] = sorted(set(node["symbols"]) | {entity.symbol})
            node["source_types"] = sorted(set(node["source_types"]) | {entity.source_type})
    input_states = []
    connected_symbols = {e.symbol for r in kept for e in [r.regulator, r.target]}
    for symbol in symbols:
        resolution = (resolutions or {}).get(symbol)
        if symbol in connected_symbols:
            state = "observed_in_subnetwork"
        elif len(qualified) > len(kept):
            state = "not_observed_in_truncated_output"
        elif resolution and resolution["candidates"]:
            state = "source_entity_without_induced_edges"
            for candidate in resolution["candidates"]:
                nodes.setdefault(candidate["entity_id"], candidate)
        else:
            state = "not_observed_in_mapping;identity_not_verified"
        input_states.append({"symbol": symbol, "state": state, "resolution": resolution})
    return {"nodes": sorted(nodes.values(), key=lambda n: n["entity_id"]),
            "records": [r.to_dict() for r in kept], "raw_records": [r.raw_record for r in kept],
            "input_states": input_states, "raw_record_count": len(kept), "upstream_record_count": len(records),
            "qualified_record_count": len(qualified), "excluded_outside_input": len(records) - len(qualified),
            "truncated": len(qualified) > len(kept), "directed": True, "mode": "induced",
            "identity_policy": "declared namespace/accession; preserve grouped or unresolved source identities",
            "warnings": sorted({warning for r in kept for warning in r.warnings})}


def summarize_subnetwork(network: dict[str, Any], *, include_self_loops: bool = True) -> dict[str, Any]:
    graph = nx.DiGraph()
    graph.add_nodes_from(n["entity_id"] for n in network["nodes"])
    raw_used = 0
    for record in network["records"]:
        source, target = record["regulator"]["entity_id"], record["target"]["entity_id"]
        if not include_self_loops and source == target:
            continue
        raw_used += 1
        graph.add_edge(source, target)
    by_type: dict[str, int] = {}
    for node in network["nodes"]:
        for kind in node["source_types"]:
            by_type[kind] = by_type.get(kind, 0) + 1
    return {"node_count": graph.number_of_nodes(), "unique_endpoint_pair_count": graph.number_of_edges(),
            "raw_record_count": raw_used, "self_loop_count": nx.number_of_selfloops(graph),
            "weak_component_count": nx.number_weakly_connected_components(graph) if graph else 0,
            "in_degree": dict(graph.in_degree()), "out_degree": dict(graph.out_degree()),
            "node_role_counts": by_type, "isolated_entity_ids": sorted(nx.isolates(graph)),
            "identity_policy": network["identity_policy"], "include_self_loops": include_self_loops,
            "input_truncated": network["truncated"],
            "interpretation": "structural metrics of observed source records; not causal or disease-driver validation"}
