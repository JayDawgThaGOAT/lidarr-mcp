"""Offline tests: one per Lidarr v1 endpoint, plus error-path tests.

No network. The list of operations is generated from the vendored spec at
tests/data/lidarr_openapi.json (same skip rules as the authoring script), and
each tool call is checked against the exact HTTP request it should produce
(method, path incl. path-param substitution, query params) via
httpx.MockTransport, using FastMCP's in-memory Client (see
https://gofastmcp.com/development/tests).
"""

import json
import ast
import os

import httpx
import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import lidarr_mcp

SPEC_PATH = os.path.join(os.path.dirname(__file__), "data", "lidarr_openapi.json")

EXCLUDE_PATHS = {"/", "/{path}", "/content/{path}", "/api", "/login", "/logout",
                 "/feed/v1/calendar/lidarr.ics"}
EXCLUDE_CONTENT_ENDPOINTS = {
    "/api/v1/mediacover/artist/{artistId}/{filename}",
    "/api/v1/mediacover/album/{albumId}/{filename}",
    "/api/v1/log/file/{filename}",
    "/api/v1/log/file/update/{filename}",
}


def spec_ops():
    """(method, path, op) for every endpoint the server is expected to wrap."""
    d = json.load(open(SPEC_PATH))
    ops = []
    for p, methods in d["paths"].items():
        for m, op in methods.items():
            if m in ("head", "parameters"):
                continue
            if p in EXCLUDE_PATHS or p in EXCLUDE_CONTENT_ENDPOINTS:
                continue
            if not (p.startswith("/api/v1") or p == "/ping"):
                continue
            ops.append((m.upper(), p, op))
    return ops


def registry_for(method, path):
    for spec in lidarr_mcp._TOOL_REGISTRY:
        if spec["method"] == method and spec["path"] == path:
            return spec
    raise AssertionError(f"no registry entry for {method} {path}")


def op_to_args(spec):
    """Build call args for a tool from its registry entry: path params get a
    sentinel value per their declared type."""
    args = {}
    for p in spec["pp"]:
        args[p["name"]] = "abc" if p["type"] == "str" else 1
    return args


def expected_path(spec):
    path = spec["path"]
    for p in spec["pp"]:
        path = path.replace("{" + p["wire"] + "}", "abc" if p["type"] == "str" else "1")
    return path


class Recorder:
    """Captures the single request made during a test and replays a canned response."""

    def __init__(self):
        self.method = None
        self.url = None
        self.headers = None
        self.params = None
        self.json = None
        self.response = httpx.Response(200, json={"success": True})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.method = request.method
        self.url = request.url
        self.headers = request.headers
        self.params = request.url.params
        self.json = json.loads(request.content) if request.content else None
        return self.response


@pytest.fixture
def recorder():
    return Recorder()


@pytest_asyncio.fixture
async def server(recorder, monkeypatch):
    transport = httpx.MockTransport(recorder.handler)
    client = lidarr_mcp.build_client("http://lidarr.example.com", "test-key", transport=transport)
    monkeypatch.setattr(lidarr_mcp, "_client", client)
    yield lidarr_mcp.mcp
    await client.aclose()


_OP_GROUP = {op: group for group, ops in lidarr_mcp._GROUPS.items() for op in ops}


async def call(server, tool, **kwargs):
    """Call `tool` (an endpoint operation name) through the portmanteau group
    tool that now hosts it, so every existing per-endpoint test keeps working
    unmodified aside from this helper."""
    async with Client(server) as c:
        return await c.call_tool(_OP_GROUP[tool], {"operation": tool, "arguments": kwargs})


# --- one test per endpoint ---------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [(m, p) for m, p, _ in spec_ops()],
    ids=[f"{m.lower()}_{p}" for m, p, _ in spec_ops()],
)
async def test_endpoint_mapping(server, recorder, method, path):
    spec = registry_for(method, path)
    await call(server, spec["name"], **op_to_args(spec))
    assert recorder.method == method
    assert recorder.url.path == expected_path(spec)


# --- coverage: registry == spec ---------------------------------------------

def test_registry_covers_spec_exactly():
    spec_ops_set = {(m, p) for m, p, _ in spec_ops()}
    registry_ops = {(s["method"], s["path"]) for s in lidarr_mcp._TOOL_REGISTRY}
    assert registry_ops == spec_ops_set


def test_all_registered_tools_have_unique_names():
    names = [s["name"] for s in lidarr_mcp._TOOL_REGISTRY]
    assert len(names) == len(set(names))


def test_all_registry_names_grouped():
    """Every registry endpoint must land in exactly one portmanteau group -
    this is the safety net for the group-tool consolidation."""
    registry_names = [s["name"] for s in lidarr_mcp._TOOL_REGISTRY]
    grouped_names = [n for names in lidarr_mcp._GROUPS.values() for n in names]
    assert sorted(grouped_names) == sorted(registry_names)
    assert len(grouped_names) == len(set(grouped_names))


async def test_group_tools_are_the_only_registered_tools(server):
    async with Client(server) as c:
        tools = await c.list_tools()
    assert {t.name for t in tools} == set(lidarr_mcp._GROUPS)


async def test_unknown_operation_rejected_by_schema(server):
    # The Literal[...] enum on `operation` means an invalid value never
    # reaches _register_group's dispatch body - pydantic rejects it first.
    with pytest.raises(ToolError, match="validation error"):
        async with Client(server) as c:
            await c.call_tool("lidarr_tags", {"operation": "not_a_real_operation"})


# --- tool annotations ----------------------------------------------------------

def test_operation_risk_matches_doc_markers():
    """`_op_risk` must agree with the WRITE:/DESTRUCTIVE: markers in the docs.

    The annotations are derived from the HTTP method rather than parsed out of
    the prose, so this pins the two together: if either the method mapping or a
    doc marker drifts, the mismatch surfaces here instead of silently
    mislabelling a destructive operation as a plain write.
    """
    disagreements = []
    for spec in lidarr_mcp._TOOL_REGISTRY:
        doc = spec.get("doc", "")
        risk = lidarr_mcp._op_risk(spec["method"])
        if risk == "destructive":
            marked = "DESTRUCTIVE:" in doc
        elif risk == "write":
            marked = "WRITE:" in doc
        else:
            marked = "WRITE:" not in doc and "DESTRUCTIVE:" not in doc
        if not marked:
            disagreements.append(f"{spec['name']}: method={spec['method']} risk={risk} doc={doc!r}")
    assert not disagreements, "risk classification disagrees with doc markers:\n  " + "\n  ".join(
        disagreements
    )


def test_group_annotations_are_pessimistic():
    """A group's hints must not understate the risk of any operation it hosts."""
    method_of = {spec["name"]: spec["method"] for spec in lidarr_mcp._TOOL_REGISTRY}
    for group, names in lidarr_mcp._GROUPS.items():
        methods = {method_of[n] for n in names}
        ann = lidarr_mcp._group_annotations(methods)
        assert ann is not None, f"{group} carries no annotations"
        expected_readonly = methods == {"GET"}
        expected_destructive = not expected_readonly and "DELETE" in methods
        assert ann.readOnlyHint is expected_readonly, group
        assert ann.destructiveHint is expected_destructive, group
        # Deletes exist, so repetition is never safe to advertise.
        assert ann.idempotentHint is expected_readonly, group
        # Every operation reaches the remote API.
        assert ann.openWorldHint is True, group
        assert ann.readOnlyHint is False or ann.destructiveHint is False, group


async def test_group_tools_publish_annotations_and_operation_meta(server):
    """Every group tool exposes all four hints and the per-operation map."""
    method_of = {spec["name"]: spec["method"] for spec in lidarr_mcp._TOOL_REGISTRY}
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    for group, names in lidarr_mcp._GROUPS.items():
        tool = tools[group]
        ann = tool.annotations
        assert ann is not None, group
        # All four hints set explicitly, never left to the MCP defaults.
        dumped = ann.model_dump(exclude_none=True)
        assert set(dumped) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}, (
            group,
            dumped,
        )
        ops = (tool.meta or {}).get("lidarr/operations")
        assert ops is not None, f"{group} has no per-operation meta"
        assert set(ops) == set(names), group
        for name, entry in ops.items():
            assert entry["method"] == method_of[name], name
            assert entry["risk"] == lidarr_mcp._op_risk(method_of[name]), name


async def test_destructive_operation_is_reachable_only_from_destructive_group(server):
    """Sanity check the wiring: a DELETE lives in a group flagged destructive."""
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    group_of = {n: g for g, names in lidarr_mcp._GROUPS.items() for n in names}
    for spec in lidarr_mcp._TOOL_REGISTRY:
        if spec["method"] != "DELETE":
            continue
        tool = tools[group_of[spec["name"]]]
        assert tool.annotations.destructiveHint is True, spec["name"]
        assert tool.annotations.readOnlyHint is False, spec["name"]


# --- query params --------------------------------------------------------------

def spec_query_defaults():
    """(method, path) -> {wire_name: schema} for every query param in the spec."""
    d = json.load(open(SPEC_PATH))
    out = {}
    for path, methods in d["paths"].items():
        for method, op in methods.items():
            if method in ("head", "parameters"):
                continue
            out[(method.upper(), path)] = {
                p["name"]: (p.get("schema") or {})
                for p in op.get("parameters", [])
                if p.get("in") == "query"
            }
    return out


def _effective_declared(raw):
    """Map a registry default snippet to what `_omit` actually sends.

    `_omit` drops values that are `""` or `None`, so those are equivalent to
    omitting the param; everything else (`False` included) is sent as-is.
    """
    value = ast.literal_eval(raw) if isinstance(raw, str) else raw
    return None if value in (None, "") else value


def test_registry_query_defaults_match_spec():
    """Every *spec-declared* query-param default must match the vendored spec.

    Guards the regression where all boolean params were generated with a
    hardcoded `False`, silently overriding API defaults that are `true`
    (e.g. wanted/missing `monitored`, queue `removeFromClient`).

    Params the spec gives no default for are deliberately not asserted here:
    they keep `False`, which matches the API's binding for plain filters and is
    required for params in an undocumented any-one-of group.
    """
    spec_defaults = spec_query_defaults()
    drift = []
    for spec in lidarr_mcp._TOOL_REGISTRY:
        available = spec_defaults.get((spec["method"], spec["path"]), {})
        for q in spec.get("qp", []):
            schema = available.get(q["wire"])
            if schema is None or "default" not in schema:
                continue
            declared = _effective_declared(q["default"])
            # An empty spec default (`''`) and no default are both "omit" in
            # sent-value terms; only `True`/`False` defaults are meaningful.
            expected = schema["default"]
            expected = None if expected in (None, "") else expected
            if declared != expected:
                drift.append(
                    f"{spec['name']}.{q['wire']}: registry={q['default']!r} spec={schema['default']!r}"
                )
    assert not drift, "registry query defaults drifted from spec:\n  " + "\n  ".join(drift)


async def test_boolean_default_true_is_sent(server, recorder):
    """A spec-`true` boolean must default to `true`, not the old hardcoded false."""
    await call(server, "lidarr_list_wanted_missing", page_size=1)
    assert recorder.params["monitored"] == "true"


async def test_boolean_without_spec_default_is_sent_as_false(server, recorder):
    """A boolean with no spec default keeps `False`.

    Omitting it would be a 400 for `trackfile.unmapped`, which belongs to an
    undocumented any-one-of group; `false` is accepted and is what an absent
    non-nullable bool binds to server-side.
    """
    await call(server, "lidarr_list_trackfile", artist_id=1)
    assert recorder.params["unmapped"] == "false"


async def test_boolean_with_spec_false_default_still_sent(server, recorder):
    """Params whose spec default really is false keep being sent explicitly."""
    await call(server, "lidarr_list_album")
    assert recorder.params["includeAllArtistAlbums"] == "false"


async def test_query_params_use_wire_names(server, recorder):
    await call(server, "lidarr_list_history", page=2, page_size=50, sort_key="date", sort_direction="descending")
    assert recorder.params["page"] == "2"
    assert recorder.params["pageSize"] == "50"
    assert recorder.params["sortKey"] == "date"
    assert recorder.params["sortDirection"] == "descending"


async def test_list_params_serialize_repeatedly(server, recorder):
    await call(server, "lidarr_list_album", album_ids=[1, 2, 3])
    assert recorder.params.get_list("albumIds") == ["1", "2", "3"]


async def test_empty_optional_params_are_omitted(server, recorder):
    await call(server, "lidarr_list_artist")
    assert "mbId" not in recorder.params
    await call(server, "lidarr_list_album")
    assert "foreignAlbumId" not in recorder.params
    assert "includeAllArtistAlbums" in recorder.params  # bool False is sent explicitly


async def test_lookup_and_path_id_encoding(server, recorder):
    await call(server, "lidarr_lookup_artist", term="Radiohead")
    assert recorder.url.path == "/api/v1/artist/lookup"
    assert recorder.params["term"] == "Radiohead"

    await call(server, "lidarr_get_artist", id=42)
    assert recorder.url.path == "/api/v1/artist/42"


# --- request bodies ------------------------------------------------------------

async def test_body_sent_as_json(server, recorder):
    body = {"artistId": 1, "foreignAlbumId": "3a55b808-c5b3-4d76-b71e-9d6b8b55e31f"}
    await call(server, "lidarr_add_album", body=body)
    assert recorder.json == body


async def test_array_body_sent_as_json(server, recorder):
    body = [{"id": 1, "size": 0}]
    await call(server, "lidarr_update_quality_definitions", body=body)
    assert recorder.json == body


async def test_get_requests_have_no_body(server, recorder):
    await call(server, "lidarr_list_artist")
    assert recorder.json is None


# --- auth header ---------------------------------------------------------------

async def test_api_key_sent_as_x_api_key_header(server, recorder):
    await call(server, "lidarr_list_tag")
    assert recorder.headers["x-api-key"] == "test-key"


async def test_no_api_key_means_no_auth_header(recorder, monkeypatch):
    transport = httpx.MockTransport(recorder.handler)
    client = lidarr_mcp.build_client("http://lidarr.example.com", None, transport=transport)
    monkeypatch.setattr(lidarr_mcp, "_client", client)
    await call(lidarr_mcp.mcp, "lidarr_list_tag")
    assert "x-api-key" not in recorder.headers
    await client.aclose()


# --- ping outside /api/v1 --------------------------------------------------------

async def test_ping_hits_unversioned_path(server, recorder):
    await call(server, "lidarr_ping")
    assert recorder.method == "GET"
    assert recorder.url.path == "/ping"


# --- error paths -----------------------------------------------------------------

async def test_404_error_message_reaches_caller(server, recorder):
    recorder.response = httpx.Response(404, json={"message": "Artist not found"})
    with pytest.raises(ToolError, match="Artist not found"):
        await call(server, "lidarr_get_artist", id=999999)


async def test_401_error_surfaces_status(server, recorder):
    recorder.response = httpx.Response(401, json={"message": "Unauthorized"})
    with pytest.raises(ToolError, match="401"):
        await call(server, "lidarr_list_tag")


async def test_400_error_surfaces_status(server, recorder):
    recorder.response = httpx.Response(400, json={"message": "Invalid request"})
    with pytest.raises(ToolError, match="400"):
        await call(server, "lidarr_add_album", body={})


async def test_non_json_error_body_does_not_crash(server, recorder):
    recorder.response = httpx.Response(502, text="<html>Bad Gateway</html>")
    with pytest.raises(ToolError, match="502"):
        await call(server, "lidarr_list_tag")


# --- main() ------------------------------------------------------------------

def test_main_requires_lidarr_url(monkeypatch):
    monkeypatch.delenv("LIDARR_URL", raising=False)
    with pytest.raises(SystemExit):
        lidarr_mcp.main()
