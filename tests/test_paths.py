from pathlib import Path

from regnetwork_mcp import paths


def test_explicit_root_overrides_environment_and_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("REGNETWORK_MCP_ROOT", str(tmp_path / "environment"))
    assert paths.resolve_data_root(tmp_path / "explicit") == tmp_path / "explicit"


def test_environment_override_is_independent_of_current_directory(tmp_path, monkeypatch):
    chosen = tmp_path / "chosen-data"
    monkeypatch.setenv("REGNETWORK_MCP_ROOT", str(chosen))
    monkeypatch.chdir(tmp_path)
    assert paths.resolve_data_root() == chosen


def test_windows_default_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("REGNETWORK_MCP_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths.resolve_data_root() == tmp_path / "Local" / "RegNetworkMCP"


def test_windows_default_falls_back_to_user_profile_not_current_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.platform, "system", lambda: "Windows")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("REGNETWORK_MCP_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.resolve_data_root() == Path.home() / "AppData" / "Local" / "RegNetworkMCP"


def test_posix_default_respects_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("REGNETWORK_MCP_ROOT", raising=False)
    assert paths.resolve_data_root() == tmp_path / "xdg" / "regnetwork-mcp"


def test_server_default_uses_resolved_data_root(tmp_path, monkeypatch):
    from regnetwork_mcp import server

    constructed = []

    class StubService:
        def __init__(self, root):
            constructed.append(root)

    monkeypatch.setenv("REGNETWORK_MCP_ROOT", str(tmp_path / "mcp-data"))
    monkeypatch.setattr(server, "ResearchService", StubService)
    server.create_server()
    assert constructed == [tmp_path / "mcp-data"]
