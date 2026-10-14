"""Resolve a stable per-user directory for the small cache and exports."""
import os
from pathlib import Path
import platform


def resolve_data_root(root: Path | str | None = None) -> Path:
    """Return the application data root without depending on the launch folder.

    An explicit argument is useful to tests and embedding applications. The
    environment variable is the supported user override. Defaults follow the
    platform's normal per-user data directory conventions.
    """
    if root is not None:
        return Path(root).expanduser()

    configured = os.environ.get("REGNETWORK_MCP_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser()

    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "RegNetworkMCP"

    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home and data_home.strip() else Path.home() / ".local" / "share"
    return base / "regnetwork-mcp"
