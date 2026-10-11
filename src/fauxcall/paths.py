"""Where things live: the workspace of fakes, and the layout inside one fake."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fauxcall.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

_SLUG_RE: Final = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_SLUG_LENGTH: Final = 40
MAX_ENDPOINT_SLUG: Final = 80


def slugify(text: str) -> str:
    """Turn a title like ``"Swagger Petstore - OpenAPI 3.0"`` into a kebab-case slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"^[0-9-]+", "", slug)
    return slug[:MAX_SLUG_LENGTH].rstrip("-") or "api"


def validate_slug(name: str) -> str:
    """Return ``name`` if it is a valid fake name (lowercase kebab-case), else raise."""
    if not _SLUG_RE.match(name) or len(name) > MAX_SLUG_LENGTH:
        raise ConfigurationError(
            f"invalid fake name {name!r}: use lowercase letters, digits and single hyphens "
            f"(max {MAX_SLUG_LENGTH} characters), starting with a letter"
        )
    return name


def endpoint_slug(endpoint_id: str) -> str:
    """Directory name for an endpoint's recordings.

    ``"GET /api/v3/pet/{petId}"`` -> ``"get-api-v3-pet-petid"``;
    ``"MCP tools/call:ask_question"`` -> ``"mcp-tools-call-ask-question"``. Very long ids are cut
    and suffixed with a short hash; :func:`unique_endpoint_slug` resolves rare collisions.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", endpoint_id.lower()).strip("-") or "endpoint"
    digest = hashlib.sha256(endpoint_id.encode()).hexdigest()[:6]
    if len(slug) > MAX_ENDPOINT_SLUG:
        return f"{slug[:MAX_ENDPOINT_SLUG].rstrip('-')}-{digest}"
    return slug


def unique_endpoint_slug(endpoint_id: str) -> str:
    """:func:`endpoint_slug` plus a short hash — used when the plain slug is already taken."""
    digest = hashlib.sha256(endpoint_id.encode()).hexdigest()[:6]
    return f"{endpoint_slug(endpoint_id)[:MAX_ENDPOINT_SLUG]}-{digest}"


def package_name(name: str) -> str:
    """Python import package for a fake: ``"pet-store"`` -> ``"fauxcall_pet_store"``."""
    return "fauxcall_" + validate_slug(name).replace("-", "_")


@dataclass(frozen=True, slots=True)
class FakeLayout:
    """Every path inside one generated fake (``<workspace>/<name>/``)."""

    root: Path
    name: str

    @property
    def package(self) -> str:
        """The fake's Python import package."""
        return package_name(self.name)

    @property
    def data(self) -> Path:
        """Committed data: recordings, model, world."""
        return self.root / "data"

    @property
    def source_dir(self) -> Path:
        """Snapshot of the input: original spec, docs, MCP introspection."""
        return self.data / "source"

    @property
    def recordings(self) -> Path:
        """Raw request/response evidence, one JSON file per exchange."""
        return self.data / "recordings"

    @property
    def model_path(self) -> Path:
        """The declarative API model."""
        return self.data / "model.json"

    @property
    def world(self) -> Path:
        """The consistent seed dataset (one JSONL file per collection)."""
        return self.data / "world"

    @property
    def series(self) -> Path:
        """Recorded value series for optional replay."""
        return self.data / "series"

    @property
    def plans(self) -> Path:
        """Recording plans."""
        return self.data / "plans"

    @property
    def approvals_path(self) -> Path:
        """The single up-front approval of mutating calls."""
        return self.data / "approvals.json"

    @property
    def progress_path(self) -> Path:
        """Build checkpoints, so an interrupted run resumes where it stopped."""
        return self.data / "progress.json"

    @property
    def jev_cache(self) -> Path:
        """Cached Jev answers (makes runtime judgments repeatable)."""
        return self.data / "jev-cache.jsonl"

    @property
    def reports(self) -> Path:
        """Analysis and parity reports."""
        return self.data / "reports"

    @property
    def state(self) -> Path:
        """Opt-in persisted runtime state (gitignored)."""
        return self.data / "state"

    @property
    def src(self) -> Path:
        """The fake's Python package directory."""
        return self.root / "src" / self.package

    @property
    def tests(self) -> Path:
        """The fake's tests."""
        return self.root / "tests"

    @property
    def env_file(self) -> Path:
        """The fake's gitignored ``.env``."""
        return self.root / ".env"

    def exists(self) -> bool:
        """Whether this fake has been started (a model or progress file exists)."""
        return self.model_path.exists() or self.progress_path.exists()


@dataclass(frozen=True, slots=True)
class Workspace:
    """The folder holding all of a project's fakes (``./fauxcall`` by default)."""

    root: Path

    def fake(self, name: str) -> FakeLayout:
        """Layout of the fake called ``name``."""
        return FakeLayout(root=self.root / validate_slug(name), name=name)

    def fakes(self) -> list[FakeLayout]:
        """Every fake in the workspace, sorted by name."""
        if not self.root.is_dir():
            return []
        found = [
            FakeLayout(root=child, name=child.name)
            for child in sorted(self.root.iterdir())
            if child.is_dir() and _SLUG_RE.match(child.name) and (child / "data").is_dir()
        ]
        return [layout for layout in found if layout.exists()]
