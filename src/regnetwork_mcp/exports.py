"""Reproducible graph files restricted to an operator-controlled export directory."""
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any
import networkx as nx

from . import __version__


FIELDS = ["regulator_symbol", "regulator_id", "target_symbol", "target_id", "regulator_type", "target_type",
          "membership_raw", "confidence_raw", "source_count_inferred", "source_databases_raw", "score", "references_raw"]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def _attribute(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list, tuple)) else value


def _graphml(network: dict[str, Any]) -> bytes:
    graph = nx.MultiDiGraph()
    graph.graph["source"] = "RegNetwork"
    graph.graph["identity_policy"] = network["identity_policy"]
    graph.graph["truncated"] = network["truncated"]
    for node in sorted(network["nodes"], key=lambda n: n["entity_id"]):
        attributes = {k: _attribute(v) for k, v in node.items() if k != "entity_id" and v is not None}
        graph.add_node(node["entity_id"], **attributes)
    for index, record in enumerate(sorted(network["records"], key=lambda r: (r["interaction_id"], r["raw_record"]))):
        attributes = {k: record[k] for k in ["interaction_id", "confidence_raw", "score", "references_raw", "source_databases_raw", "raw_record"] if record[k] is not None}
        graph.add_edge(record["regulator"]["entity_id"], record["target"]["entity_id"], key=str(index), **attributes)
    output = io.BytesIO()
    nx.write_graphml(graph, output, encoding="utf-8", infer_numeric_types=True)
    return output.getvalue()


def export_network(network: dict[str, Any], output_dir: Path, format_name: str) -> dict[str, Any]:
    if format_name not in {"tsv", "json", "graphml"}:
        raise ValueError("supported exports are tsv, json, graphml")
    network_bytes = _json(network)
    network_hash = hashlib.sha256(network_bytes).hexdigest()
    identity = hashlib.sha256(_json(["export-v1", network_hash, format_name])).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir.resolve()
    path = root / f"regnetwork-{identity}.{format_name}"
    manifest_path = root / f"regnetwork-{identity}.manifest.json"
    if not path.resolve().is_relative_to(root) or not manifest_path.resolve().is_relative_to(root):
        raise ValueError("export target escapes configured directory")
    if format_name == "json":
        content = network_bytes
    elif format_name == "graphml":
        content = _graphml(network)
    else:
        text = io.StringIO(newline="")
        writer = csv.writer(text, delimiter="\t", lineterminator="\n")
        writer.writerow(FIELDS)
        for record in network["records"]:
            writer.writerow(record["raw_record"].split("\t"))
        content = text.getvalue().encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    manifest = {"format": format_name, "sha256": digest, "bytes": len(content), "network_sha256": network_hash,
                "software_version": __version__, "directed": True, "raw_record_count": network["raw_record_count"],
                "node_count": len(network["nodes"]), "truncated": network["truncated"],
                "identity_policy": network["identity_policy"], "input_states": network["input_states"],
                "provenance": network.get("provenance"), "score_is_not_effect_strength": True,
                "activation_inhibition_sign_available": False}
    for target, body in [(path, content), (manifest_path, _json(manifest))]:
        if target.exists():
            if target.read_bytes() != body:
                raise FileExistsError("deterministic export path contains different data")
        else:
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
    return {"path": str(path), "resource_uri": path.as_uri(), "sha256": digest,
            "manifest_path": str(manifest_path), "bytes": len(content), "format": format_name}
