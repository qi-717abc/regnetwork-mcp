import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ARGS = ["--python", "3.13", "--from", "regnetwork-mcp==0.2.1", "regnetwork-mcp"]


def test_claude_and_generic_stdio_example_uses_published_package_not_source_tree():
    config = json.loads((ROOT / "examples" / "mcp-config.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["regnetwork"]
    assert server == {"type": "stdio", "command": "uvx", "args": EXPECTED_ARGS}
    assert "Q:\\scbase" not in json.dumps(config)


def test_codex_example_uses_the_same_pinned_package():
    config = tomllib.loads((ROOT / "examples" / "codex-config.toml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["regnetwork"] == {"command": "uvx", "args": EXPECTED_ARGS}


def test_project_has_one_dynamic_version_source_and_includes_license_and_readme():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dynamic"] == ["version"]
    assert project["project"]["license"] == "MIT"
    assert project["project"]["readme"] == "README.md"
    assert (ROOT / "LICENSE").is_file()
