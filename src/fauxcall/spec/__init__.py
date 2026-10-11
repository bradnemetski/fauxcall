"""Understand what ``/fauxcall <url>`` was given, and turn API specs into model skeletons.

* :mod:`~fauxcall.spec.loader` — fetch documents, read files, parse JSON/YAML specs.
* :mod:`~fauxcall.spec.discover` — tell a spec from a Swagger UI/Redoc page, free-form docs or an
  MCP server, and find the spec behind a page. Import the function from its module
  (``from fauxcall.spec.discover import discover``): re-exporting it here would shadow the module.
* :mod:`~fauxcall.spec.openapi` — OpenAPI 2.0 / 3.0 / 3.1 → endpoints, auth, role hints, summary.
* :mod:`~fauxcall.spec.docs` — readable snapshots of HTML documentation.
"""

from fauxcall.spec.discover import DiscoveredSource, SourceKind, extract_spec_urls
from fauxcall.spec.docs import DocsSnapshot, html_to_text, snapshot_docs
from fauxcall.spec.loader import FetchedDocument, fetch_text, parse_spec_text, read_local
from fauxcall.spec.openapi import (
    OpenApiDocument,
    Operation,
    auth_from_spec,
    endpoints_from_spec,
    suggest_role,
    summarize,
)

__all__ = [
    "DiscoveredSource",
    "DocsSnapshot",
    "FetchedDocument",
    "OpenApiDocument",
    "Operation",
    "SourceKind",
    "auth_from_spec",
    "endpoints_from_spec",
    "extract_spec_urls",
    "fetch_text",
    "html_to_text",
    "parse_spec_text",
    "read_local",
    "snapshot_docs",
    "suggest_role",
    "summarize",
]
