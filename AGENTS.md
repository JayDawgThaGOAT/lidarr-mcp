# AGENTS.md — lidarr-mcp

MCP server exposing Lidarr's v1 REST API (OpenAPI 3.0.4) as tools so an LLM can read and manage a Lidarr instance: artists, albums, tracks, track files, queue, wanted, history, indexers, import lists, metadata profiles, custom formats, tags, commands, system status, and more. Full surface — reads and writes. Uses FastMCP, `uv` for deps.

Exposed as **15 resource-scoped portmanteau tools**, not one tool per endpoint — see "Tool registry and the spec" below. A one-tool-per-endpoint version of this API would register 221 endpoints individually, blowing the MCP context budget (~221 tools × ~250 tokens ≈ 55k tokens just for this one server); the grouped version costs roughly a tenth of that.

## Testing
- Offline suite: `make test` (or `uv run pytest`)
- Live integration (needs `LIDARR_URL`/`LIDARR_API_KEY`): `make test-integration`
  - GET endpoints run against the live instance.
  - POST/PUT/DELETE only run when `LIDARR_WRITE_TESTS=1` (safe create→update→delete cycles against a scratch tag, then cleanup). Never point write tests at a production library.

## Tool registry and the spec
- `_TOOL_REGISTRY` in `lidarr_mcp.py` is generated from the vendored spec at `tests/data/lidarr_openapi.json` (pinned to Lidarr develop HEAD d88cbcec050c04b3064c9f36221edac152e241e2). It lists every JSON-producing endpoint under `/api/v1` plus `GET /ping`.
- Excluded on purpose: `/login`, `/logout`, static web routes, the `.ics` calendar feed, and binary/text endpoints (media covers, raw log files) — `_req` JSON-decodes every response. `/ping` is kept.
- To add a tool or refresh coverage, regenerate the registry from a newer `openapi.json` (same algorithm as the authoring script) and re-run the tests. Do not hand-edit the registry.
- Query-param defaults must match the spec. Run `python3 scripts/sync_registry_defaults.py` to re-sync them after a spec refresh (`--check` exits 1 when they drift; `tests/test_tools.py::test_registry_query_defaults_match_spec` covers this offline).
  - A spec-declared `true` boolean must default to `True`. Generating every boolean with a hardcoded `False` silently overrides API defaults — the bug fixed in `fix/boolean-query-param-defaults`, where `wanted/missing.monitored` and `queue.removeFromClient` (destructive) both inverted.
  - A boolean with **no** spec default keeps `False`. ASP.NET binds an absent non-nullable `bool` to false, so this matches the API for plain filters; and `trackfile.unmapped` belongs to an undocumented any-one-of group (`artistId`/`albumId`/`trackFileIds`/`unmapped`) that answers an omitted `unmapped` with 400. Do not "fix" these to `None` — `_omit` keeps `False`, so a wrong declared default is sent, not ignored.
- Endpoint function naming (internal, no longer an MCP tool name): `lidarr_<verb>_<resource>` derived from path + method (e.g. `lidarr_list_artist`, `lidarr_add_album`, `lidarr_delete_trackfile`, `lidarr_run_command`). Overrides for flagship/action endpoints live in the authoring script.

## Portmanteau registration — **do not go back to one tool per endpoint**
- `_GROUPS` buckets every `_TOOL_REGISTRY` name into one of 15 resource groups (`lidarr_media_library`, `lidarr_queue`, `lidarr_config`, ...). `register_tools()` registers exactly one MCP tool per group via `_register_group`, which wraps the group's endpoint functions in a single `dispatch(operation, arguments)` closure. The endpoint functions themselves are unchanged — they're plain callables looked up by name, not separately-registered tools.
- `operation` is typed `Literal[<the group's endpoint names>]`, so FastMCP/pydantic validates it against the real endpoint list before `dispatch` ever runs — an invalid operation never reaches the group tool's body.
- Adding a new endpoint: add its entry to `_TOOL_REGISTRY` as before, then add its name to exactly one group in `_GROUPS`. `tests/test_tools.py::test_all_registry_names_grouped` fails if you forget.
- New resource area big enough to need its own group (rare): add a new `_GROUPS` key. Keep the total group count at or under ~15 — that ceiling is the entire point of this pattern.
- If you're tempted to add a per-endpoint `@mcp.tool` or an extra `mcp.add_tool` call outside `_register_group`, don't — every endpoint must be reachable only via its group's `operation` enum. A 221-tool server (one per endpoint) would cost ~55k tokens of system-prompt budget on every session start; the 15-tool grouped version costs roughly a tenth of that.

## Lidarr-specific notes
- The API base is `/api/v1`, not `/api/v3` like Sonarr/Radarr. Do not "fix" this to `/api/v3`.
- `Metadata` (`/api/v1/metadata*`) is the *consumer* config (Kodi/Plex/Emby NFO writers) and is grouped with notifications (`lidarr_notifications_metadata`). `MetadataProfile` (`/api/v1/metadataprofile*`) is the MusicBrainz release-status/type filter profile and is grouped with quality profiles (`lidarr_profiles_formats`). They are unrelated resources — keep them in their separate groups.
- Library resources are Artist/Album/Track/TrackFile (no Series/Episode/Movie). `rename` and `retag` are GET previews; the actual file writes go through `lidarr_run_command` (RenameTracks/RetagTags commands).

## Annotations convention
- A group tool is `readOnlyHint=True` (`READONLY`) only when *every* operation in it is a GET (e.g. `lidarr_wanted`, `lidarr_calendar`). Mixed groups carry no hints.
- Per-operation write/destructive notes survive in the group tool's description: each operation line still ends with its original one-line doc, and destructive/write endpoints keep a `WRITE:`/`DESTRUCTIVE:` note in that doc string (see `_TOOL_REGISTRY`'s `doc` field).
- `READONLY`/`WRITE`/`DESTRUCTIVE` constants are kept for reference and for any future per-operation annotation work, but only `READONLY` is actually applied today (to all-GET groups).

## Auth and base path
- Auth: `X-Api-Key` header (generate in Lidarr Settings > General > Security). Not bearer.
- `build_client` points at the origin with no path suffix; every registered tool carries its full path (`/api/v1/...` or `/ping`). `_req` raises `ToolError` with the API status and message on `>=400`.

## Release workflow
Always use the `make bump-*` targets to bump the version (`uv version --bump patch|minor|major`), which updates `pyproject.toml` and `uv.lock` together. Do NOT edit the version by hand.

- Bump: `make bump-patch` (or `bump-minor` / `bump-major`)
- Commit message is **just the version**, e.g. `0.1.2` — nothing else.
- Tag it `v<version>` (e.g. `v0.1.2`).
- Push main and the tag:
  ```
  git push origin main
  git push origin v<version>
  ```
- Deploy to the Proxmox host (root SSH key): pull the repo then reinstall the uv tool:
  ```
  ssh root@192.168.50.3 -- 'cd /root/lidarr-mcp && git fetch origin && git reset --hard origin/main'
  ssh root@192.168.50.3 -- 'cd /root/lidarr-mcp && uv tool install --force .'
  ```
  The host runs it via `uv tool install` → `/root/.local/bin/lidarr-mcp` (not from the repo). Locally it is registered in the christopfarr project opencode via `uv run --directory /home/savagecore/Documents/christopfarr/mcp/lidarr-mcp`.

## Initial state
Version starts at `0.0.0` in the initial commit. No tag on the scaffold commit; releases begin at the first `make bump-*`.
