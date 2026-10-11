from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest

from fauxcall.errors import SpecError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.spec import loader
from fauxcall.spec.loader import (
    FetchedDocument,
    describe_error,
    excerpt,
    fetch_document,
    fetch_text,
    media_type,
    parse_spec_text,
    read_body,
    read_local,
)

FIXTURES = Path(__file__).parent / "fixtures" / "specs"


def _client(handler: httpx2.MockTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=handler)


def dig(value: Any, *keys: str | int) -> Any:
    for key in keys:
        value = value[key]
    return value


# ---------------------------------------------------------------------------
# parse_spec_text
# ---------------------------------------------------------------------------


def test_json_specs_keep_float_spellings() -> None:
    spec = parse_spec_text((FIXTURES / "frankfurter-openapi.json").read_text())
    assert spec["openapi"] == "3.1.2"
    multiple_of = dig(spec, "components", "schemas", "rates", "additionalProperties", "multipleOf")
    assert isinstance(multiple_of, JsonFloat)
    assert multiple_of.lexeme == "0.00001"  # as written, not re-spelled as 1e-05


def test_yaml_specs_get_json_compatible_scalars() -> None:
    spec = parse_spec_text((FIXTURES / "refs-openapi.yaml").read_text(), source="refs.yaml")
    things = dig(spec, "paths", "/things")
    assert list(things["get"]["responses"]) == ["200", "default"]
    new_thing = dig(spec, "components", "schemas", "NewThing", "properties")
    assert "on" in new_thing  # not the boolean True
    assert new_thing["created"]["example"] == "2024-01-15"  # not a date object
    assert things["get"]["parameters"][1]["explode"] is False


def test_yaml_quirks_are_normalized() -> None:
    text = (
        "\ufeffswagger: 2.0\n"
        "answers: [yes, no, on, off, true, False]\n"
        "when: 2024-01-15T10:00:00Z\n"
        "stamp: !!timestamp 2024-01-15 10:00:00\n"
        "limits: {low: -.inf, odd: .nan, hex: 0x1F}\n"
        "keys: {200: a, 1.5: b, ~: c, true: d, false: e}\n"
        "tags: !!set {b, a, c}\n"
    )
    spec = parse_spec_text(text)
    assert spec["swagger"] == 2.0
    assert parse_spec_text('{"openapi": "3.1.0-rc1"}')["openapi"] == "3.1.0-rc1"
    assert spec["answers"] == ["yes", "no", "on", "off", True, False]
    assert spec["when"] == "2024-01-15T10:00:00Z"
    assert spec["stamp"] == "2024-01-15 10:00:00"
    assert spec["limits"] == {"low": "-inf", "odd": "nan", "hex": 31}
    assert spec["keys"] == {"200": "a", "1.5": "b", "null": "c", "true": "d", "false": "e"}
    assert spec["tags"] == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "is empty"),
        ("  \n", "is empty"),
        ("[1, 2]", "expected a mapping, got a list"),
        ('"just a string"', "got a string"),
        ("null", "got null"),
        ("42", "got a int"),
        ("<!DOCTYPE html><html><body>docs</body></html>", "it looks like HTML or XML"),
        ('{"info": {"title": "x"}}', "no 'openapi' or 'swagger' field"),
        (
            '{"version": "v1", "openapi": "/v1/openapi.json"}',
            "its 'openapi' field is '/v1/openapi.json', not a version",
        ),
        ("swagger: true", "its 'swagger' field is True, not a version"),
        ("openapi: [unclosed", "neither valid JSON nor valid YAML"),
        ("a: 1\n---\nb: 2\n", "neither valid JSON nor valid YAML"),
        ("[" * 100_000, "nested too deeply"),
    ],
)
def test_invalid_documents_are_refused(text: str, message: str) -> None:
    with pytest.raises(SpecError, match=r"spec\.yaml") as caught:
        parse_spec_text(text, source="spec.yaml")
    assert message in str(caught.value)


def test_yaml_alias_bombs_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "MAX_YAML_NODES", 500)
    bomb = (
        "openapi: 3.0.0\n"
        "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
        "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
    )
    with pytest.raises(SpecError, match="alias bomb"):
        parse_spec_text(bomb)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_media_type_excerpt_and_errors() -> None:
    assert media_type("Text/HTML; charset=utf-8") == "text/html"
    assert media_type(None) == ""
    assert excerpt("a\n  b\tc") == "a b c"
    assert excerpt("x" * 300, limit=10) == "x" * 9 + "…"
    assert describe_error(httpx2.ConnectError("refused")) == "ConnectError: refused"
    assert describe_error(httpx2.ReadTimeout("")) == "ReadTimeout"


def test_fetched_document_properties() -> None:
    page = FetchedDocument(url="https://x/", status=200, content_type="text/html; charset=utf-8")
    assert page.media_type == "text/html"
    assert page.is_success
    assert page.is_html
    sniffed = FetchedDocument(url="https://x/", status=404, text="<!-- c -->\n<html><body>")
    assert sniffed.media_type == ""
    assert not sniffed.is_success
    assert sniffed.is_html
    assert not FetchedDocument(url="https://x/", status=200, text="<rss></rss>").is_html
    assert not FetchedDocument(url="https://x/", status=200, text='{"a": 1}').is_html


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def test_fetch_text_follows_redirects_and_decodes() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/old":
            return httpx2.Response(301, headers={"Location": "/spec.json"})
        body = '\ufeff{"openapi": "3.0.0", "info": {"title": "Café"}}'
        return httpx2.Response(
            200,
            content=body.encode("latin-1", errors="ignore"),
            headers={"Content-Type": "application/json; charset=latin-1"},
        )

    async with _client(httpx2.MockTransport(handler)) as client:
        document = await fetch_text("https://api.example.com/old", client)
    assert document.url == "https://api.example.com/spec.json"
    assert document.status == 200
    assert document.content_type == "application/json; charset=latin-1"
    assert document.text.startswith('{"openapi"')
    assert "Café" in document.text
    assert seen[0].headers["accept"].startswith("application/json")


async def test_fetch_text_raises_for_error_statuses() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/empty":
            return httpx2.Response(500)
        return httpx2.Response(404, text="  Not\n Found  ")

    async with _client(httpx2.MockTransport(handler)) as client:
        with pytest.raises(SpecError, match=r"returned HTTP 404: Not Found$"):
            await fetch_text("https://api.example.com/missing", client)
        with pytest.raises(SpecError, match=r"returned HTTP 500$"):
            await fetch_text("https://api.example.com/empty", client)
        document = await fetch_document("https://api.example.com/missing", client)
    assert document.status == 404


async def test_fetch_wraps_transport_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    async with _client(httpx2.MockTransport(handler)) as client:
        with pytest.raises(
            SpecError, match=r"GET https://down\.example\.com/ failed: ConnectError"
        ):
            await fetch_text("https://down.example.com/", client)
        with pytest.raises(SpecError, match="failed"):
            await fetch_text("https://exa mple.com:99999/", client)


async def test_event_streams_are_not_read() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=b"data: x\n\n"
        )

    async with _client(httpx2.MockTransport(handler)) as client:
        document = await fetch_document("https://mcp.example.com/sse", client)
    assert document.media_type == "text/event-stream"
    assert document.text == ""


async def test_read_body_limits() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"0123456789" * 3)

    async with _client(httpx2.MockTransport(handler)) as client:
        async with client.stream("GET", "https://x.example.com/") as response:
            assert await read_body(response, limit=15, truncate=True) == "012345678901234"
        async with client.stream("GET", "https://x.example.com/") as response:
            with pytest.raises(SpecError, match="larger than 15 bytes"):
                await read_body(response, limit=15)
        async with client.stream("GET", "https://x.example.com/") as response:
            assert len(await read_body(response, limit=30)) == 30


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------


def test_read_local(tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_bytes("\ufeffopenapi: 3.0.0\n".encode())
    assert read_local(spec) == "openapi: 3.0.0\n"
    with pytest.raises(SpecError, match="cannot read"):
        read_local(tmp_path / "missing.yaml")
    binary = tmp_path / "spec.bin"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SpecError, match="cannot read"):
        read_local(binary)
