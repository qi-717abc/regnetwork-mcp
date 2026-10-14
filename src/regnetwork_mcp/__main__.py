"""Local stdio MCP entry point; ordinary logs belong on stderr."""
import argparse
from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="Online-first read-only RegNetwork MCP (stdio)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    from .server import create_server
    create_server().run(transport='stdio')


if __name__ == "__main__":
    main()
