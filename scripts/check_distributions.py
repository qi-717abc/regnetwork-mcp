"""Check that wheel and source archive contain only intended public package files."""
from email.parser import BytesParser
import argparse
from pathlib import Path, PurePosixPath
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FORBIDDEN = {"research", ".runtime", ".cache", "exports", "tests", "AGENTS.md",
             "ACTION_LOG.md", "EXECUTION_PLAN.md", "uv.lock"}
EXPECTED_SOURCE_FILES = {"README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml",
                         ".gitignore", "PKG-INFO"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=DIST,
                        help="directory containing exactly one wheel and one sdist")
    args = parser.parse_args()
    wheels = list(args.dist_dir.glob("regnetwork_mcp-*-py3-none-any.whl"))
    sources = list(args.dist_dir.glob("regnetwork_mcp-*.tar.gz"))
    assert len(wheels) == len(sources) == 1, f"expected exactly one wheel and sdist in {args.dist_dir}"

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        assert all(not (set(PurePosixPath(name).parts) & FORBIDDEN) for name in names)
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        assert metadata["Name"] == "regnetwork-mcp"
        assert metadata["Version"] == "0.2.0"
        assert metadata.get_content_type() == "text/plain"
        entrypoints = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "regnetwork-mcp = regnetwork_mcp.__main__:main" in wheel.read(entrypoints).decode()
        assert any(name.startswith("regnetwork_mcp/") and name.endswith(".py") for name in names)

    with tarfile.open(sources[0], "r:gz") as source:
        members = [member.name for member in source.getmembers() if member.isfile()]
        relative_files = {"/".join(PurePosixPath(name).parts[1:]) for name in members}
        assert EXPECTED_SOURCE_FILES <= relative_files
        assert all(not (set(PurePosixPath(name).parts) & FORBIDDEN) for name in members)
        assert all(name in EXPECTED_SOURCE_FILES or name.startswith("src/regnetwork_mcp/") for name in relative_files)

    print(f"distribution contents passed: {wheels[0].name}, {sources[0].name}")


if __name__ == "__main__":
    main()
