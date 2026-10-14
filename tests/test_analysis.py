from regnetwork_mcp.analysis import extract_induced, resolve_scan, select_interactions, summarize_subnetwork
from regnetwork_mcp.records import parse_interaction
from regnetwork_mcp.upstream import ScanResult


def edge(source="TP53", target="BAX", source_id="NCBI:7157", target_id="NCBI:581", score="0.8"):
    return parse_interaction("\t".join([source,source_id,target,target_id,"TF","Gene","core","High","1","TRRUST",score,"N/A"]), "human")


def scan(records, complete=True):
    return ScanResult(records=list(records), upstream_total_candidates=len(records), complete=complete)


def test_exact_symbol_and_direction_do_not_select_prefix_neighbors():
    page = scan([edge(), edge(source="TP53INP1", source_id="NCBI:94241"), edge(source="MDM2", target="TP53", source_id="NCBI:4193", target_id="NCBI:7157")])
    assert len(select_interactions(page, symbol="TP53")["records"]) == 2
    outgoing = select_interactions(page, symbol="TP53", direction="outgoing")
    incoming = select_interactions(page, symbol="TP53", direction="incoming")
    assert outgoing["records"][0]["target"]["symbol"] == "BAX"
    assert incoming["records"][0]["regulator"]["symbol"] == "MDM2"


def test_partial_scan_cannot_prove_zero_exact_matches_or_unmapped_identity():
    page = scan([edge(source="TP53INP1", source_id="NCBI:94241")], complete=False)
    result = select_interactions(page, symbol="TP53")
    assert result["status"] == "partial" and result["matched_total"] is None
    assert resolve_scan(page,"TP53")["state"] == "partial"


def test_same_symbol_multiple_ids_remains_ambiguous_until_selection():
    page = scan([edge(source_id="NCBI:7157"), edge(source_id="NCBI:9999")])
    assert resolve_scan(page,"TP53")["state"] == "ambiguous"
    assert "AMBIGUOUS_ENTITY" in select_interactions(page,symbol="TP53")["warnings"][-1]
    result = select_interactions(page,symbol="TP53",entity_id=page.records[0].regulator.entity_id)
    assert len(result["records"]) == 1


def test_return_limit_and_unknown_score_are_reported_separately():
    page = scan([edge(),edge(target="MDM2",target_id="NCBI:4193"),edge(target="MYC",target_id="NCBI:4609",score="N/A")])
    limited = select_interactions(page,symbol="TP53",limit=1)
    assert limited["matched_total"] == 3 and limited["next_offset"] == 1 and limited["truncated"]
    filtered = select_interactions(page,symbol="TP53",min_score=0.8)
    assert filtered["matched_total"] is None and filtered["missing_score_records_excluded"] == 1
    assert "MISSING_SCORE_FOR_FILTER" in filtered["warnings"]


def test_induced_boundary_and_unverified_input_not_called_known_isolate():
    network = extract_induced([edge(),edge(target="MDM2",target_id="NCBI:4193")],["TP53","BAX","UNKNOWN"])
    assert network["raw_record_count"] == 1 and network["excluded_outside_input"] == 1
    assert network["input_states"][-1]["state"].startswith("not_observed_in_mapping")
    assert summarize_subnetwork(network)["isolated_entity_ids"] == []


def test_verified_source_candidate_without_induced_edges_can_be_retained():
    resolution = resolve_scan(scan([edge(source="MYC",source_id="NCBI:4609")]),"MYC")
    network = extract_induced([edge()],["TP53","BAX","MYC"],resolutions={"MYC":resolution})
    summary = summarize_subnetwork(network)
    assert summary["node_count"] == 3 and len(summary["isolated_entity_ids"]) == 1
    assert network["input_states"][-1]["state"] == "source_entity_without_induced_edges"


def test_duplicate_observations_and_self_loops_do_not_inflate_unique_degree():
    loop = edge(target="TP53",target_id="NCBI:7157")
    network = extract_induced([edge(),edge(),loop],["TP53","BAX"])
    summary = summarize_subnetwork(network)
    assert summary["raw_record_count"] == 3 and summary["unique_endpoint_pair_count"] == 2
    assert summary["self_loop_count"] == 1
    assert summarize_subnetwork(network,include_self_loops=False)["unique_endpoint_pair_count"] == 1


def test_known_truncated_mapping_cannot_assert_input_is_isolated():
    network = extract_induced([edge(),edge(source="MYC",source_id="NCBI:4609")],["TP53","BAX","MYC"],max_edges=1)
    assert network["truncated"]
    assert network["input_states"][-1]["state"] == "not_observed_in_truncated_output"
