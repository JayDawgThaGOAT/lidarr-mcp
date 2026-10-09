# lidarr-mcp

Part of the [arr-mcps](https://github.com/arr-mcps/arr-mcps) collection.
MCP server exposing [Lidarr](https://lidarr.audio)'s v1 REST API
([OpenAPI 3.0.4](https://lidarr.audio/docs/api/)) as tools, so an LLM can read
and manage a Lidarr instance: artists, albums, tracks, track files, the
download queue, wanted/missing, history, indexers, import lists, metadata
profiles, custom formats, tags, commands, system status, and more. Full
surface — reads **and** writes, with destructive tools flagged.

Built with [FastMCP](https://gofastmcp.com).

## Getting an API key

Generate one in Lidarr **Settings > General > Security**. Auth is the
`X-Api-Key` header.

## Install

Download a wheel from the [latest release](https://github.com/arr-mcps/lidarr-mcp/releases/latest)
and install it as a `uv` tool (no repo checkout needed):

```bash
uv tool install lidarr_mcp-*.whl
```

This puts a `lidarr-mcp` command on your PATH. Register it with Claude Code:

```bash
claude mcp add lidarr \
  --env LIDARR_URL=http://your-lidarr-host:8686 \
  --env LIDARR_API_KEY=<key> \
  -- lidarr-mcp
```

### From source

```bash
uv sync
cp .env.example .env   # fill in LIDARR_URL and LIDARR_API_KEY
```

```bash
claude mcp add lidarr \
  --env LIDARR_URL=http://your-lidarr-host:8686 \
  --env LIDARR_API_KEY=<key> \
  -- uv run --directory /path/to/lidarr-mcp lidarr-mcp
```

## Config

| Env var | Required | Default |
|---|---|---|
| `LIDARR_URL` | yes | - |
| `LIDARR_API_KEY` | yes* | none (no `X-Api-Key` header sent if unset) |

\* Every API endpoint requires auth; practically you must set it, but the
server still starts without one so errors surface from the API rather than at
startup.

## Tools

**15 resource-scoped tools**, each covering multiple Lidarr v1 endpoints (223
total) via an `operation` parameter. Call a tool with `operation` set to one
of its listed operations and an `arguments` dict matching that operation's
parameters — the tool's own description (visible to your MCP client) lists
every operation, its signature, and a one-line doc. This keeps the full REST
surface available while costing a fraction of the context budget of
registering all 223 endpoints as separate tools.

| Tool | Operations | Kind |
|---|---|---|
| `lidarr_media_library` | 26 | reads + writes |
| `lidarr_profiles_formats` | 26 | reads + writes |
| `lidarr_config` | 25 | reads + writes |
| `lidarr_system_commands` | 23 | reads + writes |
| `lidarr_indexers` | 23 | reads + writes |
| `lidarr_notifications_metadata` | 18 | reads + writes |
| `lidarr_tags` | 18 | reads + writes |
| `lidarr_import_lists` | 16 | reads + writes |
| `lidarr_download_clients` | 11 | reads + writes |
| `lidarr_storage` | 10 | reads + writes |
| `lidarr_queue` | 9 | reads + writes |
| `lidarr_history_blocklist` | 7 | reads + writes |
| `lidarr_release_search` | 5 | reads + writes |
| `lidarr_wanted` | 4 | read-only |
| `lidarr_calendar` | 2 | read-only |

Example: `lidarr_queue(operation="lidarr_delete_queue", arguments={"id": 42})`.
Endpoint-level naming (`lidarr_<verb>_<resource>`) is preserved as the
`operation` value, so the full endpoint list is still discoverable from each
group tool's description at runtime.

### Annotations

Each group tool carries all four MCP hints, chosen from the operations it can
dispatch to. Because a group mixes reads and writes, the hints are pessimistic
— a tool is only advertised as read-only when every operation behind it is a
GET:

| | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` |
|---|---|---|---|---|
| all-GET groups (`lidarr_wanted`, `lidarr_calendar`) | `true` | `false` | `true` | `true` |
| groups with no DELETE | `false` | `false` | `false` | `true` |
| groups that can DELETE | `false` | `true` | `false` | `true` |

The exact per-operation classification is also published in each tool's
`_meta` as `lidarr/operations` (`{operation: {method, risk}}`), since a single
annotation cannot describe 26 operations. Per-operation risk is still in the
description text too: `WRITE:` for POST/PUT, `DESTRUCTIVE:` for DELETE.

## Development

```bash
make help  # list all commands
```

| Command | Does |
|---|---|
| `make sync` | `uv sync` |
| `make test` | Offline tests - one per endpoint, mocked HTTP |
| `make test-integration` | Tests against the live instance (needs `LIDARR_URL`/`LIDARR_API_KEY`) |
| `make build` | Build wheel + sdist into `dist/` |
| `make bump-patch` / `bump-minor` / `bump-major` | Bump the version in `pyproject.toml` + `uv.lock` |
| `make clean` | Remove build artifacts |

The release workflow (`.github/workflows/release.yml`) builds and publishes to
[Releases](https://github.com/arr-mcps/lidarr-mcp/releases) whenever a `v*`
tag is pushed - so the usual flow is `make bump-patch`, commit, then tag and
push.

The integration suite is read-only by default (GET endpoints only). Set
`LIDARR_WRITE_TESTS=1` to also exercise POST/PUT/DELETE against a scratch tag
(created, updated, then deleted). Never run write tests against a production
library.
