import json

import httpx
import pytest

from app.services.hex_client import HexClient, HexError


def client(handler, **kw):
    return HexClient("secret", "https://hex.test/api/reseller/v1", transport=httpx.MockTransport(handler),
                     min_interval=0, **kw)


async def test_auth_header_and_create_payload():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        seen["path"] = req.url.path
        return httpx.Response(200, json={"status": "created", "total_to_pay_nano": 5})

    c = client(handler)
    r = await c.create_order("cid", "stars", "@durov", quantity=100)
    assert seen["auth"] == "Bearer secret"
    assert seen["path"] == "/api/reseller/v1/order/create"
    assert seen["body"] == {"custom_id": "cid", "product": "stars", "recipient_username": "durov", "quantity": 100}
    assert r["total_to_pay_nano"] == 5


async def test_retries_on_5xx_then_succeeds(monkeypatch):
    async def no_sleep(_): pass
    monkeypatch.setattr("app.services.hex_client.asyncio.sleep", no_sleep)
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(503, json={}) if n["c"] < 3 else httpx.Response(200, json={"balance_nano": 1})

    assert (await client(handler).balance())["balance_nano"] == 1
    assert n["c"] == 3


async def test_429_uses_retry_after(monkeypatch):
    slept = []

    async def fake_sleep(d): slept.append(d)
    monkeypatch.setattr("app.services.hex_client.asyncio.sleep", fake_sleep)
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"products": [{"code": "stars"}]})

    assert await client(handler).products() == [{"code": "stars"}]
    assert 3.0 in slept


async def test_4xx_raises_with_error_code_and_no_retry():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(409, json={"error_code": "out_of_stock", "error": "нет в наличии"})

    with pytest.raises(HexError) as ei:
        await client(handler).pay_order("cid")
    assert ei.value.status == 409 and ei.value.code == "out_of_stock"
    assert n["c"] == 1


async def test_network_error_becomes_hexerror(monkeypatch):
    async def no_sleep(_): pass
    monkeypatch.setattr("app.services.hex_client.asyncio.sleep", no_sleep)

    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(HexError) as ei:
        await client(handler, max_retries=1).balance()
    assert ei.value.status == 0


async def test_check_recipient_valid_flag():
    def handler(req):
        assert req.url.params["username"] == "durov"
        return httpx.Response(200, json={"valid": False})

    assert await client(handler).check_recipient("stars", "@durov") is False
