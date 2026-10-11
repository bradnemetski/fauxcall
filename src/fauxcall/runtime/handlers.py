"""Generic handlers for each endpoint role.

They cover the common shapes of REST APIs straight from the model. Anything API-specific is a custom
handler in the generated fake that uses the same building blocks (``ctx.world``, ``ctx.respond``,
``ctx.error``, ``ctx.render_record`` …).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Final

import jsonschema
from starlette.datastructures import UploadFile
from starlette.requests import Request

from fauxcall import jsonpath
from fauxcall.clock import datetime_to_ns, ns_to_datetime
from fauxcall.errors import WorldError
from fauxcall.logs import get_logger
from fauxcall.model.endpoints import WriteSemantics
from fauxcall.model.world import WorldRecord
from fauxcall.runtime import generators
from fauxcall.runtime.inputs import json_inputs, path_inputs, query_inputs, template_names
from fauxcall.runtime.listing import (
    PageError,
    apply_filters,
    apply_sort,
    link_header,
    page_value,
    paginate,
)
from fauxcall.runtime.matching import Candidate, match
from fauxcall.runtime.templating import render, render_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from starlette.responses import Response

    from fauxcall.model.jobs import JobModel
    from fauxcall.model.recording import Exchange
    from fauxcall.runtime.context import RequestContext
    from fauxcall.types import JsonObject, JsonValue

Handler = "Callable[[RequestContext], Awaitable[Response]]"
log = get_logger(__name__)

_JSON_TYPES: Final = ("json",)


# -- helpers -------------------------------------------------------------------------------------


def order_like(value: JsonValue, schema: JsonValue) -> JsonValue:
    """Reorder object keys like the real API (the observed schema keeps first-seen key order)."""
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return value
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return value
    ordered: dict[str, JsonValue] = {}
    for key in properties:
        if key in value:
            ordered[key] = order_like(value[key], properties[key])
    for key, item in value.items():
        ordered.setdefault(key, item)
    return ordered


def _success_schema(ctx: RequestContext, status: int) -> JsonValue:
    variant = ctx.variant(status)
    return variant.observed_schema if variant is not None else None


def path_key(ctx: RequestContext) -> str | None:
    """The collection key named in the URL."""
    endpoint = ctx.endpoint
    if endpoint is None:
        return None
    name = endpoint.key_param
    if name is None:
        names = list(ctx.request.path_params)
        name = names[-1] if names else None
    return None if name is None else ctx.path_param(name)


def lookup(ctx: RequestContext, collection: str, key: JsonValue) -> WorldRecord | None:
    """A record by key; keys that can't even be normalized simply aren't found."""
    if key is None:
        return None
    try:
        return ctx.world.get(collection, key)
    except WorldError:
        return None


def job_for(ctx: RequestContext, collection: str | None) -> JobModel | None:
    """The job model bound to a collection, if any."""
    if collection is None:
        return None
    return next((job for job in ctx.model.jobs if job.collection == collection), None)


def render_job(ctx: RequestContext, job: JobModel, record: WorldRecord) -> WorldRecord:
    """A job record with its state, progress and lifecycle timestamps as of ``ctx.now_ns``."""
    created_ns = datetime_to_ns(record.recorded_at) if record.recorded_at else 0
    outcome = ctx.runtime.job_outcome(ctx.session, job.name, str(record.key))
    view = ctx.runtime.jobs.view(
        job, str(record.key), created_ns=created_ns, now_ns=ctx.now_ns, outcome=outcome
    )
    value = copy.deepcopy(record.record)
    if not isinstance(value, dict):
        return record
    value[job.state_field] = view.state
    if job.progress_field is not None and view.progress is not None:
        previous = value.get(job.progress_field)
        value[job.progress_field] = (
            int(view.progress)
            if isinstance(previous, int) or previous is None
            else round(view.progress, 2)
        )
    zone = ctx.zone(job.time_zone, value)
    instants = {
        "created": created_ns,
        "started": view.started_ns,
        "updated": view.state_started_ns,
        "completed": view.completed_ns,
    }
    for field_name, event in job.timestamps.items():
        instant = instants[event]
        value[field_name] = None if instant is None else job.time_format.render(instant, zone)
    return record.model_copy(update={"record": value})


async def _build_record(ctx: RequestContext) -> tuple[JsonObject | None, Response | None]:
    """The new record from a JSON or multipart body (``(None, error)`` when invalid)."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101 - handlers always run for a known endpoint
    content_type = ctx.request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        return await _record_from_form(ctx), None
    if ctx.json_error:
        return None, ctx.error("invalid_body")
    body = ctx.json_body
    request_body = endpoint.request_body
    if body is None:
        if request_body is not None and request_body.required:
            return None, ctx.error("invalid_body")
        body = {}
    if (
        request_body is not None
        and request_body.validate_schema
        and isinstance(request_body.body_schema, dict)
    ):
        try:
            jsonschema.validate(body, request_body.body_schema)
        except jsonschema.ValidationError as exc:
            return None, ctx.error("invalid_body", value=exc.message)
    if not isinstance(body, dict):
        return None, ctx.error("invalid_body")
    return copy.deepcopy(body), None


async def _record_from_form(ctx: RequestContext) -> JsonObject:
    request = ctx.request
    assert isinstance(request, Request)  # noqa: S101 - forms only arrive over HTTP
    form = await request.form()
    record: JsonObject = {}
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            content = await value.read()
            record.setdefault("filename", value.filename)
            record.setdefault("size", len(content))
            if value.content_type:
                record.setdefault("content_type", value.content_type)
            ctx.runtime.pending_blob(ctx, name, content, value.content_type, value.filename)
        else:
            record[name] = value
    return record


def _assign_key(
    ctx: RequestContext, collection_name: str, record: JsonObject, write: WriteSemantics
) -> JsonValue:
    collection = ctx.model.collection(collection_name)
    key_path = collection.key
    existing = jsonpath.get_one(record, key_path)
    match write.id_strategy:
        case "client":
            key = existing
        case "uuid4":
            key = generators.uuid4(ctx.rng)
        case "random_int":
            key = ctx.rng.randint(10**8, 10**9 - 1)
        case _:
            key = ctx.world.next_int_key(collection_name, start=write.id_start)
    if key is not None and jsonpath.set_all(record, key_path, lambda _old: key) == 0:
        top = jsonpath.parse(key_path)
        if len(top) == 1 and isinstance(top[0], str):
            record_with_key = {top[0]: key, **record}
            record.clear()
            record.update(record_with_key)
    return key


def _apply_defaults(
    ctx: RequestContext, record: JsonObject, write: WriteSemantics, key: JsonValue
) -> None:
    defaults = write.defaults
    if not isinstance(defaults, dict):
        return
    rendered = render(defaults, ctx.resolver(id=key, key=key))
    assert isinstance(rendered, dict)  # noqa: S101 - rendering keeps the template's shape
    for name, value in rendered.items():
        record.setdefault(name, value)


def _location(ctx: RequestContext, write: WriteSemantics, key: JsonValue) -> dict[str, str]:
    if write.location_header is None:
        return {}
    return {"location": render_text(write.location_header, ctx.resolver(id=key, key=key))}


def _write_response(
    ctx: RequestContext, write: WriteSemantics, record: WorldRecord, *, sent: JsonValue
) -> Response:
    headers = _location(ctx, write, record.key)
    match write.echo:
        case "empty":
            return ctx.empty(write.status, headers)
        case "request":
            return ctx.respond(ctx.render_body(sent), status=write.status, headers=headers)
        case "message":
            body = render(write.message_template, ctx.resolver(id=record.key, key=record.key))
            return ctx.respond(body, status=write.status, headers=headers)
        case _:
            rendered = order_like(ctx.render_record(record), _success_schema(ctx, write.status))
            return ctx.respond(rendered, status=write.status, headers=headers)


def _now(ctx: RequestContext) -> datetime:
    return ns_to_datetime(ctx.now_ns)


# -- role handlers -------------------------------------------------------------------------------


async def handle_get(ctx: RequestContext) -> Response:
    """``GET /things/{id}``: the record, or the real 404."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.collection is not None  # noqa: S101
    key = path_key(ctx)
    record = lookup(ctx, endpoint.collection, key)
    if record is None:
        return ctx.error("not_found", param=endpoint.key_param, value=key)
    job = job_for(ctx, endpoint.collection)
    if job is not None:
        record = render_job(ctx, job, record)
    return ctx.respond(ctx.render_record(record))


async def handle_list(ctx: RequestContext) -> Response:
    """``GET /things``: filtered, sorted, paginated records in the real envelope."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.collection is not None  # noqa: S101
    records = ctx.world.list(endpoint.collection)
    job = job_for(ctx, endpoint.collection)
    if job is not None:
        records = [render_job(ctx, job, record) for record in records]
    ordered = apply_sort(records, endpoint.sort, ctx.params)
    try:
        page = paginate(
            apply_filters(ordered, endpoint, ctx.params),
            endpoint.pagination,
            ctx.params,
            base_url=str(ctx.request.url).split("?", 1)[0],
            query=list(ctx.request.query_params.multi_items()),
            ordering=ordered,
        )
    except PageError as exc:
        return ctx.error("invalid_param", param=exc.param, value=exc.value)
    items: list[JsonValue] = [ctx.render_record(record) for record in page.items]
    body = _shape_list(ctx, items, page)
    headers = {"link": link_header(page.links)} if page.links else {}
    return ctx.respond(ctx.render_body(body), headers=headers)


def _shape_list(ctx: RequestContext, items: list[JsonValue], page: object) -> JsonValue:
    from fauxcall.runtime.listing import Page  # noqa: PLC0415

    assert isinstance(page, Page)  # noqa: S101
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    shape = endpoint.shape
    if shape.kind != "envelope" or shape.template is None:
        return items
    body = copy.deepcopy(shape.template)
    jsonpath.set_all(body, shape.items_path or "$.data", lambda _old: items)
    pagination = endpoint.pagination
    if pagination is None:
        return body
    fields: dict[str, str] = dict(pagination.page_fields)
    if pagination.total_path:
        fields[pagination.total_path] = "total"
    if pagination.next_path:
        fields[pagination.next_path] = "next"
    if pagination.has_more_path:
        fields[pagination.has_more_path] = "has_more"
    for path, field_name in fields.items():
        value = page_value(page, pagination, field_name, None)
        jsonpath.set_all(body, path, lambda _old, value=value: value)  # type: ignore[misc]
    return body


async def handle_create(ctx: RequestContext) -> Response:
    """``POST /things``: assign a key and defaults, persist if the real API does, echo back."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.collection is not None  # noqa: S101
    write = endpoint.write or WriteSemantics(status=endpoint.success_status)
    record, problem = await _build_record(ctx)
    if problem is not None or record is None:
        return problem or ctx.error("invalid_body")
    sent = copy.deepcopy(ctx.json_body)
    key = _assign_key(ctx, endpoint.collection, record, write)
    if key is None:
        return ctx.error("invalid_body")
    _apply_defaults(ctx, record, write, key)
    if write.persist:
        try:
            stored = ctx.world.insert(endpoint.collection, record, recorded_at=_now(ctx))
        except WorldError:
            return ctx.error("conflict", value=key)
        ctx.runtime.commit_blob(ctx, endpoint.collection, stored.key)
    else:
        stored = WorldRecord(key=key, record=record, recorded_at=_now(ctx))
    return _write_response(ctx, write, stored, sent=sent)


async def handle_replace(ctx: RequestContext) -> Response:
    """``PUT /things/{id}``: replace (or upsert) the record."""
    return await _modify(ctx, merge=False)


async def handle_update(ctx: RequestContext) -> Response:
    """``PATCH /things/{id}``: merge into the record."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    merge = endpoint.write.merge if endpoint.write else True
    return await _modify(ctx, merge=merge or ctx.request.scope.get("method") == "PATCH")


async def _modify(ctx: RequestContext, *, merge: bool) -> Response:
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.collection is not None  # noqa: S101
    write = endpoint.write or WriteSemantics(status=endpoint.success_status)
    record, problem = await _build_record(ctx)
    if problem is not None or record is None:
        return problem or ctx.error("invalid_body")
    sent = copy.deepcopy(ctx.json_body)
    collection = ctx.model.collection(endpoint.collection)
    key: JsonValue = path_key(ctx)
    if key is None:
        key = jsonpath.get_one(record, collection.key)
    existing = lookup(ctx, endpoint.collection, key)
    if existing is None and not write.upsert:
        return ctx.error("not_found", param=endpoint.key_param, value=key)
    if existing is not None:
        key = existing.key
    jsonpath.set_all(record, collection.key, lambda _old: key)
    if not write.persist:
        merged = {
            **(existing.record if existing and isinstance(existing.record, dict) else {}),
            **record,
        }
        stored = WorldRecord(key=key, record=merged if merge else record, recorded_at=_now(ctx))
        return _write_response(ctx, write, stored, sent=sent)
    if existing is None:
        stored = ctx.world.insert(
            endpoint.collection, {collection.key: key, **record}, recorded_at=_now(ctx)
        )
    elif merge:
        updated = ctx.world.update(endpoint.collection, key, record)
        assert updated is not None  # noqa: S101 - existence checked above
        stored = updated
    else:
        replaced = ctx.world.replace(endpoint.collection, key, record)
        assert replaced is not None  # noqa: S101
        stored = replaced
    return _write_response(ctx, write, stored, sent=sent)


async def handle_delete(ctx: RequestContext) -> Response:
    """``DELETE /things/{id}``."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.collection is not None  # noqa: S101
    write = endpoint.write or WriteSemantics(echo="empty", status=endpoint.success_status)
    key = path_key(ctx)
    existing = lookup(ctx, endpoint.collection, key)
    if existing is None:
        if endpoint.error_case("not_found") is not None or write.persist:
            return ctx.error("not_found", param=endpoint.key_param, value=key)
        existing = WorldRecord(key=key, record={}, recorded_at=_now(ctx))
    elif write.persist:
        ctx.world.delete(endpoint.collection, existing.key)
        ctx.runtime.drop_blob(ctx, endpoint.collection, existing.key)
    return _write_response(ctx, write, existing, sent=None)


# -- replay ----------------------------------------------------------------------------------------


def exchange_inputs(exchange: Exchange) -> dict[str, JsonValue]:
    """The inputs of a recorded request (path params, query, JSON body leaves)."""
    return {
        **path_inputs(exchange.request.path_params),
        **query_inputs(exchange.request.query),
        **json_inputs("body", exchange.request.body.parsed()),
    }


def request_inputs(ctx: RequestContext) -> dict[str, JsonValue]:
    """The same view of the current request.

    Path parameters are keyed by their spec names, which can differ from the routed identifiers
    (``pet-id`` is routed as ``pet_id``).
    """
    names = template_names(ctx.endpoint.path) if ctx.endpoint else list(ctx.request.path_params)
    path = {name: value for name in names if (value := ctx.path_param(name)) is not None}
    return {
        **path_inputs(path),
        **query_inputs(ctx.request.query_params.multi_items()),
        **json_inputs("body", ctx.json_body),
    }


async def choose_exchange(ctx: RequestContext) -> Exchange | None:
    """The recording that best answers this request."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    exchanges = [
        e for e in ctx.runtime.recordings.for_endpoint(endpoint.id) if e.response is not None
    ]
    if not exchanges:
        return None
    inputs = request_inputs(ctx)
    spec = endpoint.semantic.match if endpoint.semantic else None
    if spec is not None:
        inputs = {key: value for key, value in inputs.items() if key in spec.inputs}
    candidates = []
    for exchange in exchanges:
        recorded = exchange_inputs(exchange)
        if spec is not None:
            recorded = {key: value for key, value in recorded.items() if key in spec.inputs}
        candidates.append(Candidate(exchange.id, recorded))
    result = await match(inputs, candidates, spec, ctx.runtime.jev)
    if result.candidate is None:
        return None
    if result.method != "exact":
        log.info(
            "replay_matched", endpoint=endpoint.id, method=result.method, chosen=result.candidate.id
        )
    by_id = {exchange.id: exchange for exchange in exchanges}
    return by_id[result.candidate.id]


def replay(ctx: RequestContext, exchange: Exchange) -> Response:
    """Answer with a recorded response (body-scope policies applied, contract headers kept)."""
    response = exchange.response
    assert response is not None  # noqa: S101
    status = response.status
    content_type = response.header("content-type")
    body = response.body
    if body.kind == "json":
        rendered = ctx.render_body(body.parsed(), anchor_ns=exchange.started_ns)
        return ctx.respond(rendered, status=status, content_type=content_type)
    if body.kind == "binary":
        content = ctx.runtime.recordings.binary(exchange, body) or b""
        return ctx.respond_bytes(content, status=status, content_type=content_type)
    if body.kind == "empty":
        return ctx.empty(status)
    return ctx.respond_bytes((body.text or "").encode(), status=status, content_type=content_type)


async def handle_replay(ctx: RequestContext) -> Response:
    """Answer from recordings: exact match first, then the closest one."""
    exchange = await choose_exchange(ctx)
    if exchange is None:
        spec = ctx.endpoint.semantic.match if ctx.endpoint and ctx.endpoint.semantic else None
        if spec is not None and spec.no_match == "empty":
            return _empty_answer(ctx)
        if spec is not None and spec.no_match_template:
            return ctx.error("custom", template=spec.no_match_template)
        return ctx.error("not_found")
    return replay(ctx, exchange)


def _empty_answer(ctx: RequestContext) -> Response:
    """An empty answer in the endpoint's own shape: ``[]`` or an empty envelope, else 204."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    shape = endpoint.shape
    if shape.kind == "list":
        return ctx.respond([])
    if shape.kind == "envelope" and shape.template is not None:
        body = copy.deepcopy(shape.template)
        jsonpath.set_all(body, shape.items_path or "$.data", lambda _old: [])
        return ctx.respond(body)
    return ctx.empty(204)


async def handle_search(ctx: RequestContext) -> Response:
    """Search: recorded answers for recorded queries; the world (or closest recording) otherwise."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    inputs = request_inputs(ctx)
    for exchange in ctx.runtime.recordings.for_endpoint(endpoint.id):
        if exchange.response is not None and exchange_inputs(exchange) == inputs:
            return replay(ctx, exchange)
    if endpoint.collection is not None:
        return await handle_list(ctx)
    return await handle_replay(ctx)


async def handle_echo(ctx: RequestContext) -> Response:
    """Return the request body (body-scope policies applied)."""
    if ctx.json_error:
        return ctx.error("invalid_body")
    return ctx.respond(ctx.render_body(ctx.json_body))


# -- jobs ------------------------------------------------------------------------------------------


async def handle_job_submit(ctx: RequestContext) -> Response:
    """Create a job; its state then advances with the fake clock."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.job is not None  # noqa: S101
    job = ctx.model.job(endpoint.job)
    write = endpoint.write or WriteSemantics(id_strategy="uuid4", status=endpoint.success_status)
    record, problem = await _build_record(ctx)
    if problem is not None or record is None:
        return problem or ctx.error("invalid_body")
    key = _assign_key(ctx, job.collection, record, write)
    _apply_defaults(ctx, record, write, key)
    record[job.state_field] = job.states[0].name
    outcome = await ctx.runtime.decide_outcome(ctx, job)
    stored = ctx.world.insert(job.collection, record, recorded_at=_now(ctx))
    ctx.runtime.commit_blob(ctx, job.collection, stored.key)
    if outcome is not None:
        ctx.runtime.set_job_outcome(ctx.session, job.name, str(stored.key), outcome)
    shown = render_job(ctx, job, stored)
    headers = _location(ctx, write, stored.key)
    rendered = order_like(ctx.render_record(shown), _success_schema(ctx, write.status))
    return ctx.respond(rendered, status=write.status, headers=headers)


async def handle_job_status(ctx: RequestContext) -> Response:
    """A job's current state (same as ``get`` on the job collection)."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    assert endpoint.job is not None  # noqa: S101
    job = ctx.model.job(endpoint.job)
    key = path_key(ctx)
    record = lookup(ctx, job.collection, key)
    if record is None:
        return ctx.error("not_found", param=endpoint.key_param, value=key)
    return ctx.respond(ctx.render_record(render_job(ctx, job, record)))


# -- files -----------------------------------------------------------------------------------------


async def handle_download(ctx: RequestContext) -> Response:
    """Bytes uploaded in this session, else the recorded download."""
    endpoint = ctx.endpoint
    assert endpoint is not None  # noqa: S101
    key = path_key(ctx)
    if endpoint.collection is not None and key is not None:
        blob = ctx.runtime.blob(ctx, endpoint.collection, key)
        if blob is not None:
            headers = {}
            if blob.filename:
                headers["content-disposition"] = f'attachment; filename="{blob.filename}"'
            return ctx.respond_bytes(blob.content, content_type=blob.content_type, headers=headers)
    exchange = await choose_exchange(ctx)
    if exchange is None:
        return ctx.error("not_found", param=endpoint.key_param, value=key)
    return replay(ctx, exchange)


async def handle_upload(ctx: RequestContext) -> Response:
    """Multipart upload that creates a record (metadata) and keeps the bytes for downloads."""
    return await handle_create(ctx)


async def handle_not_implemented(ctx: RequestContext) -> Response:
    """Computed/custom endpoints must be implemented in the fake; make the gap obvious."""
    log.error("endpoint_not_implemented", endpoint=ctx.endpoint_id)
    body: JsonValue = {
        "error": "fauxcall_not_implemented",
        "message": f"{ctx.endpoint_id} needs a custom handler in this fake",
    }
    return ctx.respond(body, status=501, content_type="application/json")


ROLE_HANDLERS: Final[dict[str, Callable[[RequestContext], Awaitable[Response]]]] = {
    "get": handle_get,
    "list": handle_list,
    "search": handle_search,
    "create": handle_create,
    "replace": handle_replace,
    "update": handle_update,
    "delete": handle_delete,
    "replay": handle_replay,
    "echo": handle_echo,
    "job_submit": handle_job_submit,
    "job_status": handle_job_status,
    "download": handle_download,
    "upload": handle_upload,
    "computed": handle_not_implemented,
    "custom": handle_not_implemented,
}
