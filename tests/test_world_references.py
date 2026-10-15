"""Relations in both directions: the reverse minimum, writes that keep references, expansion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fauxcall.model import (
    ApiModel,
    CollectionModel,
    EndpointModel,
    RealTarget,
    RelationModel,
    SourceInfo,
)
from fauxcall.model.world import ExpansionSpec
from fauxcall.world import (
    World,
    check_world,
    expand_collection,
    kept_by_writes,
    records_from_values,
    relation_summary,
    uncovered_targets,
)
from fauxcall.world.references import apply_delete, missing_references, plan_delete

if TYPE_CHECKING:
    from fauxcall.types import JsonValue
    from fauxcall.world import SessionWorld

NOW = datetime(2026, 10, 14, tzinfo=UTC)
AUTHORS: list[JsonValue] = [
    {"id": 1, "name": "Ursula K. Le Guin"},
    {"id": 2, "name": "J. R. R. Tolkien"},
    {"id": 3, "name": "Nobody Yet"},  # no books: breaks "every author has a book"
]
BOOKS: list[JsonValue] = [
    {"id": 10, "title": "A Wizard of Earthsea", "authorId": 1},
    {"id": 11, "title": "The Dispossessed", "authorId": 1},
    {"id": 12, "title": "The Hobbit", "authorId": 2},
]
REVIEWS: list[JsonValue] = [{"id": 100, "bookId": 10}, {"id": 101, "bookId": 12}]
SHELVES: list[JsonValue] = [{"id": 7, "books": [10, 12, 11]}]


def relation(name: str, source: str, field: str, target: str, **rules: object) -> RelationModel:
    return RelationModel.model_validate(
        {
            "name": name,
            "from_collection": source,
            "from_field": field,
            "to_collection": target,
            **rules,
        }
    )


BOOK_AUTHOR = relation("book_author", "books", "authorId", "authors", min_per_target=1)
REVIEW_BOOK = relation("review_book", "reviews", "bookId", "books", on_delete="cascade")
SHELF_BOOKS = relation("shelf_books", "shelves", "books[*]", "books", on_delete="set_null")


def library(*relations: RelationModel, endpoints: list[dict[str, str]] | None = None) -> ApiModel:
    return ApiModel(
        name="library",
        title="Library",
        source=SourceInfo(kind="openapi", url="https://library.test/spec", fetched_at=NOW),
        real=RealTarget(base_url="https://library.test"),
        collections=[
            CollectionModel(name="authors"),
            CollectionModel(name="books"),
            CollectionModel(name="reviews"),
            CollectionModel(name="shelves"),
        ],
        endpoints=[
            EndpointModel.model_validate(
                {"method": e["id"].split()[0], "path": e["id"].split()[1], **e}
            )
            for e in endpoints or []
        ],
        relations=list(relations),
    )


def world_of(model: ApiModel, **overrides: list[JsonValue]) -> SessionWorld:
    values = {"authors": AUTHORS, "books": BOOKS, "reviews": REVIEWS, "shelves": SHELVES}
    values |= overrides
    records = {
        name: records_from_values(model.collection(name), items) for name, items in values.items()
    }
    return World.from_records(model, records).session()


# -- the reverse ----------------------------------------------------------------------------------


def test_every_target_needs_its_minimum_of_referrers() -> None:
    model = library(BOOK_AUTHOR)
    problems = check_world(model, world_of(model))
    assert problems == [
        (
            "relation 'book_author' (reverse): authors[3] is referenced by 0 books record(s), "
            "needs at least 1 (record its books, or expand books after authors)"
        )
    ]
    two = library(relation("book_author", "books", "authorId", "authors", min_per_target=2))
    assert len(check_world(two, world_of(two))) == 2  # Tolkien has one book, Nobody none
    assert check_world(model, world_of(model), after_writes=True) == []  # writes may break it


def test_reverse_minimums_through_another_field() -> None:
    by_name = relation(
        "book_author_name", "books", "author", "authors", to_field="name", min_per_target=1
    )
    model = library(by_name)
    books: list[JsonValue] = [{"id": 10, "author": "Ursula K. Le Guin"}]
    problems = check_world(model, world_of(model, books=books))
    assert [p.split(":")[1].split(" is")[0].strip() for p in problems] == [
        "authors[2]",
        "authors[3]",
    ]


def test_the_summary_reports_both_directions() -> None:
    model = library(BOOK_AUTHOR, REVIEW_BOOK)
    books: list[JsonValue] = [*BOOKS, {"id": 13, "title": "Lost", "authorId": 99}]
    lines = relation_summary(model, world_of(model, books=books))
    assert lines == [
        (
            "book_author: books.authorId -> authors: 4 references, 1 dangling; 2 of 3 authors "
            "referenced (fewest referrers: 0; min_per_target: 1)"
        ),
        (
            "review_book: reviews.bookId -> books: 2 references, 0 dangling; 2 of 4 books "
            "referenced (fewest referrers: 0; min_per_target: undecided)"
        ),
    ]
    broken = library(relation("broken", "books", "authorId", "nowhere"))
    [line] = relation_summary(broken, world_of(model))
    assert line.startswith("broken: ")  # reported, not raised


# -- what writes keep -----------------------------------------------------------------------------


def test_which_references_writes_keep() -> None:
    writes = [
        {"id": "POST /books", "role": "create", "collection": "books"},
        {"id": "DELETE /authors/{id}", "role": "delete", "collection": "authors"},
    ]
    strict = relation(
        "a", "books", "authorId", "authors", on_missing="refuse", on_delete="restrict"
    )
    assert kept_by_writes(library(strict, endpoints=writes), strict)
    loose = relation("b", "books", "authorId", "authors", on_missing="accept", on_delete="cascade")
    assert not kept_by_writes(library(loose, endpoints=writes), loose)  # accepts dangling ones
    orphans = relation("c", "books", "authorId", "authors", on_missing="refuse", on_delete="ignore")
    assert not kept_by_writes(library(orphans, endpoints=writes), orphans)  # deletes orphan
    undecided = relation("d", "books", "authorId", "authors")
    assert kept_by_writes(library(undecided), undecided)  # nothing writes either side
    model = library(orphans, endpoints=writes)
    books: list[JsonValue] = [{"id": 10, "authorId": 99}]
    assert check_world(model, world_of(model, books=books), after_writes=True) == []
    assert check_world(model, world_of(model, books=books)) != []  # the built world must hold


def test_writes_to_missing_targets() -> None:
    refusing = relation("book_author", "books", "authorId", "authors", on_missing="refuse")
    model = library(refusing, REVIEW_BOOK)
    world = world_of(model)
    assert missing_references(model, world, "books", {"id": 20, "authorId": 99}) == [(refusing, 99)]
    assert missing_references(model, world, "books", {"id": 20, "authorId": 1}) == []
    assert missing_references(model, world, "books", {"id": 20, "authorId": None}) == []
    assert missing_references(model, world, "reviews", {"id": 1, "bookId": 99}) == []  # undecided


def test_deleting_a_referenced_record() -> None:
    restricted = relation("book_author", "books", "authorId", "authors", on_delete="restrict")
    model = library(restricted, REVIEW_BOOK, SHELF_BOOKS)
    world = world_of(model)
    author = world.get("authors", 1)
    assert author is not None
    plan = plan_delete(model, world, "authors", author)
    assert plan.blocked_by is not None
    assert plan.blocked_by[0] is restricted
    assert plan.deletes == []  # nothing changes
    lonely = world.get("authors", 3)
    assert lonely is not None
    assert plan_delete(model, world, "authors", lonely).blocked_by is None  # no books: fine
    book = world.get("books", 10)
    assert book is not None
    plan = plan_delete(model, world, "books", book)
    assert [(c, r.key) for c, r in plan.deletes] == [("books", 10), ("reviews", 100)]
    assert [(rel.name, r.key) for rel, r, _ in plan.clears] == [("shelf_books", 7)]
    apply_delete(world, plan)
    assert world.get("books", 10) is None
    assert world.get("reviews", 100) is None  # cascaded
    shelf = world.get("shelves", 7)
    assert shelf is not None
    assert shelf.record == {"id": 7, "books": [12, 11]}  # removed from the list, order kept


def test_restrict_down_a_cascade_refuses_the_whole_delete() -> None:
    cascade = relation("book_author", "books", "authorId", "authors", on_delete="cascade")
    guarded = relation("review_book", "reviews", "bookId", "books", on_delete="restrict")
    model = library(cascade, guarded)
    world = world_of(model)
    author = world.get("authors", 1)
    assert author is not None
    plan = plan_delete(model, world, "authors", author)
    assert plan.blocked_by is not None
    assert plan.blocked_by[0] is guarded  # Earthsea has a review


def test_clearing_a_scalar_reference_and_odd_cases() -> None:
    cleared = relation("book_author", "books", "authorId", "authors", on_delete="set_null")
    cycle = relation("author_favourite", "authors", "favourite", "books", on_delete="cascade")
    model = library(cleared, cycle)
    authors: list[JsonValue] = [{"id": 1, "favourite": 11}, {"id": 2}]
    world = world_of(model, authors=authors)
    author = world.get("authors", 1)
    assert author is not None
    plan = plan_delete(model, world, "authors", author)
    # Le Guin's books lose their author; her favourite book doesn't cascade back to her
    assert [(rel.name, r.key) for rel, r, _ in plan.clears] == [
        ("book_author", 10),
        ("book_author", 11),
    ]
    apply_delete(world, plan)
    book = world.get("books", 11)
    assert book is not None
    assert book.record == {"id": 11, "title": "The Dispossessed", "authorId": None}
    world.delete("books", 10)
    apply_delete(world, plan)  # again: the records are gone or already cleared


# -- expansion -----------------------------------------------------------------------------------


def test_expansion_covers_targets_short_of_referrers_first() -> None:
    model = library(BOOK_AUTHOR)
    world = world_of(model)
    books = world.list("books")
    wanted = uncovered_targets(world, BOOK_AUTHOR, books)
    assert wanted == [3]  # the author without a book
    assert uncovered_targets(world, REVIEW_BOOK, world.list("reviews")) == []  # no minimum
    spec = ExpansionSpec(target_count=5, vary=["authorId"])
    expanded = expand_collection(
        model.collection("books"),
        books,
        spec,
        reference_values={"authorId": [1, 2, 3]},
        first_values={"authorId": wanted},
    )
    assert expanded[3].record["authorId"] == 3  # type: ignore[index, call-overload]
    model.collections[1].expansion = spec
    grown = World.from_records(model, {"authors": world.list("authors"), "books": expanded})
    assert check_world(model, grown.session()) == []
    by_name = relation(
        "book_author_name", "books", "author", "authors", to_field="name", min_per_target=1
    )
    named = library(by_name)
    assert uncovered_targets(world_of(named), by_name, []) == [
        "Ursula K. Le Guin",
        "J. R. R. Tolkien",
        "Nobody Yet",
    ]


# -- what is left to decide -----------------------------------------------------------------------


def test_undecided_rules_are_listed() -> None:
    writes = [
        {"id": "POST /books", "role": "create", "collection": "books"},
        {"id": "DELETE /authors/{id}", "role": "delete", "collection": "authors"},
    ]
    undecided = relation("book_author", "books", "authorId", "authors")
    assert library(undecided, endpoints=writes).undecided() == [
        (
            "relation 'book_author' (books.authorId -> authors): on_missing (refuse or accept a "
            "missing target?)"
        ),
        (
            "relation 'book_author' (books.authorId -> authors): on_delete (deleting a referenced "
            "target does what?)"
        ),
        (
            "relation 'book_author' (books.authorId -> authors): min_per_target (must every target "
            "be referenced?)"
        ),
    ]
    decided = relation(
        "book_author",
        "books",
        "authorId",
        "authors",
        on_missing="refuse",
        on_delete="restrict",
        min_per_target=0,
    )
    assert library(decided, endpoints=writes).undecided() == []
    assert len(library(undecided).undecided()) == 1  # no writes: only the reverse to decide


def test_the_same_record_reached_twice() -> None:
    wrote = relation("book_author", "books", "authorId", "authors", on_delete="cascade")
    edited = relation("book_editor", "books", "editorId", "authors", on_delete="cascade")
    co = relation("book_coauthor", "books", "coAuthorId", "authors", on_delete="set_null")
    model = library(wrote, co, edited)
    books: list[JsonValue] = [{"id": 10, "authorId": 1, "coAuthorId": 1, "editorId": 1}]
    world = world_of(model, books=books)
    author = world.get("authors", 1)
    assert author is not None
    plan = plan_delete(model, world, "authors", author)
    assert [(c, r.key) for c, r in plan.deletes] == [("authors", 1), ("books", 10)]  # once
    apply_delete(world, plan)  # the book is deleted, so there's nothing left to clear
    assert world.get("books", 10) is None


def test_references_that_cannot_be_keys_count_for_nothing() -> None:
    model = library(BOOK_AUTHOR)
    books: list[JsonValue] = [*BOOKS, {"id": 13, "authorId": "not-a-number"}]
    problems = check_world(model, world_of(model, books=books))
    assert any("authors[3] is referenced by 0" in p for p in problems)
