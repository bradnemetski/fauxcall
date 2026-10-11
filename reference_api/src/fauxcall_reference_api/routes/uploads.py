"""Uploads: multipart files processed as asynchronous jobs whose status derives from time."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Depends, File, Form, Path, Response, UploadFile

from fauxcall_reference_api.errors import ApiError
from fauxcall_reference_api.routes.common import Ctx, document_api_key, error_responses
from fauxcall_reference_api.schemas import UploadOut
from fauxcall_reference_api.views import upload_out

if TYPE_CHECKING:
    from fauxcall_reference_api.context import AppContext
    from fauxcall_reference_api.state import Upload

MAX_UPLOAD_BYTES: Final = 1_048_576
DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"
_PRINTABLE: Final = frozenset(chr(code) for code in range(0x20, 0x7F)) - {'"', "\\"}

router = APIRouter(
    prefix="/v1",
    tags=["uploads"],
    dependencies=[Depends(document_api_key)],
    responses=error_responses(401, 403, 429),
)


def content_disposition(filename: str) -> str:
    """``attachment; filename="<name>"``; non-printable-ASCII, quote and backslash become ``_``."""
    safe = "".join(char if char in _PRINTABLE else "_" for char in filename)
    return f'attachment; filename="{safe}"'


def _upload_or_404(context: AppContext, upload_id: str) -> Upload:
    upload = context.uploads.get(upload_id)
    if upload is None:
        raise ApiError(404, "upload_not_found", f"No upload with id '{upload_id}'.")
    return upload


@router.post(
    "/uploads",
    status_code=202,
    summary="Upload a file",
    response_model_exclude_unset=True,
    responses=error_responses(400, 413),
)
async def create_upload(
    ctx: Ctx,
    file: Annotated[UploadFile, File(description="The file (at most 1 MiB).")],
    label: Annotated[str | None, Form(description="Optional free-text label.")] = None,
) -> UploadOut:
    """Store a file and start processing it: ``queued`` 2 s, ``processing`` 6 s, then terminal.

    Files containing the ASCII text ``CORRUPT`` end ``failed`` (``corrupt_file``), others ``done``.
    """
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise ApiError(
            413, "payload_too_large", f"File exceeds the 1 MiB ({MAX_UPLOAD_BYTES} bytes) limit."
        )
    now_ns = ctx.now_ns()
    upload = ctx.uploads.create(
        filename=file.filename or "",
        content_type=file.content_type or DEFAULT_CONTENT_TYPE,
        content=content,
        label=label,
        now_ns=now_ns,
    )
    return upload_out(upload, now_ns)


@router.get("/uploads", summary="List uploads", response_model_exclude_unset=True)
async def list_uploads(ctx: Ctx) -> list[UploadOut]:
    """Every upload, as a bare array, newest first."""
    now_ns = ctx.now_ns()
    return [upload_out(upload, now_ns) for upload in ctx.uploads.all()]


@router.get(
    "/uploads/{id}",
    summary="Get an upload",
    response_model_exclude_unset=True,
    responses=error_responses(404),
)
async def get_upload(
    upload_id: Annotated[str, Path(alias="id", description="Upload id (UUID4).")], ctx: Ctx
) -> UploadOut:
    """An upload's current status, derived from the time elapsed since it was created."""
    return upload_out(_upload_or_404(ctx, upload_id), ctx.now_ns())


@router.get(
    "/uploads/{id}/content",
    summary="Download an upload",
    response_class=Response,
    responses={
        200: {
            "description": "The original bytes, with the original content type.",
            "content": {"application/octet-stream": {"schema": {"type": "string"}}},
        },
        **error_responses(404, 409),
    },
)
async def get_upload_content(
    upload_id: Annotated[str, Path(alias="id", description="Upload id (UUID4).")], ctx: Ctx
) -> Response:
    """The original bytes, only once the upload is ``done`` (otherwise 409 ``not_ready``)."""
    upload = _upload_or_404(ctx, upload_id)
    status = upload.view(ctx.now_ns()).status
    if status != "done":
        raise ApiError(
            409,
            "not_ready",
            f"Upload '{upload.id}' is {status}; content is available only when status is done.",
        )
    return Response(
        content=upload.content,
        headers={
            "Content-Type": upload.content_type,
            "Content-Disposition": content_disposition(upload.filename),
        },
    )


@router.delete(
    "/uploads/{id}",
    status_code=204,
    summary="Delete an upload",
    response_class=Response,
    responses=error_responses(404),
)
async def delete_upload(
    upload_id: Annotated[str, Path(alias="id", description="Upload id (UUID4).")], ctx: Ctx
) -> Response:
    """Delete an upload (204, empty body)."""
    if not ctx.uploads.delete(upload_id):
        raise ApiError(404, "upload_not_found", f"No upload with id '{upload_id}'.")
    return Response(status_code=204)
