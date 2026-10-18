"""How the real server checks what a client answers its requests (the SDK's result schemas).

A valid answer comes back the way zod returns it: known members in the schema's order, unknown
ones after them (results are loose objects) or dropped (content blocks are strict). An invalid
one raises :class:`InvalidAnswerError` with zod's message, which the tool then reports: unions
list every alternative's issues, discriminated unions name the options (recordings 000473-000479).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall_everything import zod

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fauxcall.types import JsonObject, JsonValue

ROLES: Final = ("user", "assistant")
ACTIONS: Final = ("accept", "decline", "cancel")
STOP_REASONS: Final = ("endTurn", "stopSequence", "maxTokens")
_CONTENT_FIELDS: Final = {
    "text": ("type", "text", "annotations", "_meta"),
    "image": ("type", "data", "mimeType", "annotations", "_meta"),
    "audio": ("type", "data", "mimeType", "annotations", "_meta"),
}
_REQUIRED_STRINGS: Final = {
    "text": ("text",),
    "image": ("data", "mimeType"),
    "audio": ("data", "mimeType"),
}

type FieldSpec = tuple[str, str, bool]
"""A member of an object schema: name, ``string``/``number``/``any``, required."""

_TASK_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("taskId", "string", True),
    ("status", "string", True),
    ("pollInterval", "number", False),
    ("statusMessage", "string", False),
)
"""The async tools' ``task``: ``z.object({taskId, status, pollInterval?, statusMessage?})``."""


class InvalidAnswerError(ValueError):
    """The client's answer breaks the result schema; the message is zod's."""


def _refuse(issues: Sequence[zod.Issue]) -> InvalidAnswerError:
    return InvalidAnswerError(zod.issues_text(issues))


def _loose(value: JsonObject, known: dict[str, JsonValue]) -> JsonObject:
    """Known members (in schema order) first, then the client's other members."""
    out: JsonObject = {}
    if "_meta" in value:
        out["_meta"] = value["_meta"]
    out.update(known)
    out.update({key: item for key, item in value.items() if key not in out})
    return out


def _wrong(kind: str, item: JsonValue) -> bool:
    if kind == "string":
        return not isinstance(item, str)
    if kind == "number":
        return isinstance(item, bool) or not isinstance(item, int | float)
    return False


def _strict(
    value: JsonValue, fields: Sequence[FieldSpec], path: zod.Path
) -> tuple[JsonObject, list[zod.Issue]]:
    """``z.object(shape)``: the members it knows, in shape order, and their issues."""
    if not isinstance(value, dict):
        return {}, [zod.wrong_type("object", value, path)]
    out: JsonObject = {}
    issues: list[zod.Issue] = []
    for name, kind, needed in fields:
        if name not in value:
            if needed and kind == "any":
                issues.append(zod.required([*path, name]))
            elif needed:
                issues.append(zod.wrong_type(kind, zod.MISSING, [*path, name]))
        elif _wrong(kind, value[name]):
            issues.append(zod.wrong_type(kind, value[name], [*path, name]))
        else:
            out[name] = value[name]
    return out, issues


def _content(value: JsonValue, path: zod.Path) -> tuple[JsonValue, list[zod.Issue]]:
    """``SamplingContentSchema``: a text, image or audio block (discriminated by ``type``)."""
    if not isinstance(value, dict):
        return value, [zod.not_an_object(value, path)]
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in _CONTENT_FIELDS:
        return value, [zod.no_discriminator(tuple(_CONTENT_FIELDS), [*path, "type"])]
    issues = [
        zod.wrong_type("string", value.get(name, zod.MISSING), [*path, name])
        for name in _REQUIRED_STRINGS[kind]
        if not isinstance(value.get(name), str)
    ]
    block = {name: value[name] for name in _CONTENT_FIELDS[kind] if name in value}
    return block, issues


def create_message_result(answer: JsonValue) -> JsonObject:
    """``CreateMessageResultSchema``: ``model``, ``stopReason``, ``role``, ``content``."""
    if not isinstance(answer, dict):
        raise _refuse([zod.wrong_type("object", answer, [])])
    issues: list[zod.Issue] = []
    known: JsonObject = {}
    model = answer.get("model", zod.MISSING)
    if isinstance(model, str):
        known["model"] = model
    else:
        issues.append(zod.wrong_type("string", model, ["model"]))
    if "stopReason" in answer:  # z.enum([...]).or(z.string()), optional
        stop = answer["stopReason"]
        if isinstance(stop, str):
            known["stopReason"] = stop
        else:
            alternatives = [
                [zod.not_one_of(STOP_REASONS, [])],
                [zod.wrong_type("string", stop, [])],
            ]
            issues.append(zod.union(alternatives, ["stopReason"]))
    role = answer.get("role", zod.MISSING)
    if isinstance(role, str) and role in ROLES:
        known["role"] = role
    else:
        issues.append(zod.not_one_of(ROLES, ["role"]))
    content, problems = _content(answer.get("content", zod.MISSING), ["content"])  # type: ignore[arg-type]
    issues += problems
    known["content"] = content
    if issues:
        raise _refuse(issues)
    return _loose(answer, known)


def _flat_value(item: JsonValue, path: zod.Path) -> zod.Issue | None:
    """An elicited value: a string, a number, a boolean or an array of strings (a ``z.union``)."""
    finite = isinstance(item, int | float) and zod.received(item) == "number"
    if isinstance(item, str | bool) or finite:
        return None
    if isinstance(item, list):
        as_array = [
            zod.wrong_type("string", element, [index])
            for index, element in enumerate(item)
            if not isinstance(element, str)
        ]
        if not as_array:
            return None
    else:
        as_array = [zod.wrong_type("array", item, [])]
    alternatives = [
        [zod.wrong_type("string", item, [])],
        [zod.wrong_type("number", item, [])],
        [zod.wrong_type("boolean", item, [])],
        as_array,
    ]
    return zod.union(alternatives, path)


def elicit_result(answer: JsonValue) -> JsonObject:
    """``ElicitResultSchema``: ``action`` and an optional flat ``content`` record."""
    if not isinstance(answer, dict):
        raise _refuse([zod.wrong_type("object", answer, [])])
    issues: list[zod.Issue] = []
    known: JsonObject = {}
    action = answer.get("action", zod.MISSING)
    if isinstance(action, str) and action in ACTIONS:
        known["action"] = action
    else:
        issues.append(zod.not_one_of(ACTIONS, ["action"]))
    content = answer.get("content")
    if content is not None:
        if not isinstance(content, dict):
            issues.append(zod.wrong_type("record", content, ["content"]))
        else:
            issues += [
                issue
                for key, item in content.items()
                if (issue := _flat_value(item, ["content", key])) is not None
            ]
            known["content"] = content
    if issues:
        raise _refuse(issues)
    out = _loose(answer, known)
    if "content" in answer and answer["content"] is None:
        del out["content"]  # null is read as absent
    return out


def list_roots_result(answer: JsonValue) -> list[JsonValue]:
    """``ListRootsResultSchema``: roots, each a ``file://`` URI with an optional name."""
    if not isinstance(answer, dict):
        raise _refuse([zod.wrong_type("object", answer, [])])
    roots = answer.get("roots", zod.MISSING)
    if not isinstance(roots, list):
        raise _refuse([zod.wrong_type("array", roots, ["roots"])])
    issues: list[zod.Issue] = []
    parsed: list[JsonValue] = []
    for index, root in enumerate(roots):
        if not isinstance(root, dict):
            issues.append(zod.wrong_type("object", root, ["roots", index]))
            continue
        uri = root.get("uri", zod.MISSING)
        if not isinstance(uri, str):
            issues.append(zod.wrong_type("string", uri, ["roots", index, "uri"]))
        elif not uri.startswith("file://"):
            issues.append(
                zod.Issue(
                    {
                        "origin": "string",
                        "code": "invalid_format",
                        "format": "starts_with",
                        "prefix": "file://",
                    },
                    ["roots", index, "uri"],
                    'Invalid string: must start with "file://"',
                )
            )
        name = root.get("name")
        if "name" in root and not isinstance(name, str):
            issues.append(zod.wrong_type("string", name, ["roots", index, "name"]))
        parsed.append({key: root[key] for key in ("uri", "name", "_meta") if key in root})
    if issues:
        raise _refuse(issues)
    return parsed


def task_or(answer: JsonValue, sync_fields: Sequence[FieldSpec]) -> JsonObject:
    """The async tools' ``z.union([z.object({task}), z.object(direct result)])``.

    The first alternative that fits wins, with only the members it knows; when neither does,
    zod lists both alternatives' issues (recordings 000478, 000479).
    """
    if not isinstance(answer, dict):
        raise _refuse([zod.union([[zod.wrong_type("object", answer, [])]] * 2, [])])
    if "task" in answer:
        task, task_issues = _strict(answer["task"], _TASK_FIELDS, ["task"])
        if not task_issues:
            return {"task": task}
    else:
        task_issues = [zod.wrong_type("object", zod.MISSING, ["task"])]
    direct, direct_issues = _strict(answer, sync_fields, [])
    if not direct_issues:
        return direct
    raise _refuse([zod.union([task_issues, direct_issues], [])])


def poll_result(answer: JsonValue) -> JsonObject:
    """``z.looseObject({status: z.string(), statusMessage: z.string().optional()})``."""
    if not isinstance(answer, dict):
        raise _refuse([zod.wrong_type("object", answer, [])])
    issues: list[zod.Issue] = []
    status = answer.get("status", zod.MISSING)
    if not isinstance(status, str):
        issues.append(zod.wrong_type("string", status, ["status"]))
    if "statusMessage" in answer and not isinstance(answer["statusMessage"], str):
        issues.append(zod.wrong_type("string", answer["statusMessage"], ["statusMessage"]))
    if issues:
        raise _refuse(issues)
    return answer
