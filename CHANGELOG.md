# Changelog

Versions follow the `0.x` series while the public interfaces and research workflows mature.

## 0.2.0

- Add `download_official_file` for one file selected from RegNetwork's official download catalog.
- Save the `.7z` archive and a provenance manifest under the local data root; compute a local SHA-256, enforce an 8 GiB per-file limit, and do not extract archives.
- Keep upstream access read-only and never download files automatically.

## 0.1.0

- First planned public release of the local stdio MCP package.
- Online-first, read-only access to RegNetwork.
- Eight tools for status, entity resolution, interaction queries, neighbors, subnetworks, summaries, evidence fields and exports.
- Bounded local cache and TSV, JSON and GraphML export.
