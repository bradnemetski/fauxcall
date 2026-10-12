from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from fauxcall import __version__
from fauxcall.errors import CredentialsRefusedError, SpecError
from fauxcall.spec import discover as discover_module
from fauxcall.spec.discover import (
    _conventional_bases,
    _http_url,
    _swagger_config_urls,
    discover,
    extract_spec_urls,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

FIXTURES = Path(__file__).parent / "fixtures" / "specs"
JSON = "application/json"
HTML = "text/html; charset=utf-8"
SSE = "text/event-stream"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def sse(*messages: dict[str, Any], event: str = "message") -> bytes:
    return "".join(f"event: {event}\ndata: {json.dumps(m)}\n\n" for m in messages).encode()


def initialize_result(name: str = "DeepWiki", **info: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": name, "version": "2.14.3", **info},
        },
    }


type Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeWeb:
    """A route table behind httpx2.MockTransport; unknown URLs answer 404."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx2.Request] = []

    def route(self, method: str, url: str, handler: Handler) -> None:
        self.routes[method, url] = handler

    def respond(
        self,
        url: str,
        body: str | bytes = b"",
        content_type: str | None = "text/plain",
        *,
        status: int = 200,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> None:
        all_headers = {"Content-Type": content_type} if content_type else {}
        all_headers.update(headers or {})
        content = body.encode() if isinstance(body, str) else body
        self.route(
            method, url, lambda _: httpx2.Response(status, content=content, headers=all_headers)
        )

    def file(self, url: str, name: str, content_type: str) -> None:
        self.respond(url, (FIXTURES / name).read_bytes(), content_type)

    def json(self, url: str, value: Any, *, status: int = 200, method: str = "GET") -> None:
        self.respond(url, json.dumps(value), JSON, status=status, method=method)

    def fail(self, url: str, *, method: str = "GET") -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

        self.route(method, url, handler)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, str(request.url)))
        if handler is None:
            return httpx2.Response(404, text="Not Found", headers={"Content-Type": "text/plain"})
        return handler(request)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def urls(self, method: str = "GET") -> list[str]:
        return [str(r.url) for r in self.requests if r.method == method]


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


async def discovered(web: FakeWeb, target: str) -> discover_module.DiscoveredSource:
    async with web.client() as client:
        return await discover(target, client)


async def failure(web: FakeWeb, target: str) -> str:
    with pytest.raises((SpecError, CredentialsRefusedError)) as caught:
        await discovered(web, target)
    return str(caught.value)


# ---------------------------------------------------------------------------
# extract_spec_urls
# ---------------------------------------------------------------------------


def test_extract_from_swagger_ui_pages() -> None:
    petstore = read("petstore3-index.html")
    assert extract_spec_urls(petstore, "https://petstore3.swagger.io/") == []
    initializer = read("petstore3-swagger-initializer.js")
    urls = extract_spec_urls(petstore, "https://petstore3.swagger.io/", scripts=[initializer])
    assert urls[0] == "https://petstore3.swagger.io/api/v3/openapi.json"
    assert "https://petstore31.swagger.io/api/v31/openapi.json" in urls
    assert "https://petstore.swagger.io/v2/swagger.json" not in urls  # Swagger UI's placeholder
    on_petstore = extract_spec_urls(petstore, "https://petstore.swagger.io/", scripts=[initializer])
    assert on_petstore[0] == "https://petstore.swagger.io/v2/swagger.json"
    assert extract_spec_urls(
        read("fakerestapi-index.html"), "https://fakerestapi.azurewebsites.net/index.html"
    ) == ["https://fakerestapi.azurewebsites.net/swagger/v1/swagger.json"]


def test_extract_fetched_spec_paths() -> None:
    # flasgger (httpbin) fetches the spec itself: fetch("/spec.json") then SwaggerUIBundle({spec})
    html = read("httpbin-index.html")
    assert extract_spec_urls(html, "https://httpbin.org/") == ["https://httpbin.org/spec.json"]


def test_extract_from_redoc_pages() -> None:
    assert extract_spec_urls(read("redoc-spec-url.html"), "https://api.example.com/redoc") == [
        "https://api.example.com/openapi.json"
    ]
    assert extract_spec_urls(read("redoc-init.html"), "https://docs.example.com/reference/") == [
        "https://docs.example.com/reference/specs/refs-openapi.yaml"
    ]


def test_extract_from_links_renderers_scripts_and_anchors() -> None:
    html = """<!DOCTYPE html><html><head>
      <link rel="alternate" type="application/vnd.oai.openapi+json" href="/spec/v2.json#top">
      <link rel="service-desc" href="https://api.example.com/openapi.yaml">
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
      <link rel="stylesheet" href="/site.css">
    </head><body>
      <rapi-doc spec-url="specs/rapi.json"></rapi-doc>
      <openapi-explorer spec-url="specs/explorer.json"></openapi-explorer>
      <elements-api apiDescriptionUrl="specs/elements.yaml" router="hash"></elements-api>
      <script id="api-reference" data-url="specs/scalar.json"></script>
      <script>
        const config = { url: "specs/inline.json", other: {"url": "https://cdn.example.com/x"} };
        const templated = { url: `/api/${version}/openapi.json`, alt: { url: "{{specUrl}}" } };
        const ftp = { url: "ftp://files.example.com/openapi.json" };
        const again = { url: 'specs/rapi.json' };
        const configs = [{ configUrl: "{{cfg}}" }, { configUrl: "/c.json" }];
        const twice = { configUrl: "/c.json" };
        const mirror = "https://mirror.example.net/openapi.json";
        const local = "https://docs.example.com/v1/swagger.yaml";
        const page = "https://docs.example.com/guide.html";
      </script>
      <a href="downloads/openapi.yaml">Download the spec</a>
      <a href="https://other.example.org/swagger.json">Their spec</a>
      <a href="/guide">Guide</a>
    </body></html>"""
    assert extract_spec_urls(html, "https://docs.example.com/ref/") == [
        "https://docs.example.com/ref/specs/rapi.json",
        "https://docs.example.com/ref/specs/explorer.json",
        "https://docs.example.com/ref/specs/elements.yaml",
        "https://docs.example.com/ref/specs/scalar.json",
        "https://docs.example.com/spec/v2.json",
        "https://api.example.com/openapi.yaml",
        "https://docs.example.com/ref/specs/inline.json",
        "https://cdn.example.com/x",
        "https://docs.example.com/v1/swagger.yaml",
        "https://mirror.example.net/openapi.json",
        "https://docs.example.com/ref/downloads/openapi.yaml",
        "https://other.example.org/swagger.json",
    ]


def test_extract_ignores_unrelated_urls_on_plain_pages() -> None:
    html = """<html><body><script>
      track({ url: "https://analytics.example.com/collect" });
      load({ url: "/static/openapi.json" });
    </script></body></html>"""
    assert extract_spec_urls(html, "https://www.example.com/") == [
        "https://www.example.com/static/openapi.json"
    ]


def test_extract_reads_swagger_ui_query_parameters() -> None:
    html = '<div id="swagger-ui"></div><script src="./swagger-ui-bundle.js"></script>'
    page = "https://petstore.swagger.io/?url=https://api.example.com/openapi.json&configUrl=/c"
    assert extract_spec_urls(html, page) == ["https://api.example.com/openapi.json"]
    assert extract_spec_urls("<p>no renderer</p>", page) == []


def test_swagger_config_documents() -> None:
    base = "https://app.example.com/swagger-ui/index.html"
    springdoc = {"configUrl": "/v3/api-docs/swagger-config", "url": "/v3/api-docs"}
    assert _swagger_config_urls(json.dumps(springdoc), base) == [
        "https://app.example.com/v3/api-docs"
    ]
    grouped = {
        "urls": [{"url": "/a.json"}, {"url": "/a.json"}, {"name": "x"}, "junk", {"url": " "}]
    }
    assert _swagger_config_urls(json.dumps(grouped), base) == ["https://app.example.com/a.json"]
    assert _swagger_config_urls('{"url": "https://api.example.com/users/1"}', base) == []
    assert _swagger_config_urls("[1, 2]", base) == []
    assert _swagger_config_urls("not json", base) == []


@pytest.mark.parametrize(
    ("url", "bases"),
    [
        ("https://h.example.com", ["https://h.example.com/"]),
        ("https://h.example.com/", ["https://h.example.com/"]),
        ("https://h.example.com/v1", ["https://h.example.com/v1/", "https://h.example.com/"]),
        ("https://h.example.com/docs/", ["https://h.example.com/docs/", "https://h.example.com/"]),
        (
            "https://h.example.com/a/b/index.html?x=1",
            ["https://h.example.com/a/b/", "https://h.example.com/"],
        ),
    ],
)
def test_conventional_bases(url: str, bases: list[str]) -> None:
    assert _conventional_bases(url) == bases


@pytest.mark.parametrize(
    ("target", "url"),
    [
        ("https://api.example.com/x", "https://api.example.com/x"),
        ("api.example.com/v1/openapi.json", "https://api.example.com/v1/openapi.json"),
        ("localhost:8000/openapi.json", "http://localhost:8000/openapi.json"),
        ("127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("10.0.0.5/docs", "https://10.0.0.5/docs"),
    ],
)
def test_bare_hosts_become_urls(target: str, url: str) -> None:
    assert _http_url(target) == url


@pytest.mark.parametrize(
    "target", ["openapi.yaml", "specs/openapi.yaml", "ftp://example.com/spec.json", "not a url"]
)
def test_unusable_targets(target: str) -> None:
    with pytest.raises(SpecError, match="neither an existing file nor an http"):
        _http_url(target)


# ---------------------------------------------------------------------------
# Specs and local files
# ---------------------------------------------------------------------------


async def test_local_files(web: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec_file = tmp_path / "refs.yaml"
    shutil.copy(FIXTURES / "refs-openapi.yaml", spec_file)
    source = await discovered(web, str(spec_file))
    assert (source.kind, source.url, source.title) == ("openapi", str(spec_file), "Refs Demo API")
    assert source.spec_url == spec_file.resolve().as_uri()
    assert source.spec_text == read("refs-openapi.yaml")
    assert isinstance(source.spec, dict)
    assert source.spec["openapi"] == "3.0.3"
    assert source.notes == [f"read {spec_file.resolve()}: OpenAPI 3.0.3 document"]
    assert (await discovered(web, spec_file.as_uri())).kind == "openapi"
    monkeypatch.chdir(tmp_path)
    assert (await discovered(web, " refs.yaml ")).title == "Refs Demo API"

    with pytest.raises(SpecError, match="no such file"):
        await discovered(web, (tmp_path / "missing.yaml").as_uri())
    with pytest.raises(SpecError, match="is a directory"):
        await discovered(web, str(tmp_path))
    with pytest.raises(SpecError, match="neither an existing file nor an http"):
        await discovered(web, "missing.yaml")
    with pytest.raises(SpecError, match="neither an existing file nor an http"):
        await discovered(web, "x" * 5000)  # too long to even be a file name
    (tmp_path / "notes.txt").write_text("just some notes")
    with pytest.raises(SpecError, match="not an OpenAPI/Swagger document"):
        await discovered(web, "notes.txt")
    assert web.requests == []


async def test_spec_urls(web: FakeWeb) -> None:
    petstore_url = "https://petstore3.swagger.io/api/v3/openapi.json"
    web.file(petstore_url, "petstore3-openapi.json", JSON)
    web.file("https://raw.example.com/refs.yaml", "refs-openapi.yaml", "text/plain; charset=utf-8")
    web.respond(
        "https://httpbin.org/spec",
        status=302,
        headers={"Location": "/spec.json"},
        content_type=None,
    )
    web.file("https://httpbin.org/spec.json", "httpbin-spec.json", JSON)
    web.file("https://api.frankfurter.dev/v1/openapi.json", "frankfurter-openapi.json", JSON)

    petstore = await discovered(web, petstore_url)
    assert (petstore.kind, petstore.spec_url, petstore.title) == (
        "openapi",
        petstore_url,
        "Swagger Petstore - OpenAPI 3.0",
    )
    assert petstore.spec_text == read("petstore3-openapi.json")
    assert petstore.page_text is None
    assert petstore.notes == [
        f"GET {petstore_url} -> HTTP 200 (application/json): OpenAPI 3.0.4 document"
    ]

    refs = await discovered(web, "https://raw.example.com/refs.yaml")
    assert (refs.kind, refs.title) == ("openapi", "Refs Demo API")

    httpbin = await discovered(web, "https://httpbin.org/spec")
    assert (httpbin.spec_url, httpbin.title) == ("https://httpbin.org/spec.json", "httpbin.org")
    assert httpbin.notes == [
        "GET https://httpbin.org/spec -> HTTP 200 (application/json): Swagger 2.0 document"
    ]

    bare = await discovered(web, "api.frankfurter.dev/v1/openapi.json")
    assert (bare.url, bare.spec_url) == (
        "api.frankfurter.dev/v1/openapi.json",
        "https://api.frankfurter.dev/v1/openapi.json",
    )


# ---------------------------------------------------------------------------
# Docs pages that embed a spec
# ---------------------------------------------------------------------------


async def test_petstore3_swagger_ui(web: FakeWeb) -> None:
    web.file("https://petstore3.swagger.io/", "petstore3-index.html", "text/html")
    web.file(
        "https://petstore3.swagger.io/swagger-initializer.js",
        "petstore3-swagger-initializer.js",
        "application/javascript",
    )
    web.file("https://petstore3.swagger.io/api/v3/openapi.json", "petstore3-openapi.json", JSON)
    source = await discovered(web, "https://petstore3.swagger.io/")
    assert (source.kind, source.spec_url, source.title) == (
        "swagger_ui",
        "https://petstore3.swagger.io/api/v3/openapi.json",
        "Swagger Petstore - OpenAPI 3.0",
    )
    assert source.page_text == read("petstore3-index.html")
    assert source.spec_text == read("petstore3-openapi.json")
    assert source.notes == [
        "GET https://petstore3.swagger.io/ -> HTTP 200 (text/html): Swagger UI page",
        (
            "GET https://petstore3.swagger.io/swagger-initializer.js (from a script on the page) "
            "-> HTTP 200 (application/javascript)"
        ),
        (
            "GET https://petstore3.swagger.io/api/v3/openapi.json (from a spec-like URL in "
            "swagger-initializer.js) -> HTTP 200 (application/json): OpenAPI 3.0.4 document"
        ),
    ]
    assert web.urls() == [  # the library bundles are never fetched
        "https://petstore3.swagger.io/",
        "https://petstore3.swagger.io/swagger-initializer.js",
        "https://petstore3.swagger.io/api/v3/openapi.json",
    ]


async def test_fakerestapi_swagger_ui(web: FakeWeb) -> None:
    page = "https://fakerestapi.azurewebsites.net/index.html"
    web.file(page, "fakerestapi-index.html", "text/html;charset=utf-8")
    spec_url = "https://fakerestapi.azurewebsites.net/swagger/v1/swagger.json"
    web.file(spec_url, "fakerestapi-swagger.json", "application/json;charset=utf-8")
    source = await discovered(web, page)
    assert (source.kind, source.spec_url, source.title) == (
        "swagger_ui",
        spec_url,
        "FakeRESTApi.Web V1",
    )
    assert source.notes[-1] == (
        f"GET {spec_url} (from url in an inline script) -> HTTP 200 (application/json): "
        "OpenAPI 3.0.1 document"
    )


async def test_httpbin_swagger_ui_that_fetches_its_spec(web: FakeWeb) -> None:
    web.file("https://httpbin.org/", "httpbin-index.html", HTML)
    web.file("https://httpbin.org/spec.json", "httpbin-spec.json", JSON)
    source = await discovered(web, "https://httpbin.org/")
    assert (source.kind, source.spec_url, source.title) == (
        "swagger_ui",
        "https://httpbin.org/spec.json",
        "httpbin.org",
    )
    assert source.notes[-1] == (
        "GET https://httpbin.org/spec.json (from a spec-like path in an inline script) -> "
        "HTTP 200 (application/json): Swagger 2.0 document"
    )


async def test_redoc_pages(web: FakeWeb) -> None:
    web.file("https://api.example.com/redoc", "redoc-spec-url.html", HTML)
    web.file("https://api.example.com/openapi.json", "frankfurter-openapi.json", JSON)
    web.file("https://docs.example.com/reference/", "redoc-init.html", "text/html")
    web.file(
        "https://docs.example.com/reference/specs/refs-openapi.yaml",
        "refs-openapi.yaml",
        "application/yaml",
    )
    fastapi = await discovered(web, "https://api.example.com/redoc")
    assert (fastapi.kind, fastapi.spec_url, fastapi.title) == (
        "redoc",
        "https://api.example.com/openapi.json",
        "Frankfurter API",
    )
    assert fastapi.page_text == read("redoc-spec-url.html")
    assert (
        fastapi.notes[0] == "GET https://api.example.com/redoc -> HTTP 200 (text/html): Redoc page"
    )
    init = await discovered(web, "https://docs.example.com/reference/")
    assert (init.kind, init.title) == ("redoc", "Refs Demo API")
    assert init.notes[-1] == (
        "GET https://docs.example.com/reference/specs/refs-openapi.yaml (from Redoc.init() in an "
        "inline script) -> HTTP 200 (application/yaml): OpenAPI 3.0.3 document"
    )


SPRINGDOC_PAGE = """<!DOCTYPE html><html><head><title>Swagger UI</title>
<link rel="stylesheet" href="./swagger-ui.css"></head><body><div id="swagger-ui"></div>
<script src="./swagger-ui-bundle.js"></script>
<script src="https://cdn.example.net/analytics.js"></script>
<script src="./swagger-initializer.js"></script>
<script src="./swagger-initializer.js"></script></body></html>"""
SPRINGDOC_INITIALIZER = """window.onload = function() {
  window.ui = SwaggerUIBundle({
    url: "https://petstore.swagger.io/v2/swagger.json",
    configUrl: "/v3/api-docs/swagger-config",
    dom_id: '#swagger-ui',
  });
};"""


async def test_swagger_ui_config_urls_are_followed(web: FakeWeb) -> None:
    page = "https://app.example.com/swagger-ui/index.html"
    web.respond(page, SPRINGDOC_PAGE, HTML)
    web.respond(
        "https://app.example.com/swagger-ui/swagger-initializer.js",
        SPRINGDOC_INITIALIZER,
        "application/javascript",
    )
    web.json(
        "https://app.example.com/v3/api-docs/swagger-config",
        {
            "configUrl": "/v3/api-docs/swagger-config",
            "urls": [
                {"url": "/v3/api-docs/admin", "name": "admin"},
                {"url": "/v3/api-docs/public", "name": "public"},
            ],
            "validatorUrl": "",
        },
    )
    web.json("https://app.example.com/v3/api-docs/admin", {"error": "forbidden"}, status=403)
    web.file("https://app.example.com/v3/api-docs/public", "petstore3-openapi.json", JSON)
    source = await discovered(web, page)
    assert (source.kind, source.spec_url) == (
        "swagger_ui",
        "https://app.example.com/v3/api-docs/public",
    )
    assert source.notes == [
        f"GET {page} -> HTTP 200 (text/html): Swagger UI page",
        (
            "GET https://app.example.com/swagger-ui/swagger-initializer.js (from a script on the "
            "page) -> HTTP 200 (application/javascript)"
        ),
        (
            "GET https://app.example.com/v3/api-docs/swagger-config (from configUrl in "
            "swagger-initializer.js) -> HTTP 200 (application/json): a Swagger UI configuration"
        ),
        (
            "GET https://app.example.com/v3/api-docs/admin (from the configuration at "
            "https://app.example.com/v3/api-docs/swagger-config) -> HTTP 403 (application/json)"
        ),
        (
            "GET https://app.example.com/v3/api-docs/public (from the configuration at "
            "https://app.example.com/v3/api-docs/swagger-config) -> HTTP 200 (application/json): "
            "OpenAPI 3.0.4 document"
        ),
    ]
    assert "https://petstore.swagger.io/v2/swagger.json" not in web.urls()
    assert "https://cdn.example.net/analytics.js" not in web.urls()


async def test_swagger_ui_configuration_as_input(web: FakeWeb) -> None:
    web.json("https://app.example.com/swagger-config", {"urls": [{"url": "/openapi.json"}]})
    web.file("https://app.example.com/openapi.json", "frankfurter-openapi.json", JSON)
    source = await discovered(web, "https://app.example.com/swagger-config")
    assert (source.kind, source.spec_url) == ("swagger_ui", "https://app.example.com/openapi.json")
    assert source.notes[0] == (
        "GET https://app.example.com/swagger-config -> HTTP 200 (application/json): "
        "not an OpenAPI/Swagger document"
    )


async def test_linked_spec_on_a_plain_docs_page(web: FakeWeb) -> None:
    page = (
        "<html><head><title>Widget docs</title><link rel='alternate' "
        "type='application/openapi+yaml' href='/openapi.yaml'></head><body>Docs</body></html>"
    )
    web.respond("https://docs.example.com/", page, HTML)
    web.file("https://docs.example.com/openapi.yaml", "refs-openapi.yaml", "application/yaml")
    source = await discovered(web, "https://docs.example.com/")
    assert (source.kind, source.title, source.page_text) == ("openapi", "Refs Demo API", page)
    assert source.notes[0] == "GET https://docs.example.com/ -> HTTP 200 (text/html): HTML page"


async def test_candidates_that_fail_are_skipped(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = """<html><body><redoc spec-url="/a.json"></redoc><script>
      Redoc.init("/b.json"); Redoc.init("/c.json"); Redoc.init("/d.json"); Redoc.init("/e.json");
    </script></body></html>"""
    web.respond("https://api.example.com/docs/", page, HTML)
    web.fail("https://api.example.com/a.json")
    web.respond("https://api.example.com/b.json", "<html>nope</html>", HTML)
    web.file("https://api.example.com/e.json", "frankfurter-openapi.json", JSON)
    monkeypatch.setattr(discover_module, "MAX_CANDIDATES", 3)
    source = await discovered(web, "https://api.example.com/docs/")
    assert source.kind == "html_docs"  # no spec within budget: the page itself is the source
    assert source.notes[1:4] == [
        (
            "GET https://api.example.com/a.json (from <redoc spec-url>) failed: "
            "ConnectError: connection refused"
        ),
        (
            "GET https://api.example.com/b.json (from Redoc.init() in an inline script) -> "
            "HTTP 200 (text/html): not an OpenAPI/Swagger document"
        ),
        (
            "GET https://api.example.com/c.json (from Redoc.init() in an inline script) -> "
            "HTTP 404 (text/plain)"
        ),
    ]
    assert "https://api.example.com/e.json" not in web.urls()  # the budget ran out first
    assert source.notes[-1] == "no spec found: treating the page as free-form documentation"


# ---------------------------------------------------------------------------
# Conventional locations, free-form docs and failures
# ---------------------------------------------------------------------------


async def test_conventional_locations(web: FakeWeb) -> None:
    computed = """<html><head><title>API</title></head><body><div id="swagger-ui"></div>
    <script src="/swagger-ui-bundle.js"></script>
    <script>SwaggerUIBundle({ url: window.location.origin + specPath });</script></body></html>"""
    web.respond("https://computed.example.com/docs/index.html", computed, HTML)
    web.file("https://computed.example.com/v3/api-docs", "petstore3-openapi.json", JSON)
    source = await discovered(web, "https://computed.example.com/docs/index.html")
    assert (source.kind, source.spec_url) == (
        "swagger_ui",
        "https://computed.example.com/v3/api-docs",
    )
    assert source.notes[-1] == (
        "GET https://computed.example.com/v3/api-docs -> HTTP 200 (application/json): "
        "OpenAPI 3.0.4 document (a conventional location)"
    )
    assert web.urls()[1:5] == [
        "https://computed.example.com/docs/openapi.json",
        "https://computed.example.com/docs/swagger.json",
        "https://computed.example.com/docs/v3/api-docs",
        "https://computed.example.com/docs/openapi.yaml",
    ]

    index = {"version": "v1", "openapi": "/v1/openapi.json", "docs": "https://frankfurter.dev/"}
    web.json("https://api.frankfurter.dev/v1", index)  # links its spec; is not one
    web.file("https://api.frankfurter.dev/v1/openapi.json", "frankfurter-openapi.json", JSON)
    root = await discovered(web, "https://api.frankfurter.dev/v1")
    assert (root.kind, root.spec_url) == ("openapi", "https://api.frankfurter.dev/v1/openapi.json")

    web.respond("https://typo.example.com/api/v3/openapi.jsn", "nope", status=404)
    web.file("https://typo.example.com/api/v3/openapi.json", "petstore3-openapi.json", JSON)
    typo = await discovered(web, "https://typo.example.com/api/v3/openapi.jsn")
    assert (typo.kind, typo.spec_url) == ("openapi", "https://typo.example.com/api/v3/openapi.json")
    assert typo.notes[:2] == [
        "GET https://typo.example.com/api/v3/openapi.jsn -> HTTP 404 (text/plain): nope",
        "POST https://typo.example.com/api/v3/openapi.jsn (MCP initialize) -> HTTP 404: Not Found",
    ]


async def test_free_form_docs(web: FakeWeb) -> None:
    web.file("https://docs.example.com/api/", "docs-page.html", "text/html")
    source = await discovered(web, "https://docs.example.com/api/")
    assert (source.kind, source.title, source.spec_url) == (
        "html_docs",
        "Widgets API Reference",
        None,
    )
    assert source.page_text == read("docs-page.html")
    assert source.notes == [
        "GET https://docs.example.com/api/ -> HTTP 200 (text/html): HTML page",
        (
            "no spec at the conventional locations (openapi.json, swagger.json, v3/api-docs, "
            "openapi.yaml, swagger/v1/swagger.json, api-docs) under https://docs.example.com/api/ "
            "or https://docs.example.com/"
        ),
        "no spec found: treating the page as free-form documentation",
    ]
    assert len(web.requests) == 13

    web.respond("https://docs.example.com/llms.txt", "# Widgets\n\nGET /widgets lists widgets.")
    text = await discovered(web, "https://docs.example.com/llms.txt")
    assert (text.kind, text.page_text, text.title) == (
        "html_docs",
        "# Widgets\n\nGET /widgets lists widgets.",
        None,
    )
    assert text.notes[-1] == "no spec found: treating the text as free-form documentation"


async def test_failures_list_everything_tried(web: FakeWeb) -> None:
    web.respond("https://nothing.example.com/x", "Not here", status=404)
    message = await failure(web, "https://nothing.example.com/x")
    assert message == (
        "could not find an OpenAPI/Swagger spec, docs page or MCP server at "
        "'https://nothing.example.com/x'.\nTried:\n"
        "  - GET https://nothing.example.com/x -> HTTP 404 (text/plain): Not here\n"
        "  - POST https://nothing.example.com/x (MCP initialize) -> HTTP 404: Not Found\n"
        "  - no spec at the conventional locations (openapi.json, swagger.json, v3/api-docs, "
        "openapi.yaml, swagger/v1/swagger.json, api-docs) under https://nothing.example.com/x/ "
        "or https://nothing.example.com/\n"
        "Pass the spec itself (a JSON/YAML file or URL), its Swagger UI or Redoc page, a docs "
        "page, or an MCP endpoint (usually ending in /mcp)."
    )

    web.json("https://api.example.com/users", [{"id": 1}])
    assert "https://api.example.com/users -> HTTP 200 (application/json): not an OpenAPI" in (
        await failure(web, "https://api.example.com/users")
    )
    web.respond("https://img.example.com/logo.png", b"\x89PNG", "image/png")
    assert "(image/png): not an OpenAPI/Swagger document" in await failure(
        web, "https://img.example.com/logo.png"
    )

    web.fail("https://down.example.com/")
    web.fail("https://down.example.com/", method="POST")
    message = await failure(web, "https://down.example.com/")
    assert "  - GET https://down.example.com/ failed: ConnectError: connection refused\n" in message
    assert (
        "  - POST https://down.example.com/ (MCP initialize) failed: ConnectError: connection "
        "refused\n"
    ) in message
    assert "conventional" not in message

    web.respond("https://flaky.example.com/api", "gone", status=500)
    web.fail("https://flaky.example.com/api/openapi.json")
    assert (
        "stopped probing conventional spec locations: GET https://flaky.example.com/api/"
        "openapi.json failed: ConnectError: connection refused"
    ) in await failure(web, "https://flaky.example.com/api")


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def test_streamable_http_mcp_with_sse_responses(web: FakeWeb) -> None:
    url = "https://mcp.deepwiki.com/mcp"
    web.json(
        url,
        {
            "jsonrpc": "2.0",
            "id": "server-error",
            "error": {"code": -32600, "message": "Not Acceptable: Client must accept SSE"},
        },
        status=406,
    )
    received: list[Any] = []

    def initialize(request: httpx2.Request) -> httpx2.Response:
        received.append(json.loads(request.content))
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["content-type"] == "application/json"
        events = sse(
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}},
            {"jsonrpc": "2.0", "id": 99, "result": {}},
            initialize_result(),
        )
        return httpx2.Response(200, headers={"Content-Type": SSE}, content=b": ping\n\n" + events)

    web.route("POST", url, initialize)
    source = await discovered(web, url)
    assert received == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fauxcall", "version": __version__},
            },
        }
    ]
    assert (source.kind, source.title, source.spec) == ("mcp", "DeepWiki", None)
    assert source.mcp_initialize == initialize_result()
    assert source.mcp_transport == {
        "transport": "streamable_http",
        "endpoint": url,
        "content_type": SSE,
        "response_mode": "sse",
    }
    assert source.notes == [
        (
            f'GET {url} -> HTTP 406 (application/json): {{"jsonrpc": "2.0", "id": "server-error", '
            '"error": {"code": -32600, "message": "Not Acceptable: Client must accept SSE"}}'
        ),
        (
            "MCP server 'DeepWiki' 2.14.3 answered initialize (protocol 2025-06-18) over "
            "streamable_http"
        ),
    ]


async def test_streamable_http_mcp_with_json_responses_and_sessions(web: FakeWeb) -> None:
    url = "https://weather.example.com/mcp"
    web.respond(url, "Method Not Allowed", status=405)
    batch = json.dumps([initialize_result("weather", title="Weather Server")])
    web.respond(url, batch, JSON, method="POST", headers={"Mcp-Session-Id": "abc123"})
    source = await discovered(web, url)
    assert (source.kind, source.title) == ("mcp", "Weather Server")
    assert source.mcp_transport == {
        "transport": "streamable_http",
        "endpoint": url,
        "content_type": JSON,
        "response_mode": "json",
        "session_header": "Mcp-Session-Id",
        "session_id": "abc123",
    }


async def test_mcp_detected_from_json_rpc_or_paths(web: FakeWeb) -> None:
    rpc = "https://rpc.example.com/"
    web.json(rpc, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "POST only"}})
    web.json(rpc, initialize_result("rpc"), method="POST")
    assert (await discovered(web, rpc)).title == "rpc"

    landing = "https://tools.example.com/mcp"
    web.respond(
        landing, "<html><head><title>Connect</title></head><body>Use POST</body></html>", HTML
    )
    web.json(landing, initialize_result("tools"), method="POST")
    source = await discovered(web, landing)
    assert (source.kind, source.title) == ("mcp", "tools")
    assert "no spec at the conventional locations" in source.notes[1]

    plain = "https://plain.example.com/mcp/"
    web.json(plain, {"status": "ok"})
    web.json(plain, initialize_result("plain"), method="POST")
    assert (await discovered(web, plain)).title == "plain"


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (
            httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
            ),
            "-> HTTP 200 (application/json): JSON-RPC error -32601: nope",
        ),
        (
            httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}}
            ),
            "-> HTTP 200 (application/json): the initialize result has no serverInfo",
        ),
        (
            httpx2.Response(200, text="OK"),
            "-> HTTP 200 (text/plain): no JSON-RPC response to initialize",
        ),
        (
            httpx2.Response(200, headers={"Content-Type": SSE}, content=b"data: not json\n\n"),
            "-> HTTP 200 (text/event-stream): no JSON-RPC response to initialize",
        ),
        (
            httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://a.example.com/.well-known'
                    '/oauth-protected-resource"'
                },
            ),
            (
                "-> HTTP 401 — authorization required (WWW-Authenticate: Bearer "
                'resource_metadata="https://a.example.com/.well-known/oauth-protected-resource")'
            ),
        ),
        (httpx2.Response(403, text="denied"), "-> HTTP 403: denied"),
    ],
)
async def test_mcp_probe_failures(web: FakeWeb, response: httpx2.Response, problem: str) -> None:
    url = "https://broken.example.com/mcp"
    web.respond(url, "Method Not Allowed", status=405)
    web.route("POST", url, lambda _: response)
    assert f"  - POST {url} (MCP initialize) {problem}\n" in await failure(web, url)


@pytest.mark.parametrize("status", [401, 403])
async def test_an_mcp_endpoint_refusing_the_credentials_says_so(web: FakeWeb, status: int) -> None:
    url = "https://locked.example.com/mcp"
    web.respond(url, "Method Not Allowed", status=405)
    web.route("POST", url, lambda _: httpx2.Response(status))
    with pytest.raises(CredentialsRefusedError) as caught:
        await discovered(web, url)
    assert str(caught.value).startswith(
        f"{url} refused the credentials: HTTP {status}. If it needs a key or token, put it in "
        "the fake's .env as FAUXCALL_REAL__HEADERS"
    )


async def test_mcp_event_streams_are_bounded(web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover_module, "MAX_SSE_EVENTS", 2)
    url = "https://chatty.example.com/mcp"
    pings = sse(*[{"jsonrpc": "2.0", "method": "ping"}] * 3, initialize_result())
    web.respond(url, pings, SSE, method="POST")
    assert "no JSON-RPC response to initialize" in await failure(web, url)


async def test_mcp_probe_timeouts(web: FakeWeb) -> None:
    url = "https://slow.example.com/mcp"

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    web.route("POST", url, timeout)
    assert f"  - POST {url} (MCP initialize) failed: ReadTimeout: timed out\n" in await failure(
        web, url
    )


class LegacyStream(httpx2.AsyncByteStream):
    """An SSE stream that announces an endpoint, then relays what the POST handler queues."""

    def __init__(self, first: bytes, queue: asyncio.Queue[bytes | None]) -> None:
        self.first = first
        self.queue = queue

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        while (chunk := await self.queue.get()) is not None:
            yield chunk


def legacy_server(
    web: FakeWeb,
    url: str,
    *,
    endpoint_event: bytes = b"event: endpoint\ndata: /messages/?session_id=s1\n\n",
    replies: list[bytes] | None = None,
    post_status: int = 202,
    closed: bool = False,
) -> None:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    if closed:  # the stream ends right after the first chunk
        queue.put_nowait(None)
    web.route(
        "GET",
        url,
        lambda _: httpx2.Response(
            200,
            headers={"Content-Type": SSE},
            stream=LegacyStream(b": hi\n\n" + endpoint_event, queue),
        ),
    )
    web.respond(url, "Method Not Allowed", status=405, method="POST")

    def post(request: httpx2.Request) -> httpx2.Response:
        assert json.loads(request.content)["method"] == "initialize"
        for reply in replies if replies is not None else [sse(initialize_result("legacy"))]:
            queue.put_nowait(reply)
        queue.put_nowait(None)
        return httpx2.Response(post_status, text="Accepted")

    web.route("POST", "https://legacy.example.com/messages/?session_id=s1", post)


async def test_legacy_sse_transport(web: FakeWeb) -> None:
    url = "https://legacy.example.com/sse"
    legacy_server(
        web,
        url,
        replies=[
            sse({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}),
            sse(initialize_result("legacy")),
        ],
    )
    source = await discovered(web, url)
    assert (source.kind, source.title) == ("mcp", "legacy")
    assert source.mcp_transport == {
        "transport": "sse",
        "endpoint": "https://legacy.example.com/messages/?session_id=s1",
        "content_type": SSE,
        "response_mode": "sse",
    }
    assert source.notes == [
        f"GET {url} -> HTTP 200 (text/event-stream)",
        f"POST {url} (MCP initialize) -> HTTP 405: Method Not Allowed",
        "MCP server 'legacy' 2.14.3 answered initialize (protocol 2025-06-18) over sse",
    ]


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        (
            {"endpoint_event": b"event: message\ndata: {}\n\n", "closed": True},
            "no 'endpoint' event on the stream",
        ),
        ({"replies": []}, "no initialize response on the stream"),
        (
            {
                "replies": [
                    sse(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "error": {"code": -32602, "message": "bad version"},
                        }
                    )
                ]
            },
            "JSON-RPC error -32602: bad version",
        ),
    ],
)
async def test_legacy_sse_failures(web: FakeWeb, options: dict[str, Any], problem: str) -> None:
    url = "https://legacy.example.com/sse"
    legacy_server(web, url, **options)
    assert f"  - GET {url} (MCP SSE transport): {problem}\n" in await failure(web, url)


async def test_mcp_handshakes_have_a_deadline(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discover_module, "MCP_HANDSHAKE_SECONDS", 0.05)
    silent = "https://legacy.example.com/sse"
    legacy_server(web, silent, endpoint_event=b"event: message\ndata: {}\n\n")
    assert f"  - GET {silent} (MCP SSE transport): no answer within 0.05 seconds\n" in (
        await failure(web, silent)
    )

    hanging = "https://hanging.example.com/mcp"
    never: asyncio.Queue[bytes | None] = asyncio.Queue()
    web.route(
        "POST",
        hanging,
        lambda _: httpx2.Response(
            200, headers={"Content-Type": SSE}, stream=LegacyStream(b": wait\n\n", never)
        ),
    )
    assert f"  - POST {hanging} (MCP initialize): no answer within 0.05 seconds\n" in (
        await failure(web, hanging)
    )


async def test_legacy_sse_post_rejected(web: FakeWeb) -> None:
    url = "https://legacy.example.com/sse"
    legacy_server(web, url, post_status=500)
    assert (
        "  - POST https://legacy.example.com/messages/?session_id=s1 (MCP initialize) -> HTTP 500\n"
    ) in await failure(web, url)


async def test_legacy_sse_stream_is_bounded(web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover_module, "MAX_SSE_EVENTS", 2)
    url = "https://legacy.example.com/sse"
    legacy_server(web, url, endpoint_event=b"event: ping\ndata: 1\n\n" * 3)
    assert "(MCP SSE transport): no 'endpoint' event on the stream" in await failure(web, url)


async def test_deprecated_and_unreachable_sse_endpoints(web: FakeWeb) -> None:
    deprecated = "https://mcp.deepwiki.com/sse"
    gone = {"error": "SSE transport is deprecated", "endpoint": "/mcp"}
    web.json(deprecated, gone, status=410)
    web.respond(deprecated, "Method Not Allowed", status=405, method="POST")
    message = await failure(web, deprecated)
    assert f"  - GET {deprecated} -> HTTP 410 (application/json): " in message
    assert "SSE transport is deprecated" in message
    assert f"  - GET {deprecated} (MCP SSE transport) -> HTTP 410 (application/json)\n" in message

    unreachable = "https://gone.example.com/sse"
    web.fail(unreachable)
    web.fail(unreachable, method="POST")
    message = await failure(web, unreachable)
    assert f"  - GET {unreachable} (MCP SSE transport) failed: ConnectError" in message


async def test_inline_candidates_are_not_refetched_after_scripts(web: FakeWeb) -> None:
    page = """<html><body><div id="swagger-ui"></div>
    <script>window.ui = SwaggerUIBundle({ url: "/missing.json" });</script>
    <script src="init.js"></script></body></html>"""
    web.respond("https://ui.example.com/docs/", page, HTML)
    web.respond("https://ui.example.com/docs/init.js", 'const spec = { url: "/openapi.json" };')
    web.file("https://ui.example.com/openapi.json", "frankfurter-openapi.json", JSON)
    source = await discovered(web, "https://ui.example.com/docs/")
    assert (source.kind, source.spec_url) == ("swagger_ui", "https://ui.example.com/openapi.json")
    assert web.urls().count("https://ui.example.com/missing.json") == 1


async def test_pages_without_a_findable_spec_are_docs(web: FakeWeb) -> None:
    page = """<html><head><title>API console</title></head><body><div id="swagger-ui"></div>
    <script src="./swagger-initializer.js"></script></body></html>"""
    web.respond("https://console.example.com/", page, HTML)
    source = await discovered(web, "https://console.example.com/")
    assert (source.kind, source.title) == ("html_docs", "API console")
    assert source.notes[1] == (
        "GET https://console.example.com/swagger-initializer.js (from a script on the page) -> "
        "HTTP 404 (text/plain)"
    )

    landing = "https://tools.example.com/mcp"
    web.respond(landing, "<html><body><h1>Tools</h1></body></html>", HTML)
    web.respond(landing, "nope", status=400, method="POST")
    docs = await discovered(web, landing)
    assert (docs.kind, docs.title) == ("html_docs", "Tools")
    assert f"POST {landing} (MCP initialize) -> HTTP 400: nope" in docs.notes


async def test_json_rpc_answers_that_are_not_mcp(web: FakeWeb) -> None:
    url = "https://rpc.example.com/"
    web.json(url, {"jsonrpc": "2.0", "result": "pong"})
    message = await failure(web, url)
    assert f"  - POST {url} (MCP initialize) -> HTTP 404: Not Found\n" in message
    assert "no spec at the conventional locations" in message


async def test_responses_without_a_content_type(web: FakeWeb) -> None:
    web.respond("https://bare.example.com/", b"", None, status=500)
    assert "  - GET https://bare.example.com/ -> HTTP 500\n" in await failure(
        web, "https://bare.example.com/"
    )
