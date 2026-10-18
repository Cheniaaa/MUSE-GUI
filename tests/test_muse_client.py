import asyncio
from hashlib import sha256
from threading import Event

import httpx2
import pytest

from muse_gui.backend.muse_client import MuseClient


def test_zip_upload_uses_raw_body(tmp_path):
    # 若改回 multipart 或丢掉资源身份，真实发出的请求不再匹配。
    archive = tmp_path / "package.zip"
    archive.write_bytes(b"PK\x03\x04\x00\xffraw archive")
    sent = []

    def handle(request):
        sent.append(request)
        return httpx2.Response(201, json={"resource_id": "package-1", "revision": 2})

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as transport:
        client = MuseClient("http://localhost:8000", client=transport)
        assert client.upload("/catalog/training-packages", archive, "package-1") == {
            "resource_id": "package-1", "revision": 2}
    assert len(sent) == 1
    assert sent[0].headers["content-type"] == "application/zip"
    assert sent[0].content == archive.read_bytes()
    assert sent[0].url.params["resource_id"] == "package-1"
    assert sent[0].extensions["timeout"]["read"] == 120.0


def test_request_preserves_json_pagination_and_timeouts():
    sent = []

    def handle(request):
        sent.append(request)
        return httpx2.Response(200, json={"items": [{"id": "env-1"}], "next_offset": 10})

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as transport:
        client = MuseClient("https://localhost:8000/api/", client=transport)
        page = client.request("GET", "/catalog/environments", params={"offset": 5})
        assert page == {"items": [{"id": "env-1"}], "next_offset": 10}
        client.request("GET", "/health")
        client.request("POST", "/catalog/environments/derive", json={"boxes": []})
    assert str(sent[0].url) == "https://localhost:8000/api/catalog/environments?offset=5"
    assert [r.extensions["timeout"]["read"] for r in sent] == [30.0, 3.0, 120.0]
    assert sent[2].content == b'{"boxes":[]}'


def test_http_error_retains_structured_response():
    with httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(
        422, json={"errors": [{"field": "request.seed", "message": "invalid"}]}))) as transport:
        client = MuseClient("http://localhost", client=transport)
        with pytest.raises(httpx2.HTTPStatusError) as caught:
            client.request("POST", "/experiments", json={})
    assert caught.value.response.json() == {
        "errors": [{"field": "request.seed", "message": "invalid"}]}


def test_download_preserves_bytes_and_close_releases_owned_client(tmp_path):
    data = bytes(range(256)) * 700
    with httpx2.Client(transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, content=data))) as transport:
        client = MuseClient("http://localhost", client=transport)
        destination = tmp_path / "artifact.bin"
        client.download("/runs/run-1/artifacts/blob", destination)
        client.close()
        assert not transport.is_closed
    assert sha256(destination.read_bytes()).hexdigest() == sha256(data).hexdigest()
    owned = MuseClient("http://localhost")
    owned.close()
    with pytest.raises(RuntimeError, match="closed"):
        owned.request("GET", "/health")


def test_no_content_response_and_invalid_json_shape():
    responses = iter([httpx2.Response(204), httpx2.Response(200, json=42)])
    with httpx2.Client(transport=httpx2.MockTransport(lambda r: next(responses))) as transport:
        client = MuseClient("http://localhost", client=transport)
        assert client.request("DELETE", "/previews/p-1") == {}
        with pytest.raises(ValueError, match="JSON"):
            client.request("GET", "/catalog/schema")


def test_stream_yields_terminal_then_closes_without_waiting_for_server(websocket_server):
    paths = []
    closed = Event()

    def handle(ws):
        paths.append(ws.request.path)
        ws.send('{"kind":"state","payload":{"state":"RUNNING"}}')
        ws.send('{"kind":"terminal","payload":{"state":"COMPLETED"}}')
        ws.wait_closed()
        closed.set()

    with websocket_server(handle) as port:
        client = MuseClient(f"http://127.0.0.1:{port}/api")
        async def consume():
            return [event async for event in client.stream("/streams/runs/r-1/events?x=1", Event())]
        events = asyncio.run(asyncio.wait_for(consume(), 3))
        client.close()
    assert events == [{"kind": "state", "payload": {"state": "RUNNING"}},
                      {"kind": "terminal", "payload": {"state": "COMPLETED"}}]
    assert paths == ["/api/streams/runs/r-1/events?x=1"]
    assert closed.is_set()


def test_stream_stop_interrupts_idle_receive(websocket_server):
    stop = Event()
    def handle(ws):
        stop.set()
        ws.wait_closed()

    with websocket_server(handle) as port:
        client = MuseClient(f"http://127.0.0.1:{port}")
        async def consume():
            return [event async for event in client.stream("/streams/runs/r-1/events", stop)]
        assert asyncio.run(asyncio.wait_for(consume(), 3)) == []
        client.close()


def test_stream_connect_factory_is_injectable():
    urls = []
    class Connection:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def recv(self):
            return '{"kind":"terminal","payload":{"state":"FAILED"}}'
    def connect(url, **kwargs):
        urls.append(url)
        return Connection()
    client = MuseClient("https://localhost/api", connect_factory=connect)
    async def consume():
        return [event async for event in client.stream("/events", Event())]
    assert asyncio.run(consume())[0]["payload"]["state"] == "FAILED"
    assert urls == ["wss://localhost/api/events"]
    client.close()
