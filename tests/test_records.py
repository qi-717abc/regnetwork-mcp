"""Regression cases derived from the actual RegNetwork data review."""
import pytest
from regnetwork_mcp.records import UpstreamFormatError, parse_entity, parse_interaction, parse_query_page, parse_references


def row(*, source="TP53", source_id="NCBI:7157", target="BAX", target_id="NCBI:581", target_type="Gene", score="0.2", refs="N/A"):
    return "\t".join([source, source_id, target, target_id, "TF", target_type, "non-core", "Low", "1", "RNAinterLow", score, refs])


def test_source_boundary_label_is_preserved_not_reclassified_from_help():
    record = parse_interaction(row(), "human")
    assert record.score == 0.2
    assert record.confidence_raw == "Low"
    assert record.references == ()
    assert "references:missing_in_upstream" in record.warnings


def test_precursor_and_mature_mirna_do_not_merge_by_symbol():
    precursor = parse_entity("human", "hsa-let-7b", "miRBase:MI0000063", "miRNA")
    mature = parse_entity("human", "hsa-let-7b", "miRBase:MIMAT0000063", "miRNA")
    assert precursor.entity_id != mature.entity_id


def test_aliases_share_declared_id_but_species_do_not():
    first = parse_entity("human", "hsa-miR-101", "miRBase:MIMAT0000099", "miRNA")
    alias = parse_entity("human", "hsa-miR-101-3p", "miRBase:MIMAT0000099", "miRNA")
    other_species = parse_entity("mouse", "hsa-miR-101", "miRBase:MIMAT0000099", "miRNA")
    assert first.entity_id == alias.entity_id
    assert first.entity_id != other_species.entity_id


def test_missing_ids_do_not_collapse_unrelated_symbols():
    first = parse_entity("human", "ARNTL", "NA", "TF")
    second = parse_entity("human", "SUMO2/SUMO3", "NA", "TF")
    assert first.identity_state == "missing_id"
    assert first.entity_id != second.entity_id


def test_grouped_accessions_are_not_exploded_into_independent_edges():
    entity = parse_entity("human", "miR-194-5p", "miRBase:MI0000488;MI0000732", "miRNA")
    assert entity.identity_state == "grouped_source"
    assert entity.accession is None
    assert entity.raw_identifier == "miRBase:MI0000488;MI0000732"


def test_unknown_source_type_retained_with_warning():
    record = parse_interaction(row(target="PTEN", target_type="Protein"), "human")
    assert record.target.source_type == "Protein"
    assert "target:unrecognised_type:Protein" in record.warnings


def test_reference_dedup_keeps_methods_unknown_tokens_and_occurrence_counts():
    references = parse_references("123(Experimental Research);123(Experimental Research);123(Other/Unclassified);unparsed citation")
    assert len(references) == 3
    assert references[0].occurrences == 2
    assert references[1].method == "Other/Unclassified"
    assert references[2].parse_state == "unparsed"
    assert references[2].raw == "unparsed citation"


@pytest.mark.parametrize("payload", [{"error": "server failed"}, {"result": [], "totalResults": True}, {"result": [], "totalResults": -1}, "HTML page"])
def test_error_or_changed_shape_is_not_treated_as_empty(payload):
    with pytest.raises(UpstreamFormatError):
        parse_query_page(payload, "human")


def test_successful_empty_is_distinct_from_error():
    page = parse_query_page({"result": [], "totalResults": 0}, "human")
    assert page.records == ()
    assert page.upstream_total_candidates == 0


@pytest.mark.parametrize("score", ["Infinity", "-0.1", "1.1", "not-a-score"])
def test_invalid_score_does_not_become_fabricated_zero(score):
    with pytest.raises(UpstreamFormatError):
        parse_interaction(row(score=score), "human")


def test_six_field_snapshot_not_misparsed_as_enriched_online_record():
    with pytest.raises(UpstreamFormatError):
        parse_interaction("TP53\t7157\tBAX\t581\tTF\tGene", "human")
