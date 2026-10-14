# Maintainer release steps

This document describes the first public release. Publishing is separate from local testing: a successful local build does not mean the package is available on GitHub or PyPI.

## One-time account setup

1. Create the public GitHub repository that will hold the clean first-release snapshot. Do not push the existing private project history or `research/regnetwork` materials.
2. In GitHub repository settings, create two Actions environments named `testpypi` and `pypi`. Add a required reviewer to `pypi`; leave `testpypi` without a reviewer so the first upload can run automatically.
3. On TestPyPI, add a Trusted Publisher for this GitHub repository, workflow `.github/workflows/release.yml`, and environment `testpypi`. If the project does not exist yet, use TestPyPI's pending publisher setup.
4. On PyPI, add the matching Trusted Publisher, using the same repository and workflow with environment `pypi`. If necessary, use PyPI's pending publisher setup for the first upload.
5. Keep publishing permission scoped to the release jobs. The workflow requests GitHub's OIDC token only for the two upload jobs; no PyPI password or long-lived token is stored in this repository.

The repository owner/name must match the repository where this clean snapshot is published. The project name on both package indexes is `regnetwork-mcp`.

## Release sequence

1. Update `src/regnetwork_mcp/__init__.py`, `CHANGELOG.md`, and the pinned install examples together. Confirm `pyproject.toml` reads the version from the Python package.
2. Run the exact locked workflow commands: `uv sync --locked`, `uv run --locked --no-sync python -m pytest -q`, `uv run --locked --no-sync uv build --no-sources --no-build-isolation`, and `uv run --locked --no-sync python scripts/check_distributions.py --dist-dir dist`. Verify both wheel and sdist installation and stdio startup from outside the source checkout with the MCP SDK client, using an explicit cache/export data root.
3. Commit the release changes and create a matching tag such as `v0.1.0`.
4. The release workflow tests and builds distributions, publishes them to TestPyPI, and then waits for approval at the GitHub `pypi` environment.
5. Before approving production, install the TestPyPI package into a throwaway Python 3.13 environment, verify its version and start the MCP with an SDK client. Inspect the TestPyPI project page and build files. Approve the `pypi` environment only after those checks pass.
6. After production publication, verify the PyPI project page and run the exact pinned `uvx` command from outside the source checkout. Then create the corresponding GitHub release notes.

If a version is already uploaded to an index, do not try to overwrite it. Correct the issue in a new patch version and matching tag.
