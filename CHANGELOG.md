# Changelog

Versions follow the `0.x` series while the public interfaces and research workflows mature.

## 0.2.1

- Preserve the nine MCP tools and scientific interpretation while improving resource limits and local I/O stability.
- Stream query responses with a 32 MiB decoded-byte budget and reject mappings above 100,000 records explicitly, without caching or presenting a truncated response as complete.
- Publish downloads and exports using process locks and recoverable file/manifest transactions; allow two distinct official downloads to progress while preserving request pacing and explicit overwrite protection.
- Move blocking cache, parsing, export and download writes off the event loop; batch related SQLite updates and reuse parsed interactions.
- Test interrupted processes, cancellation, concurrent access, compressed response limits, cache failures and the latest allowed MCP SDK in CI.

## 0.2.0

- Add `download_official_file` for one file selected from RegNetwork's official download catalog.
- Save the `.7z` archive and a provenance manifest under the local data root; compute a local SHA-256, enforce an 8 GiB per-file limit, and do not extract archives.
- Keep upstream access read-only and never download files automatically.

## 0.1.0

- First planned public release of the local stdio MCP package.
- Online-first, read-only access to RegNetwork.
- Eight tools for status, entity resolution, interaction queries, neighbors, subnetworks, summaries, evidence fields and exports.
- Bounded local cache and TSV, JSON and GraphML export.
