"""Discovery against the real services FauxCall is validated on (run with ``-m network``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from fauxcall import __version__
from fauxcall.spec.discover import discover
from fauxcall.spec.openapi import OpenApiDocument, endpoints_from_spec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.network


@pytest.fixture
async def client() -> AsyncIterator[httpx2.AsyncClient]:
    headers = {"User-Agent": f"fauxcall/{__version__} (+https://github.com/bradnemetski/fauxcall)"}
    async with httpx2.AsyncClient(timeout=30.0, headers=headers) as http:
        yield http


@pytest.mark.parametrize(
    ("target", "kind", "spec_url"),
    [
        (
            "https://petstore3.swagger.io/",
            "swagger_ui",
            "https://petstore3.swagger.io/api/v3/openapi.json",
        ),
        (
            "https://fakerestapi.azurewebsites.net/index.html",
            "swagger_ui",
            "https://fakerestapi.azurewebsites.net/swagger/v1/swagger.json",
        ),
        (
            "https://api.frankfurter.dev/v1/openapi.json",
            "openapi",
            "https://api.frankfurter.dev/v1/openapi.json",
        ),
        (
            "https://timeapi.io/swagger/v1/swagger.json",
            "openapi",
            "https://timeapi.io/swagger/v1/swagger.json",
        ),
        ("https://httpbin.org/spec.json", "openapi", "https://httpbin.org/spec.json"),
        ("https://httpbin.org/", "swagger_ui", "https://httpbin.org/spec.json"),
        (
            "https://api.frankfurter.dev/v1",
            "openapi",
            "https://api.frankfurter.dev/v1/openapi.json",
        ),
        (
            "https://timeapi.io/swagger/index.html",
            "swagger_ui",
            "https://timeapi.io/swagger/v1/swagger.json",
        ),
    ],
)
async def test_live_specs(
    client: httpx2.AsyncClient, target: str, kind: str, spec_url: str
) -> None:
    source = await discover(target, client)
    assert (source.kind, source.spec_url) == (kind, spec_url)
    assert isinstance(source.spec, dict)
    doc = OpenApiDocument(source.spec, spec_url=source.spec_url)
    assert doc.real_target().base_url.startswith("https://")
    assert endpoints_from_spec(doc)


async def test_live_mcp_server(client: httpx2.AsyncClient) -> None:
    source = await discover("https://mcp.deepwiki.com/mcp", client)
    assert (source.kind, source.title) == ("mcp", "DeepWiki")
    assert source.mcp_transport["transport"] == "streamable_http"
