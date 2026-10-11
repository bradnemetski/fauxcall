from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from bs4 import BeautifulSoup

from fauxcall.errors import SpecError
from fauxcall.spec.docs import DocsSnapshot, html_to_text, page_title, snapshot_docs

FIXTURES = Path(__file__).parent / "fixtures" / "specs"

DOCS_TEXT = """# Widgets API

The Widgets API lets you create and list widgets. Base URL: `https://api.example.com/v1`.

## Authentication

Send your key in the `X-Api-Key` header.
Keys never expire.

## List widgets

```bash
curl https://api.example.com/v1/widgets \\
  -H "X-Api-Key: $KEY"
```

### Query parameters

| Name | Type | Description |
| --- | --- | --- |
| `limit` | integer | Max items (1-100) |
| `color` | string | Filter by color \\| name |

### Errors

- `401` - missing key
- `429` - rate limited
  - Retry after `Retry-After` seconds

- First
- Second

> Widgets are eventually consistent.

color

One of red, green, blue.

---

See Create a widget, errors, changelog, GitHub, email us, nothing, this page."""


def test_docs_page_to_text() -> None:
    html = (FIXTURES / "docs-page.html").read_text(encoding="utf-8")
    snapshot = html_to_text(html, "https://docs.example.com/api/")
    assert snapshot.url == "https://docs.example.com/api/"
    assert snapshot.title == "Widgets API Reference"
    assert snapshot.text == DOCS_TEXT
    assert snapshot.links == [
        "https://docs.example.com/",
        "https://docs.example.com/api/guides/auth",
        "https://docs.example.com/api/widgets",
        "https://docs.example.com/api/errors",
        "https://www.docs.example.com/api/changelog",
        "https://docs.example.com/terms",
    ]


def test_base_href_and_content_fallbacks() -> None:
    html = """<html><head><base href="/v2/"><title> </title></head><body>
      <main>   </main>
      <article><h1>Guide <a href="#guide">#</a></h1><p>Read <a href="next">on</a>.</p></article>
    </body></html>"""
    snapshot = html_to_text(html, "https://docs.example.com/a/b")
    assert snapshot.title == "Guide"
    assert snapshot.text == "# Guide\n\nRead on."
    # like a browser, fragment-only links resolve against <base href>, not the page
    assert snapshot.links == ["https://docs.example.com/v2/", "https://docs.example.com/v2/next"]

    assert html_to_text("<p>Just <b>a</b> fragment</p>", "https://x.example.com/").text == (
        "Just a fragment"
    )
    body_only = html_to_text("<html><body><div>Body text</div></body></html>", "https://x/")
    assert (body_only.title, body_only.text) == (None, "Body text")
    assert html_to_text("", "https://x.example.com/") == DocsSnapshot(url="https://x.example.com/")


def test_block_rendering_details() -> None:
    html = """<main>
      <h2>¶</h2>
      <h3>Shapes <span>¶</span></h3>
      <pre class="lang-json">{"a": 1}</pre>
      <pre><code>```nested fence```</code></pre>
      <pre><code class="highlight">plain</code></pre>
      <table></table>
      <table><tr><td>a</td><td>b</td><td>c</td></tr><tr></tr><tr><td>1</td></tr></table>
      <ul><li></li><li>One<pre>x = 1</pre></li></ul>
      <blockquote></blockquote>
      <blockquote><p>First</p><p>Second</p></blockquote>
      <p>Line<!-- hidden --> one<br>line two</p>
    </main>"""
    assert html_to_text(html, "https://x.example.com/").text == (
        "### Shapes\n\n"
        '```json\n{"a": 1}\n```\n\n'
        "~~~\n```nested fence```\n~~~\n\n"
        "```\nplain\n```\n\n"
        "| a | b | c |\n| --- | --- | --- |\n| 1 |  |  |\n\n"
        "- One\n  ```\n  x = 1\n  ```\n\n"
        "> First\n>\n> Second\n\n"
        "Line one\nline two"
    )


def test_page_title() -> None:
    assert page_title(BeautifulSoup("<title>T</title><h1>H</h1>", "html.parser")) == "T"
    assert page_title(BeautifulSoup("<h1> Heading <em>one</em> </h1>", "html.parser")) == (
        "Heading one"
    )
    assert page_title(BeautifulSoup("<h1> </h1>", "html.parser")) is None
    assert page_title(BeautifulSoup("<p>none</p>", "html.parser")) is None


async def test_snapshot_docs() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/docs":
            return httpx2.Response(301, headers={"Location": "/docs/"})
        if request.url.path == "/docs/":
            return httpx2.Response(
                200,
                content=(FIXTURES / "docs-page.html").read_bytes(),
                headers={"Content-Type": "text/html"},
            )
        if request.url.path == "/llms.txt":
            return httpx2.Response(200, text="  # Widgets\n\nGET /widgets  \n")
        return httpx2.Response(404, text="missing")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        snapshot = await snapshot_docs("https://docs.example.com/docs", client)
        assert snapshot.url == "https://docs.example.com/docs/"
        assert snapshot.text == DOCS_TEXT
        assert "https://docs.example.com/docs/widgets" in snapshot.links
        text = await snapshot_docs("https://docs.example.com/llms.txt", client)
        assert text == DocsSnapshot(
            url="https://docs.example.com/llms.txt", text="# Widgets\n\nGET /widgets"
        )
        with pytest.raises(SpecError, match="returned HTTP 404: missing"):
            await snapshot_docs("https://docs.example.com/gone", client)
