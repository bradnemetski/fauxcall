"""Generic role handlers at their edges: keys, bodies, envelopes, upserts, replay, jobs, files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from _engine import harness, j, market_model, recorded, save_exchange, with_endpoints
from fauxcall.model import Body, CapturedResponse, ErrorTemplate

if TYPE_CHECKING:
    from pathlib import Path

JSON = {"content-type": "application/json"}


async def test_keys_from_the_last_path_parameter_and_unparseable_keys(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {"id": "GET /v1/lists/{id}", "role": "get", "collection": "watchlists"},  # no key_param
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        made = (await client.post("/v1/watchlists", json={"name": "Tech"})).json()
        assert (await client.get(f"/v1/lists/{made['id']}")).json()["name"] == "Tech"
        # Integer-keyed collection: a key that isn't a number simply isn't found.
        assert (await client.get("/v1/watchlists/not-a-number")).status_code == 404


async def test_create_body_validation(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "POST /v1/strict",
            "role": "create",
            "collection": "watchlists",
            "request_body": {"required": True},
        },
        {"id": "POST /v1/loose", "role": "create", "collection": "watchlists"},
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.post("/v1/strict")).status_code == 400  # required body missing
        assert (await client.post("/v1/loose")).status_code == 200  # optional: {} is fine
        not_object = await client.post("/v1/loose", content=b"[1, 2]", headers=JSON)
        assert not_object.status_code == 400
        broken = await client.post("/v1/loose", content=b"{", headers=JSON)
        assert broken.status_code == 400


async def test_get_and_list_render_job_state(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {"id": "GET /v1/jobs", "role": "list", "collection": "uploads"},
        {"id": "GET /v1/jobs/{id}", "role": "get", "collection": "uploads", "key_param": "id"},
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        job_id = (await client.post("/v1/uploads", json={"name": "a.csv"})).json()["id"]
        assert (await client.get(f"/v1/jobs/{job_id}")).json()["status"] == "queued"
        await client.post("/__fauxcall/clock", json={"op": "advance", "seconds": 600})
        listed = (await client.get("/v1/jobs")).json()
        assert [item["status"] for item in listed] == ["done"]
        assert (await client.get(f"/v1/jobs/{job_id}")).json()["status"] == "done"


async def test_envelope_fields_without_and_with_pagination(tmp_path: Path) -> None:
    template = {"data": [], "meta": {"count": None, "next": None, "more": None}}
    model = with_endpoints(
        market_model(),
        {
            "id": "GET /v1/all-exchanges",
            "role": "list",
            "collection": "exchanges",
            "shape": {"kind": "envelope", "template": template, "items_path": "$.data"},
        },
        {
            "id": "GET /v1/paged-exchanges",
            "role": "list",
            "collection": "exchanges",
            "shape": {"kind": "envelope", "template": template, "items_path": "$.data"},
            "pagination": {
                "style": "offset",
                "limit_param": "limit",
                "offset_param": "offset",
                "default_limit": 1,
                "total_path": "$.meta.count",
                "next_path": "$.meta.next",
                "has_more_path": "$.meta.more",
            },
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        plain = (await client.get("/v1/all-exchanges")).json()
        assert [e["code"] for e in plain["data"]] == ["XNYS", "XLON"]
        assert plain["meta"] == {"count": None, "next": None, "more": None}  # left as recorded
        paged = (await client.get("/v1/paged-exchanges")).json()
        assert [e["code"] for e in paged["data"]] == ["XNYS"]
        assert paged["meta"]["count"] == 2
        assert paged["meta"]["more"] is True


async def test_replace_without_a_path_key_upserts_and_validates(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "PUT /v1/watchlists",
            "role": "replace",
            "collection": "watchlists",
            "write": {"upsert": True},
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        created = await client.put("/v1/watchlists", json={"id": 4242, "name": "Upserted"})
        assert created.status_code == 200
        assert (await client.get("/v1/watchlists/4242")).json()["name"] == "Upserted"
        replaced = await client.put("/v1/watchlists", json={"id": 4242, "name": "Again"})
        assert replaced.json()["name"] == "Again"
        bad = await client.put("/v1/watchlists/4242", content=b"{", headers=JSON)
        assert bad.status_code == 400
        missing = await client.put("/v1/watchlists/9999", json={"name": "nobody"})
        assert missing.status_code == 404  # no upsert on this endpoint


async def test_deleting_an_unknown_record_without_persistence(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "DELETE /v1/scratch/{id}",
            "role": "delete",
            "collection": "watchlists",
            "key_param": "id",
            "write": {"persist": False, "echo": "empty", "status": 204},
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.delete("/v1/scratch/123")).status_code == 204
        assert (await client.delete("/v1/watchlists/123")).status_code == 404


async def test_replay_of_binary_empty_and_text_bodies(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {"id": "GET /v1/logo", "role": "replay"},
        {"id": "GET /v1/nothing", "role": "replay"},
        {"id": "GET /v1/motd", "role": "replay"},
    )
    fake = harness(tmp_path, model=model)
    png = CapturedResponse(
        status=200,
        headers=[("content-type", "image/png")],
        body=Body(kind="binary", size=4, sha256="0" * 64, file="missing.bin"),
    )
    save_exchange(tmp_path, recorded("GET /v1/logo", response=png))
    save_exchange(tmp_path, recorded("GET /v1/nothing", response=CapturedResponse(status=204)))
    text = CapturedResponse(
        status=200, headers=[("content-type", "text/plain")], body=Body(kind="text", text="hi")
    )
    save_exchange(tmp_path, recorded("GET /v1/motd", response=text))
    async with fake.client() as client:
        logo = await client.get("/v1/logo")
        assert (logo.status_code, logo.headers["content-type"]) == (200, "image/png")
        assert (await client.get("/v1/nothing")).status_code == 204
        motd = await client.get("/v1/motd")
        assert (motd.text, motd.headers["content-type"]) == ("hi", "text/plain")


async def test_search_falls_back_to_the_collection(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "GET /v1/find",
            "role": "search",
            "collection": "exchanges",
            "params": [{"name": "code", "location": "query", "maps_to": "code", "operator": "eq"}],
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        found = (await client.get("/v1/find", params={"code": "XLON"})).json()
        assert [e["code"] for e in found] == ["XLON"]


async def test_job_submit_rejects_bad_bodies(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        bad = await client.post("/v1/uploads", content=b"{", headers=JSON)
        assert bad.status_code == 400


async def test_downloads_uploads_and_unimplemented_endpoints(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {
            "id": "POST /v1/files",
            "role": "upload",
            "collection": "uploads",
            "write": {"id_strategy": "uuid4", "status": 201},
        },
        {
            "id": "GET /v1/files/{id}",
            "role": "download",
            "collection": "uploads",
            "key_param": "id",
        },
        {"id": "GET /v1/reports/{id}", "role": "download", "key_param": "id"},
        {"id": "GET /v1/convert", "role": "computed"},
    )
    model.errors.append(ErrorTemplate(name="unused", status=418))
    fake = harness(tmp_path, model=model)
    pdf = CapturedResponse(
        status=200,
        headers=[("content-type", "application/pdf")],
        body=Body(kind="text", text="%PDF"),
    )
    save_exchange(tmp_path, recorded("GET /v1/reports/{id}", response=pdf))
    async with fake.client() as client:
        multipart = (
            b"--b0undary\r\n"
            b'Content-Disposition: form-data; name="file"; filename=""\r\n'
            b"\r\n"
            b"raw bytes\r\n"
            b"--b0undary--\r\n"
        )
        uploaded = await client.post(
            "/v1/files",
            content=multipart,
            headers={"content-type": "multipart/form-data; boundary=b0undary"},
        )
        assert uploaded.status_code == 201
        file_id = uploaded.json()["id"]
        download = await client.get(f"/v1/files/{file_id}")
        assert download.content == b"raw bytes"
        assert "content-disposition" not in download.headers  # no file name was given
        report = await client.get("/v1/reports/7")
        assert report.text == "%PDF"  # recorded download
        assert (await client.get(f"/v1/files/{file_id}0")).status_code == 404
        missing = await client.get("/v1/convert")
        assert missing.status_code == 501
        assert j(missing.json())["error"] == "fauxcall_not_implemented"


async def test_model_world_mismatches_answer_a_structured_500(tmp_path: Path) -> None:
    model = with_endpoints(
        market_model(),
        {"id": "POST /v1/broken", "role": "create", "collection": "uploads"},  # uuid keys
    )
    assert any("can't make the uuid keys" in problem for problem in model.problems())
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        broken = await client.post("/v1/broken", json={"name": "x"})
    assert broken.status_code == 500
    assert j(broken.json())["error"] == "fauxcall_error"


async def test_page_fields_are_optional_and_unpersisted_deletes_keep_records(
    tmp_path: Path,
) -> None:
    template: dict[str, object] = {"items": [], "more": None}
    model = with_endpoints(
        market_model(),
        {
            "id": "GET /v1/some-exchanges",
            "role": "list",
            "collection": "exchanges",
            "shape": {"kind": "envelope", "template": template, "items_path": "$.items"},
            "pagination": {
                "style": "offset",
                "limit_param": "limit",
                "offset_param": "offset",
                "default_limit": 5,
                "has_more_path": "$.more",
            },
        },
        {
            "id": "DELETE /v1/pretend/{id}",
            "role": "delete",
            "collection": "watchlists",
            "key_param": "id",
            "write": {"persist": False, "echo": "empty", "status": 204},
        },
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        page = (await client.get("/v1/some-exchanges")).json()
        assert page == {"items": page["items"], "more": False}
        made = (await client.post("/v1/watchlists", json={"name": "Keep"})).json()
        assert (await client.delete(f"/v1/pretend/{made['id']}")).status_code == 204
        assert (await client.get(f"/v1/watchlists/{made['id']}")).status_code == 200


async def test_no_match_empty_answers_in_the_endpoint_shape(tmp_path: Path) -> None:
    empty_match = {"match": {"inputs": ["query.q"], "fallback": "none", "no_match": "empty"}}
    envelope = {"results": [], "count": 0}
    model = with_endpoints(
        market_model(),
        {
            "id": "GET /v1/find-list",
            "role": "replay",
            "shape": {"kind": "list"},
            "semantic": empty_match,
        },
        {
            "id": "GET /v1/find-envelope",
            "role": "replay",
            "shape": {"kind": "envelope", "template": envelope, "items_path": "$.results"},
            "semantic": empty_match,
        },
        {"id": "GET /v1/find-one", "role": "replay", "semantic": empty_match},
    )
    fake = harness(tmp_path, model=model)
    async with fake.client() as client:
        assert (await client.get("/v1/find-list", params={"q": "x"})).json() == []
        assert (await client.get("/v1/find-envelope", params={"q": "x"})).json() == envelope
        assert (await client.get("/v1/find-one", params={"q": "x"})).status_code == 204
