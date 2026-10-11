"""Readable snapshots of free-form HTML API documentation.

When an API has no machine-readable spec, the agent authors one from its documentation and the
responses it records. :func:`html_to_text` turns a docs page into compact markdown-ish text —
headings as ``#``, code blocks fenced, tables as pipe rows, list items as ``-`` — and lists the
same-site pages it links to, so the agent can follow the reference without wading through markup.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString, PreformattedString
from pydantic import Field

from fauxcall.model.common import Model
from fauxcall.spec.loader import fetch_text

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx2
    from bs4.element import PageElement

_DROPPED_TAGS: Final = (
    "head",
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "nav",
    "footer",
    "button",
)
"""Never part of the readable text (links inside them are still collected)."""

_HEADINGS: Final = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "body",
        "caption",
        "center",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "header",
        "html",
        "main",
        "p",
        "section",
        "summary",
    }
)
_PERMALINK_MARKS: Final = "¶#🔗 \t"
_WHITESPACE: Final = re.compile(r"\s+")


class DocsSnapshot(Model):
    """A docs page reduced to what an agent needs to read."""

    url: str = Field(description="The page (after redirects).")
    title: str | None = None
    text: str = Field(default="", description="Markdown-ish text of the main content.")
    links: list[str] = Field(
        default_factory=list, description="Absolute same-site links, without fragments, in order."
    )


def attribute(tag: Tag, name: str) -> str | None:
    """An attribute as one string (multi-valued ones like ``rel`` joined by spaces)."""
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value


def page_title(soup: BeautifulSoup) -> str | None:
    """The page's ``<title>``, else its first ``<h1>``."""
    title = soup.title.get_text(" ", strip=True) if soup.title is not None else ""
    if title:
        return title
    heading = soup.find("h1")
    text = heading.get_text(" ", strip=True).rstrip(_PERMALINK_MARKS) if heading else ""
    return text or None


def _inline(tag: Tag) -> str:
    """The text of a tag on one line (inline code keeps its backticks)."""
    return " ".join(_Renderer().render(tag).split())


def _language(tag: Tag | None) -> str:
    if tag is None:
        return ""
    for css_class in (attribute(tag, "class") or "").split():
        for prefix in ("language-", "lang-"):
            if css_class.startswith(prefix):
                return css_class.removeprefix(prefix)
    return ""


def _fence(pre: Tag) -> str:
    code = pre.find("code")
    language = _language(pre) or _language(code)
    text = pre.get_text().strip("\n")
    marker = "~~~" if "```" in text else "```"
    return f"{marker}{language}\n{text}\n{marker}"


def _table(table: Tag) -> str:
    rows: list[list[str]] = []
    for row in table.find_all("tr"):
        cells = [
            _inline(cell).replace("|", "\\|")
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = [header, ["---"] * width, *body]
    return "\n".join("| " + " | ".join(cells) + " |" for cells in lines)


def _list(node: Tag) -> str:
    lines: list[str] = []
    for item in node.find_all("li", recursive=False):
        body = [line for line in _Renderer().render(item).split("\n") if line.strip()]
        if body:
            lines.append(f"- {body[0]}")
            lines.extend(f"  {line}" for line in body[1:])
    return "\n".join(lines)


def _quote(node: Tag) -> str:
    body = _Renderer().render(node)
    return "\n".join(f"> {line}" if line else ">" for line in body.split("\n")) if body else ""


def _heading(node: Tag) -> str:
    text = _inline(node).rstrip(_PERMALINK_MARKS)
    return f"{'#' * _HEADINGS[node.name]} {text}" if text else ""


def _rule(_node: Tag) -> str:
    return "---"


_BLOCK_RENDERERS: Final[dict[str, Callable[[Tag], str]]] = {
    **dict.fromkeys(_HEADINGS, _heading),
    "pre": _fence,
    "table": _table,
    "ul": _list,
    "ol": _list,
    "blockquote": _quote,
    "hr": _rule,
}
"""Elements rendered as a block of their own."""


class _Renderer:
    """Walks a tree, producing blocks separated by blank lines."""

    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.inline: list[str] = []

    def render(self, node: Tag) -> str:
        for child in node.children:
            self.walk(child)
        self.flush()
        return "\n\n".join(self.blocks)

    def flush(self) -> None:
        lines = [" ".join(line.split()) for line in "".join(self.inline).split("\n")]
        text = "\n".join(line for line in lines if line)
        if text:
            self.blocks.append(text)
        self.inline = []

    def block(self, text: str) -> None:
        self.flush()
        if text:
            self.blocks.append(text)

    def walk(self, node: PageElement) -> None:
        if isinstance(node, Tag):
            self.tag(node)
        elif isinstance(node, NavigableString) and not isinstance(node, PreformattedString):
            self.inline.append(_WHITESPACE.sub(" ", str(node)))  # not comments, doctypes, CDATA

    def tag(self, node: Tag) -> None:
        render = _BLOCK_RENDERERS.get(node.name)
        if render is not None:
            self.block(render(node))
        elif node.name == "br":
            self.inline.append("\n")
        elif node.name == "code":
            self.inline.append(f"`{' '.join(node.get_text().split())}`")
        else:
            is_block = node.name in _BLOCK_TAGS
            if is_block:
                self.flush()
            for child in node.children:
                self.walk(child)
            if is_block:
                self.flush()


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _links(soup: BeautifulSoup, base_url: str, page_url: str) -> list[str]:
    page = urldefrag(page_url).url
    site = _site(page_url)
    links: list[str] = []
    seen = {page}
    for anchor in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, (attribute(anchor, "href") or "").strip())).url
        if urlsplit(url).scheme not in {"http", "https"} or _site(url) != site or url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def html_to_text(html: str, base_url: str) -> DocsSnapshot:
    """Reduce a docs page to readable text and its same-site links.

    The text comes from ``<main>`` (else ``<article>``, else ``<body>``), leaving out navigation,
    footers, scripts and styles. Headings become ``#`` lines, ``<pre>`` blocks are fenced (with
    their ``language-*`` class), tables become pipe rows, list items become ``-`` lines (nested
    lists indented), inline code keeps backticks. Links are resolved against ``<base href>`` (or
    ``base_url``), kept when they stay on the same site (``www.`` ignored), and returned without
    fragments, de-duplicated, excluding the page itself.

    Args:
        html: The page.
        base_url: The page's URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    resolve_against = base_url
    base = soup.find("base", href=True)
    if base is not None:
        resolve_against = urljoin(base_url, attribute(base, "href") or "")
    title = page_title(soup)
    links = _links(soup, resolve_against, base_url)
    for tag in soup.find_all(list(_DROPPED_TAGS)):
        tag.decompose()
    text = ""
    for candidate in (soup.find("main"), soup.find("article"), soup.body, soup):
        if candidate is not None:
            text = _Renderer().render(candidate)
            if text:
                break
    return DocsSnapshot(url=base_url, title=title, text=text, links=links)


async def snapshot_docs(url: str, client: httpx2.AsyncClient) -> DocsSnapshot:
    """Fetch a docs page and reduce it with :func:`html_to_text`.

    Non-HTML documentation (plain text, markdown) is kept as-is, without links.

    Raises:
        SpecError: The page could not be fetched or answered with a non-2xx status.
    """
    document = await fetch_text(url, client)
    if document.is_html:
        return html_to_text(document.text, document.url)
    return DocsSnapshot(url=document.url, text=document.text.strip())
