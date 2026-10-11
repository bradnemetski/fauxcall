"""The Petstore's behavior, mirroring the real server over FauxCall's consistent world.

The real service is swagger-petstore v3 (Swagger Inflector + Jackson on Jetty). Its controllers
and in-memory ``ArrayList`` stores (``PetData``, ``OrderData``, ``UserData``) explain everything
recorded, including the quirks agents will meet:

- writes are not validated (missing ``name``, unknown ``status`` are stored as sent);
- every update deletes the record and appends it again, so it moves to the end of listings;
- ``DELETE`` always succeeds, even for ids that never existed;
- ``/store/inventory`` sums order *quantities* per order status (not pets!) and fails while any
  order has no status (Jackson cannot write a ``null`` map key);
- the shared sandbox holds two broken records nobody can remove: a user with a null username
  (every lookup that reaches it, and every user write, fails with 500) and a pet with a ``null``
  tag (every ``findByTags`` fails with 500). See :data:`USERS_POISONED` / :data:`TAGS_POISONED`.

Recording ids in comments point at the evidence in ``data/recordings``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fauxcall_petstore import bodies, wire
from fauxcall_petstore.bodies import ABSENT, Unreadable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue
    from fauxcall.world import SessionWorld

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

_HEALTHY = os.environ.get("FAUXCALL_PETSTORE_HEALTHY", "").lower() in {"1", "true", "yes"}

USERS_POISONED = not _HEALTHY
"""The sandbox's user list holds a user with a null username after the seeded ones: lookups past
the seeded users and every user write raise a NullPointerException (000100-000107, 000247-000286;
``UserData.findUserByName``/``addUser``/``deleteUser``). ``FAUXCALL_PETSTORE_HEALTHY=1`` turns this
off (documented behavior from the server source, not recorded)."""

TAGS_POISONED = not _HEALTHY
"""A sandbox pet has a null entry in ``tags``: ``PetData.findPetByTags`` fails (000014-000020)."""

SEEDED_PASSWORD = "XXXXXXXXXXX"  # noqa: S105 - the server's public placeholder, not a secret
"""What the server stores for its seeded users (``UserData.createUser``; visible in 000108)."""

HIDDEN_ORDER_QUANTITIES: tuple[tuple[str, int], ...] = (("placed", -20),)
"""Orders in the sandbox that cannot be enumerated (there is no list endpoint), summed by status.

``/store/inventory`` answered ``{"approved": 7, "placed": -19}`` (000207, 000349, 000351) while the
known orders contributed approved 7 (order 10) and placed 1 (order 0); the rest is these."""

UPLOAD_PREFIX = "/tmp/inflector"  # noqa: S108 - the real server's temp file names (000180)


def _register(
    endpoint_id: str,
) -> Callable[
    [Callable[[RequestContext], Awaitable[Response]]],
    Callable[[RequestContext], Awaitable[Response]],
]:
    def decorate(
        handler: Callable[[RequestContext], Awaitable[Response]],
    ) -> Callable[[RequestContext], Awaitable[Response]]:
        HANDLERS[endpoint_id] = handler
        return handler

    return decorate


# -- shared helpers --------------------------------------------------------------------------------


def _path_long(ctx: RequestContext, name: str) -> int | Response:
    raw = ctx.path_param(name) or ""
    value = bodies.parse_long(raw)
    return wire.convert_error(ctx, raw) if value is None else value


def _content_type(ctx: RequestContext) -> str:
    return bodies.media_type(ctx.header("content-type"))


def _query_values(ctx: RequestContext, name: str) -> list[str]:
    return ctx.request.query_params.getlist(name)


def _pets(world: SessionWorld) -> list[dict[str, JsonValue]]:
    return [r.record for r in world.list("pets") if isinstance(r.record, dict)]


def _tags(pet: dict[str, JsonValue]) -> list[JsonValue]:
    tags = pet.get("tags")
    return tags if isinstance(tags, list) else []


def _find_pet(world: SessionWorld, pet_id: int) -> dict[str, JsonValue] | None:
    found = world.get("pets", pet_id)
    return found.record if found is not None and isinstance(found.record, dict) else None


def _store_pet(world: SessionWorld, pet: dict[str, JsonValue]) -> None:
    """``PetData.addPet``: drop any pet with this id, then append (the pet moves to the end)."""
    world.delete("pets", pet["id"])
    world.insert("pets", {k: v for k, v in pet.items() if v is not None})


def _pet_entity(ctx: RequestContext, pet: dict[str, JsonValue]) -> Response:
    return wire.entity(ctx, wire.pet_json(pet), wire.pet_xml(pet), yaml=wire.pet_full(pet))


def _pets_entity(ctx: RequestContext, pets: list[dict[str, JsonValue]]) -> Response:
    return wire.entity(
        ctx,
        [wire.pet_json(p) for p in pets],
        wire.pets_xml(pets),
        yaml=[wire.pet_full(p) for p in pets],
    )


def _pet_not_found(ctx: RequestContext, content_type: str = wire.XML) -> Response:
    return wire.plain_error(ctx, 404, "Pet not found", content_type)


# -- pets ------------------------------------------------------------------------------------------


@_register("GET /api/v3/pet/findByStatus")
async def find_pets_by_status(ctx: RequestContext) -> Response:
    """Pets whose status equals the (first) ``status`` value, in list order (000001-000004)."""
    values = _query_values(ctx, "status")
    if not values:
        return wire.api_error(ctx, 400, "Input error: missing required query parameter `status`")
    status = values[0]
    if status not in {"available", "pending", "sold"}:
        return wire.api_error(
            ctx,
            400,
            f"Input error: query parameter `status value `{status}` is not in the allowable "
            "values `[available, pending, sold]`",
        )
    found = [p for p in _pets(ctx.world) if p.get("status") == status]
    return _pets_entity(ctx, found)


@_register("GET /api/v3/pet/findByTags")
async def find_pets_by_tags(ctx: RequestContext) -> Response:
    """Pets carrying any of the tag names (a pet appears once per matching tag)."""
    values = _query_values(ctx, "tags")
    if not values:
        return wire.api_error(ctx, 400, "Input error: missing required query parameter `tags`")
    tags = [t for t in values if t != ""]
    if not tags:
        return wire.plain_error(ctx, 400, "No tags provided. Try again?")
    pets = _pets(ctx.world)
    if TAGS_POISONED or any(not isinstance(t, dict) for p in pets for t in _tags(p)):
        return wire.server_error(ctx)  # a null tag NPEs in PetData.findPetByTags
    found = [
        pet
        for pet in pets
        for tag in _tags(pet)
        for name in tags
        if isinstance(tag, dict) and tag.get("name") == name
    ]
    return _pets_entity(ctx, found)


@_register("GET /api/v3/pet/{petId}")
async def get_pet_by_id(ctx: RequestContext) -> Response:
    """One pet; 404 ``Pet not found`` (text, labeled XML) when absent (000024)."""
    pet_id = _path_long(ctx, "petId")
    if not isinstance(pet_id, int):
        return pet_id
    pet = _find_pet(ctx.world, pet_id)
    return _pet_not_found(ctx) if pet is None else _pet_entity(ctx, pet)


async def _read_pet(ctx: RequestContext) -> dict[str, JsonValue] | Response:
    media = _content_type(ctx)
    try:
        if media == "application/x-www-form-urlencoded":
            return bodies.pet_from_form(bodies.decode_form(ctx.body_bytes))
        if media not in {"application/json", "application/xml"}:
            return wire.unsupported_media(ctx)
        pet = bodies.read_model(ctx.body_bytes, media, bodies.pet_from)
    except Unreadable:
        return wire.body_error(ctx, "Pet")
    if pet is ABSENT:
        return wire.server_error(ctx)  # a null body fails before the controller (000425)
    assert isinstance(pet, dict)  # noqa: S101 - read_model returns a record or ABSENT
    return pet


@_register("POST /api/v3/pet")
async def add_pet(ctx: RequestContext) -> Response:
    """Store the pet as sent (no validation); an existing id is replaced (000130-000152)."""
    pet = await _read_pet(ctx)
    if not isinstance(pet, dict):
        return pet
    if pet.get("id") is None:
        return wire.server_error(ctx)  # unboxing a null id (000152)
    _store_pet(ctx.world, pet)
    return _pet_entity(ctx, pet)


@_register("PUT /api/v3/pet")
async def update_pet(ctx: RequestContext) -> Response:
    """Replace an existing pet wholesale (000155-000165)."""
    pet = await _read_pet(ctx)
    if not isinstance(pet, dict):
        return pet
    pet_id = pet.get("id")
    if not isinstance(pet_id, int):
        return wire.server_error(ctx)
    if _find_pet(ctx.world, pet_id) is None:
        return _pet_not_found(ctx)
    _store_pet(ctx.world, pet)
    return _pet_entity(ctx, pet)


@_register("POST /api/v3/pet/{petId}")
async def update_pet_with_form(ctx: RequestContext) -> Response:
    """Set name and status from the query string; status is cleared when absent (000166-000179)."""
    pet_id = _path_long(ctx, "petId")
    if not isinstance(pet_id, int):
        return pet_id
    name = ctx.query("name")
    if name is None:
        return wire.plain_error(ctx, 400, "No Name provided. Try again?")
    pet = _find_pet(ctx.world, pet_id)
    if pet is None:
        return _pet_not_found(ctx)
    pet["name"] = name
    pet["status"] = ctx.query("status")
    _store_pet(ctx.world, pet)
    return _pet_entity(ctx, pet)


@_register("DELETE /api/v3/pet/{petId}")
async def delete_pet(ctx: RequestContext) -> Response:
    """Remove the pet; always ``Pet deleted`` (000188-000199)."""
    pet_id = _path_long(ctx, "petId")
    if not isinstance(pet_id, int):
        return pet_id
    ctx.world.delete("pets", pet_id)
    return wire.text(ctx, "Pet deleted")


@_register("POST /api/v3/pet/{petId}/uploadImage")
async def upload_file(ctx: RequestContext) -> Response:
    """Append the server's temp file path to ``photoUrls`` (000180-000187)."""
    pet_id = _path_long(ctx, "petId")
    if not isinstance(pet_id, int):
        return pet_id
    if _content_type(ctx) != "application/octet-stream":
        return wire.unsupported_media(ctx)
    if not ctx.body_bytes:
        return wire.plain_error(ctx, 400, "No file uploaded", wire.JSON)
    pet = _find_pet(ctx.world, pet_id)
    if pet is None:
        return _pet_not_found(ctx, wire.JSON)
    photos = pet.get("photoUrls")
    if not isinstance(photos, list):
        return wire.server_error(ctx)  # getPhotoUrls().add on a form-created pet
    photos.append(f"{UPLOAD_PREFIX}{ctx.rng.getrandbits(63)}.tmp")
    _store_pet(ctx.world, pet)
    return _pet_entity(ctx, pet)


# -- store -----------------------------------------------------------------------------------------


def inventory_counts(orders: list[dict[str, JsonValue]]) -> dict[str, int] | None:
    """``OrderData.getCountByStatus`` over hidden + known orders; ``None`` where it would fail.

    Quantities are primitive ``int`` (never null); a null status becomes a null map key, which
    Jackson cannot write (000213-000235).
    """
    counts: dict[str | None, int] = {}
    entries = [*HIDDEN_ORDER_QUANTITIES]
    entries += [(o.get("status"), o.get("quantity")) for o in orders]  # type: ignore[misc]
    for status, quantity in entries:
        key = status if isinstance(status, str) else None
        counts[key] = counts.get(key, 0) + (quantity if isinstance(quantity, int) else 0)
    if None in counts:
        return None
    keys = wire.hashmap_order([k for k in counts if k is not None])
    return {key: counts[key] for key in keys}


def _orders(world: SessionWorld) -> list[dict[str, JsonValue]]:
    return [r.record for r in world.list("orders") if isinstance(r.record, dict)]


@_register("GET /api/v3/store/inventory")
async def get_inventory(ctx: RequestContext) -> Response:
    """Order quantities summed by order status (000207, 000210)."""
    counts = inventory_counts(_orders(ctx.world))
    if counts is None:
        return wire.server_error(ctx)
    return wire.entity(ctx, dict(counts), wire.map_xml(counts))  # YAML: same map


def _order_entity(ctx: RequestContext, order: dict[str, JsonValue]) -> Response:
    return wire.entity(
        ctx,
        wire.ordered(order, wire.ORDER_FIELDS),
        wire.order_xml(order),
        yaml=wire.full(order, wire.ORDER_FIELDS),
    )


@_register("POST /api/v3/store/order")
async def place_order(ctx: RequestContext) -> Response:
    """Store the order as sent; an existing id is replaced (000208-000232)."""
    media = _content_type(ctx)
    if media == "application/x-www-form-urlencoded":
        return wire.server_error(ctx)  # 000229
    if media not in {"application/json", "application/xml"}:
        return wire.unsupported_media(ctx)
    try:
        order = bodies.read_model(ctx.body_bytes, media, bodies.order_from)
    except Unreadable:
        return wire.body_error(ctx, "Order")
    if order is ABSENT:
        return wire.server_error(ctx)  # 000425
    assert isinstance(order, dict)  # noqa: S101
    ctx.world.delete("orders", order["id"])
    ctx.world.insert("orders", {k: v for k, v in order.items() if v is not None})
    return _order_entity(ctx, order)


@_register("GET /api/v3/store/order/{orderId}")
async def get_order_by_id(ctx: RequestContext) -> Response:
    """One order; 404 ``Order not found`` (000070)."""
    order_id = _path_long(ctx, "orderId")
    if not isinstance(order_id, int):
        return order_id
    found = ctx.world.get("orders", order_id)
    if found is None or not isinstance(found.record, dict):
        return wire.plain_error(ctx, 404, "Order not found")
    return _order_entity(ctx, found.record)


@_register("DELETE /api/v3/store/order/{orderId}")
async def delete_order(ctx: RequestContext) -> Response:
    """Remove the order; always 200 with no body (000233-000240)."""
    order_id = _path_long(ctx, "orderId")
    if not isinstance(order_id, int):
        return order_id
    ctx.world.delete("orders", order_id)
    return wire.empty_ok(ctx)


# -- users -----------------------------------------------------------------------------------------


def _user_json(user: dict[str, JsonValue]) -> dict[str, JsonValue]:
    shown = dict(user)
    if shown.get("password") == "[REDACTED]":  # a seeded user's recorded (redacted) password
        shown["password"] = SEEDED_PASSWORD
    return wire.ordered(shown, wire.USER_FIELDS)


def _user_entity(ctx: RequestContext, user: dict[str, JsonValue]) -> Response:
    body = _user_json(user)
    return wire.entity(ctx, body, wire.user_xml(body), yaml=wire.full(body, wire.USER_FIELDS))


class _Poisoned(Exception):  # noqa: N818 - the server's NullPointerException
    """A scan reached the user with a null username."""


def _find_user(world: SessionWorld, username: str) -> dict[str, JsonValue] | None:
    """``UserData.findUserByName``: scan in order; the poisoned record sits after the known ones."""
    for record in world.list("users"):
        user = record.record
        if isinstance(user, dict) and user.get("username") == username:
            return user
    if USERS_POISONED:
        raise _Poisoned
    return None


def _add_user(world: SessionWorld, user: dict[str, JsonValue]) -> None:
    """``UserData.addUser``: the reverse scan hits the poisoned record first."""
    if USERS_POISONED:
        raise _Poisoned
    if user.get("username") is None:
        # The healthy server stores it (that is how the sandbox got poisoned); the world needs a
        # key, so the fake only echoes it. Healthy mode is documentation-based, never recorded.
        return
    world.delete("users", user["username"])
    world.insert("users", {k: v for k, v in user.items() if v is not None})


def _delete_user(world: SessionWorld, username: str) -> None:
    """``UserData.deleteUser`` (``removeIf`` evaluates every user before removing any)."""
    if USERS_POISONED:
        raise _Poisoned
    world.delete("users", username)


async def _read_user(ctx: RequestContext) -> dict[str, JsonValue] | Response:
    media = _content_type(ctx)
    try:
        if media == "application/x-www-form-urlencoded":
            return bodies.user_from_form(bodies.decode_form(ctx.body_bytes))
        if media not in {"application/json", "application/xml"}:
            return wire.unsupported_media(ctx)
        user = bodies.read_model(ctx.body_bytes, media, bodies.user_from)
    except Unreadable:
        return wire.body_error(ctx, "User")
    except bodies.MissingPrimitive:
        return wire.server_error(ctx)  # 000412: a form without id
    if user is ABSENT:
        return wire.server_error(ctx)  # like a null order body (000425); not recorded for users
    assert isinstance(user, dict)  # noqa: S101
    return user


@_register("POST /api/v3/user")
async def create_user(ctx: RequestContext) -> Response:
    """Add a user (fails with 500 while the store is poisoned: 000247-000258)."""
    user = await _read_user(ctx)
    if not isinstance(user, dict):
        return user
    try:
        _add_user(ctx.world, user)
    except _Poisoned:
        return wire.server_error(ctx)
    return _user_entity(ctx, user)


@_register("POST /api/v3/user/createWithList")
async def create_users_with_list_input(ctx: RequestContext) -> Response:
    """Add several users (000261-000266)."""
    media = _content_type(ctx)
    if media != "application/json":
        return wire.unsupported_media(ctx)  # the operation consumes JSON only (000415)
    try:
        users = bodies.read_users(ctx.body_bytes, media)
    except Unreadable:
        return wire.body_error(ctx, "User[]")
    if not users:
        return wire.plain_error(ctx, 400, "No User provided. Try again?")
    try:
        for user in users:
            _add_user(ctx.world, user)
    except _Poisoned:
        return wire.server_error(ctx)
    shown = [_user_json(u) for u in users]
    return wire.entity(
        ctx,
        list(shown),
        wire.users_xml(shown),
        yaml=[wire.full(u, wire.USER_FIELDS) for u in shown],
    )


@_register("GET /api/v3/user/login")
async def login_user(ctx: RequestContext) -> Response:
    """Always succeeds: a random session number, valid for an hour (000109-000120)."""
    expires = wire.java_date_string(ctx.now_ns + 3_600 * 10**9)
    session = ctx.rng.getrandbits(63)
    media = wire.XML if wire.wants_xml(ctx) else wire.JSON
    return ctx.respond_bytes(
        f"Logged in user session: {session}".encode(),
        content_type=media,
        headers={"X-Rate-Limit": "5000", "X-Expires-After": expires},
    )


@_register("GET /api/v3/user/logout")
async def logout_user(ctx: RequestContext) -> Response:
    """Always ``User logged out`` (000121-000123)."""
    return wire.text(ctx, "User logged out")


@_register("GET /api/v3/user/{username}")
async def get_user_by_name(ctx: RequestContext) -> Response:
    """A seeded user, or 500 once the scan reaches the poisoned record (000095-000108)."""
    username = ctx.path_param("username") or ""
    try:
        user = _find_user(ctx.world, username)
    except _Poisoned:
        return wire.server_error(ctx)
    if user is None:
        return wire.plain_error(ctx, 404, "User not found")
    return _user_entity(ctx, user)


@_register("PUT /api/v3/user/{username}")
async def update_user(ctx: RequestContext) -> Response:
    """Replace a user (500 while the store is poisoned: 000267-000276)."""
    username = ctx.path_param("username") or ""
    user = await _read_user(ctx)
    if not isinstance(user, dict):
        return user
    try:
        existing = _find_user(ctx.world, username)
        if existing is None:
            return wire.plain_error(ctx, 404, "User not found")
        _delete_user(ctx.world, username)
        _add_user(ctx.world, user)
    except _Poisoned:
        return wire.server_error(ctx)
    return _user_entity(ctx, user)


@_register("DELETE /api/v3/user/{username}")
async def delete_user(ctx: RequestContext) -> Response:
    """Remove a user; 200 with no body (500 while the store is poisoned: 000277-000286)."""
    username = ctx.path_param("username") or ""
    try:
        _delete_user(ctx.world, username)
    except _Poisoned:
        return wire.server_error(ctx)
    return wire.empty_ok(ctx)
