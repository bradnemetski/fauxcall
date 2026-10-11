from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from _engine import API_KEY, FRIDAY_CLOSE, OPEN_NS, WEEKEND_NS, harness, j, market_model
from fauxcall.config import FaultRule
from fauxcall.model.realism import LatencyProfile
from fauxcall.runtime import FakeRuntime
from fauxcall.timefmt import NS_PER_SECOND, parse_iso

if TYPE_CHECKING:
    from pathlib import Path


async def test_list_and_get_with_real_404(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        listing = await client.get("/v1/exchanges")
        assert listing.status_code == 200
        assert listing.headers["content-type"].startswith("application/json")
        assert [e["code"] for e in listing.json()] == ["XNYS", "XLON"]
        assert listing.text.startswith('[{"code":"XNYS"')  # compact style, recorded key order
        found = await client.get("/v1/exchanges/XLON")
        assert found.json()["name"] == "London Stock Exchange"
        missing = await client.get("/v1/exchanges/XXXX")
        assert missing.status_code == 404
        error = missing.json()["error"]
        assert error["code"] == "not_found"
        assert error["message"] == "code XXXX"
        assert len(error["request_id"]) == 36


async def test_envelope_pagination_with_real_cursor_spelling_and_filters(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        first = (await client.get("/v1/tickers")).json()
        assert [t["symbol"] for t in first["data"]] == ["AAPL", "IBM"]
        assert first["total"] == 4
        cursor = first["next_cursor"]
        assert json.loads(base64.urlsafe_b64decode(cursor)) == {"after": "IBM"}
        second = (await client.get("/v1/tickers", params={"cursor": cursor})).json()
        assert [t["symbol"] for t in second["data"]] == ["MSFT", "VOD"]
        assert second["next_cursor"] is None
        filtered = (await client.get("/v1/tickers", params={"exchange": "XLON"})).json()
        assert [t["symbol"] for t in filtered["data"]] == ["VOD"]
        assert filtered["total"] == 1
        big = (await client.get("/v1/tickers", params={"limit": "100"})).json()
        assert len(big["data"]) == 4
        bad = await client.get("/v1/tickers", params={"limit": "abc"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "invalid_parameter"
        out_of_range = await client.get("/v1/tickers", params={"limit": "0"})
        assert out_of_range.status_code == 400
        garbage = await client.get("/v1/tickers", params={"cursor": "not-a-cursor"})
        assert garbage.status_code == 400
    # Floats keep their recorded spelling.
    async with fake.client() as client:
        assert '"price":227.50' in (await client.get("/v1/tickers")).text


async def test_auth_missing_unknown_denied_and_exempt(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client(key=None) as anonymous:
        response = await anonymous.get("/v1/exchanges")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert (await anonymous.get("/v1/time")).status_code == 200  # auth_required: false
    async with fake.client(key="any-well-formed-key") as stranger:
        assert (await stranger.get("/v1/exchanges")).status_code == 200  # mode "any"
    async with fake.client(key="denied-key") as mallory:
        denied = await mallory.get("/v1/exchanges")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "forbidden"
    strict = harness(
        tmp_path / "strict",
        auth={"mode": "allowlist", "users": [{"name": "alice", "api_key": API_KEY}]},
    )
    async with strict.client(key="someone-else") as client:
        assert (await client.get("/v1/exchanges")).status_code == 401
    async with strict.client() as client:
        assert (await client.get("/v1/exchanges")).status_code == 200


async def test_rate_limit_headers_429_and_fast_mode(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        responses = [await client.get("/v1/exchanges") for _ in range(6)]
        assert [r.status_code for r in responses] == [200, 200, 200, 200, 200, 429]
        response = responses[-1]
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert int(response.headers["retry-after"]) >= 1
        assert response.json()["error"]["code"] == "rate_limited"
        frozen = await client.post("/__fauxcall/clock", json={"op": "freeze"})
        assert frozen.status_code == 200
        assert (await client.get("/v1/exchanges")).status_code == 429  # setting the clock: no
        advanced = await client.post("/__fauxcall/clock", json={"op": "advance", "seconds": 60})
        assert advanced.status_code == 200
        assert (await client.get("/v1/exchanges")).status_code == 200  # time passed: lifted
    fast = harness(tmp_path / "fast", fast=True)
    async with fast.client() as client:
        assert all([(await client.get("/v1/exchanges")).status_code == 200 for _ in range(8)])


async def test_crud_location_ids_sessions_and_reset(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client(session="a") as a, fake.client(session="b") as b:
        created = await a.post("/v1/watchlists", json={"name": "Tech", "symbols": ["AAPL"]})
        assert created.status_code == 201
        assert created.headers["location"] == "/v1/watchlists/1001"
        body = created.json()
        assert list(body)[:2] == ["id", "name"]
        assert body["id"] == 1001
        assert body["created_at"].endswith("Z")
        second = await a.post("/v1/watchlists", json={"name": "Banks"})
        assert second.json()["id"] == 1002
        assert second.json()["symbols"] == []  # server default
        assert (await a.get("/v1/watchlists/1001")).json()["name"] == "Tech"
        assert (await b.get("/v1/watchlists/1001")).status_code == 404  # session isolation
        replaced = await a.put("/v1/watchlists/1001", json={"name": "Tech 2"})
        assert (
            replaced.json() == {"name": "Tech 2", "id": 1001} or replaced.json()["name"] == "Tech 2"
        )
        patched = await a.patch("/v1/watchlists/1002", json={"symbols": ["JPM"]})
        assert patched.json()["name"] == "Banks"
        assert patched.json()["symbols"] == ["JPM"]
        assert (await a.put("/v1/watchlists/9999", json={"name": "x"})).status_code == 404
        deleted = await a.delete("/v1/watchlists/1001")
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert (await a.delete("/v1/watchlists/1001")).status_code == 404
        invalid = await a.post("/v1/watchlists", json={"symbols": []})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_body"
        broken = await a.post(
            "/v1/watchlists", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert broken.status_code == 400
        reset = await a.post("/__fauxcall/reset", json={"session": "a"})
        assert reset.status_code == 200
        assert (await a.get("/v1/watchlists/1002")).status_code == 404


async def test_async_job_progresses_with_the_clock_and_download(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        submitted = await client.post(
            "/v1/uploads",
            files={"file": ("report.csv", b"a,b\n1,2\n", "text/csv")},
            data={"label": "q3"},
        )
        assert submitted.status_code == 202
        job = submitted.json()
        assert job["status"] == "queued"
        assert job["filename"] == "report.csv"
        assert job["size"] == 8
        assert job["label"] == "q3"
        job_id = job["id"]
        await client.post("/__fauxcall/clock", json={"op": "freeze"})
        fake.base.advance(5)  # frozen: base time passing changes nothing
        assert (await client.get(f"/v1/uploads/{job_id}")).json()["status"] == "queued"
        await client.post("/__fauxcall/clock", json={"op": "advance", "seconds": 5})
        processing = (await client.get(f"/v1/uploads/{job_id}")).json()
        assert processing["status"] == "processing"
        assert processing["progress"] == 50
        assert processing["completed_at"] is None
        await client.post("/__fauxcall/clock", json={"op": "advance", "seconds": 10})
        done = (await client.get(f"/v1/uploads/{job_id}")).json()
        assert done["status"] == "done"
        assert done["progress"] == 100
        assert done["completed_at"] is not None
        download = await client.get(f"/v1/uploads/{job_id}/content")
        assert download.content == b"a,b\n1,2\n"
        assert download.headers["content-disposition"] == 'attachment; filename="report.csv"'
        assert (
            await client.get("/v1/uploads/00000000-0000-4000-8000-000000000000")
        ).status_code == 404


async def test_now_policy_tracks_the_clock_and_freezes(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client(key=None) as client:
        body = (await client.get("/v1/time")).json()
        assert parse_iso(body["utc"]) == OPEN_NS
        assert len(body["utc"].split(".")[1]) == 8  # 7 digits + "Z"
        assert body["unix_ms"] == OPEN_NS // 1_000_000
        assert body["zone"] == "UTC"  # untouched static field
        fake.base.advance(30)
        later = (await client.get("/v1/time")).json()
        assert parse_iso(later["utc"]) == OPEN_NS + 30 * NS_PER_SECOND
        assert later["request_id"] != body["request_id"]
        await client.post("/__fauxcall/clock", json={"op": "travel", "at": "2030-01-01T00:00:00Z"})
        assert (await client.get("/v1/time")).json()["utc"].startswith("2030-01-01T00:00:00")


async def test_calendar_policy_live_when_open_frozen_when_closed(tmp_path: Path) -> None:
    open_fake = harness(tmp_path / "open")
    async with open_fake.client() as client:
        quote = (await client.get("/v1/tickers/AAPL/quote")).json()
        assert list(quote) == ["symbol", "price", "as_of"]  # projection
        assert parse_iso(quote["as_of"]) == OPEN_NS - 1_500_000_000  # now - 1.5 s lag
    closed = harness(tmp_path / "closed", start_ns=WEEKEND_NS)
    async with closed.client() as client:
        assert (await client.get("/v1/tickers/AAPL/quote")).json()["as_of"] == FRIDAY_CLOSE
        assert (await client.get("/v1/tickers/NOPE/quote")).status_code == 404


async def test_search_exact_replay_then_closest(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        exact = (await client.get("/v1/search", params={"q": "big blue"})).json()
        assert exact["results"] == [{"symbol": "IBM"}]
        close = (await client.get("/v1/search", params={"q": "apple inc"})).json()
        assert close["results"] == [{"symbol": "AAPL"}]


async def test_faults_injected_through_admin(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client() as client:
        rules = [
            FaultRule(endpoint="GET /v1/exchanges", kind="error", status=503, first_n=1).model_dump(
                mode="json"
            )
        ]
        assert (await client.put("/__fauxcall/faults", json=rules)).status_code == 200
        first = await client.get("/v1/exchanges")
        assert first.status_code == 503
        assert (await client.get("/v1/exchanges")).status_code == 200  # only the first call
        delay = [
            FaultRule(endpoint="GET /v1/exchanges/*", kind="delay", delay_ms=750).model_dump(
                mode="json"
            )
        ]
        await client.put("/__fauxcall/faults", json=delay)
        assert (await client.get("/v1/exchanges/XNYS")).status_code == 200
        assert 0.75 in fake.sleeps
        await client.put(
            "/__fauxcall/faults",
            json=[FaultRule(endpoint="*", kind="rate_limit").model_dump(mode="json")],
        )
        limited = await client.get("/v1/exchanges/XNYS")
        assert limited.status_code == 429
        await client.put(
            "/__fauxcall/faults",
            json=[FaultRule(endpoint="*", kind="timeout", delay_ms=100).model_dump(mode="json")],
        )
        assert (await client.get("/v1/exchanges/XNYS")).status_code == 504
        assert (await client.get("/__fauxcall/faults")).json()["faults"][0]["kind"] == "timeout"
        assert (await client.delete("/__fauxcall/faults")).json() == {"faults": []}


async def test_unknown_routes_methods_and_journal(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client(session="eval-1") as client:
        unknown = await client.get("/nope")
        assert unknown.status_code == 404
        assert unknown.text == "Not Found"
        assert (await client.delete("/v1/exchanges")).status_code == 405
        await client.get("/v1/exchanges/XNYS")
        journal = (await client.get("/__fauxcall/journal", params={"session": "eval-1"})).json()[
            "entries"
        ]
        assert [entry["path"] for entry in journal] == [
            "/nope",
            "/v1/exchanges",
            "/v1/exchanges/XNYS",
        ]
        assert journal[-1]["endpoint"] == "GET /v1/exchanges/{code}"
        assert journal[-1]["request_headers"] == [[k, v] for k, v in journal[-1]["request_headers"]]
        assert any(
            value == "[REDACTED]"
            for key, value in journal[-1]["request_headers"]
            if key == "x-api-key"
        )
        cleared = await client.delete("/__fauxcall/journal", params={"session": "eval-1"})
        assert cleared.json()["removed"] == 3


async def test_admin_health_sessions_snapshot_world_and_model(tmp_path: Path) -> None:
    fake = harness(tmp_path)
    async with fake.client(session="s1") as client:
        await client.post("/v1/watchlists", json={"name": "Tech"})
        health = (await client.get("/__fauxcall/health")).json()
        assert health["fake"] == "market"
        assert "s1" in health["sessions"]
        assert health["jev_active"] is False
        snapshot = (await client.get("/__fauxcall/snapshot")).json()
        assert snapshot["session"] == "s1"
        await client.post("/__fauxcall/reset", json={"session": "s1"})
        assert (await client.get("/v1/watchlists/1001")).status_code == 404
        restored = await client.post(
            "/__fauxcall/restore", json={"session": "s1", "snapshot": snapshot["snapshot"]}
        )
        assert restored.status_code == 200
        assert (await client.get("/v1/watchlists/1001")).status_code == 200
        world = (await client.get("/__fauxcall/world/exchanges")).json()
        assert len(world["records"]) == 2
        assert (await client.get("/__fauxcall/world/nope")).status_code == 404
        assert (await client.get("/__fauxcall/model")).json()["name"] == "market"
        clock = (await client.get("/__fauxcall/clock")).json()
        assert clock["mode"] == "real"
        bad = await client.post("/__fauxcall/clock", json={"op": "scale"})
        assert bad.status_code == 400
        await client.post("/__fauxcall/clock", json={"op": "scale", "rate": 60})
        fake.base.advance(1)
        assert (await client.get("/__fauxcall/clock")).json()["rate"] == 60
        await client.post("/__fauxcall/clock", json={"op": "real"})
        users = await client.put(
            "/__fauxcall/auth/users", json=[{"name": "bob", "api_key": "k", "status": "denied"}]
        )
        assert users.json() == {"users": ["bob"]}


async def test_admin_token_protects_admin_routes(tmp_path: Path) -> None:
    fake = harness(tmp_path, admin_token="s3cret")
    async with fake.client() as client:
        assert (await client.get("/__fauxcall/health")).status_code == 401
        ok = await client.get("/__fauxcall/health", headers={"X-Fauxcall-Admin-Token": "s3cret"})
        assert ok.status_code == 200


async def test_latency_emulation_sleeps_toward_the_recorded_profile(tmp_path: Path) -> None:
    model = market_model()
    model.endpoint("GET /v1/exchanges").latency = LatencyProfile.from_samples([250.0])
    fake = harness(tmp_path, model=model, latency=True)
    async with fake.client() as client:
        await client.get("/v1/exchanges")
    assert fake.sleeps
    assert 0.2 < fake.sleeps[-1] <= 0.25


async def test_persisted_state_survives_restart(tmp_path: Path) -> None:
    fake = harness(tmp_path, persist_state=True)
    async with fake.client() as client:
        await client.post("/v1/watchlists", json={"name": "Keep me"})
    assert (tmp_path / "data" / "state" / "world.json").exists()
    restarted = FakeRuntime(fake.runtime.settings, fake.runtime.model)
    record = restarted.world.session().get("watchlists", 1001)
    assert record is not None
    assert j(record.record)["name"] == "Keep me"
