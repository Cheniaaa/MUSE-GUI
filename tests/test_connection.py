from contextlib import contextmanager
from threading import Event, get_ident
import socket

import httpx2
import pytest
from PySide6.QtTest import QSignalSpy

from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.backend.muse_client import MuseClient


def health(catalog="catalog-a", instance="instance-1", revision=2):
    return {"status": "ok", "gpu_busy": False, "platform_revision": "0.2",
             "api_revision": revision, "catalog_id": catalog, "instance_id": instance}


def test_send_stream_uses_the_existing_control_socket(tmp_path, wait_until, websocket_server):
    import json
    seen = []
    def socket_handler(ws):
        ws.send(json.dumps({"kind": "state", "state": "ready"}))
        seen.append(json.loads(ws.recv(timeout=3)))
        ws.send(json.dumps({"kind": "view", "event": {"kind": "selection", "object_id": seen[0]["object_id"]}}))
        ws.wait_closed()
    with websocket_server(socket_handler) as port:
        with controller_for(tmp_path, wait_until, response_for) as controller:
            controller.connect({"base_url": f"http://127.0.0.1:{port}"})
            wait_until(lambda: controller.state["state"] == "connected")
            received = QSignalSpy(controller.result_received)
            controller.subscribe("preview-control", "/streams/previews/p-1")
            value = {"kind": "select", "object_id": "box-中文"}
            controller.send_stream("preview-control", value)
            value["object_id"] = "caller-mutated"
            wait_until(lambda: any(received.at(i)[2].get("data", {}).get("kind") == "view" for i in range(received.count())))
            assert seen == [{"kind": "select", "object_id": "box-中文"}]
            controller.unsubscribe("preview-control")


@contextmanager
def controller_for(tmp_path, wait_until, handler):
    transports = []
    def factory(url):
        transport = httpx2.Client(transport=httpx2.MockTransport(handler))
        transports.append(transport)
        return MuseClient(url, client=transport)
    controller = ConnectionController(client_factory=factory, cache_dir=tmp_path)
    try:
        yield controller
    finally:
        wait_until(controller.close)
        for transport in transports:
            transport.close()


def response_for(request, *, catalog="catalog-a", instance="instance-1", revision=2):
    if request.url.path == "/health":
        return httpx2.Response(200, json=health(catalog, instance, revision))
    if request.url.path == "/catalog/schema":
        return httpx2.Response(200, json={"title": catalog, "type": "object"})
    return httpx2.Response(200, json={"items": [{"id": catalog, "name": "same-name"}], "next_offset": None})


def test_reconnect_changes_generation_and_catalog(tmp_path, wait_until):
    entered, release = Event(), Event()
    def handle(request):
        if request.url.path == "/slow":
            entered.set()
            assert release.wait(5)
            return httpx2.Response(200, json={"old": True})
        return response_for(request, catalog="catalog-a" if request.url.host == "a" else "catalog-b")

    with controller_for(tmp_path, wait_until, handle) as controller:
        received = QSignalSpy(controller.result_received)
        controller.connect({"base_url": "http://a", "catalog_id": "catalog-a"})
        wait_until(lambda: controller.state["state"] == "connected" and controller.cached("/catalog/environments") is not None)
        old_generation = controller.state["generation"]
        controller.request("late", "GET", "/slow")
        wait_until(entered.is_set)
        controller.connect({"base_url": "http://b", "catalog_id": "catalog-b"})
        try:
            wait_until(lambda: controller.state["state"] == "connected" and controller.cached("/catalog/environments") is not None)
            assert controller.state["generation"] > old_generation
            assert controller.state["catalog_id"] == "catalog-b"
            assert controller.state["binding_required"] is True
            assert controller.cached("/catalog/environments")["data"]["items"][0]["id"] == "catalog-b"
            release.set()
            wait_until(lambda: not controller.has_pending_requests)
            assert all(received.at(i)[1] != "late" for i in range(received.count()))
        finally:
            release.set()


def test_health_mismatch_prevents_catalog_and_run_requests(tmp_path, wait_until):
    seen = []
    def handle(request):
        seen.append(request.url.path)
        return response_for(request, catalog="other")
    with controller_for(tmp_path, wait_until, handle) as controller:
        controller.connect({"base_url": "http://a", "catalog_id": "expected"})
        wait_until(lambda: controller.state["state"] == "failed")
        assert controller.state["error"]["kind"] == "catalog_mismatch"
        assert seen == ["/health"]
        controller.request("run", "POST", "/runs", json={})
        assert seen == ["/health"]


@pytest.mark.parametrize("revision", [1, 3])
def test_incompatible_revision_is_explicit_error(tmp_path, wait_until, revision):
    with controller_for(tmp_path, wait_until, lambda r: response_for(r, revision=revision)) as controller:
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: controller.state["state"] == "failed")
        assert controller.state["error"]["kind"] == "incompatible_api"
        assert controller.state["error"]["api_revision"] == revision


def test_post_failure_is_not_retried(tmp_path, wait_until):
    attempts = []
    def handle(request):
        if request.url.path == "/runs" and request.method == "POST":
            attempts.append(request)
            raise httpx2.ReadTimeout("response lost", request=request)
        return response_for(request)
    with controller_for(tmp_path, wait_until, handle) as controller:
        received = QSignalSpy(controller.result_received)
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: controller.state["state"] == "connected")
        controller.request("submit", "POST", "/runs", json={"experiment_id": "e-1"})
        wait_until(lambda: any(received.at(i)[1] == "submit" for i in range(received.count())))
        result = next(received.at(i)[2] for i in range(received.count()) if received.at(i)[1] == "submit")
        assert len(attempts) == 1
        assert result["ok"] is False
        assert result["error"]["kind"] == "connection"
        assert result["error"]["outcome"] == "unknown"
        assert controller.state["state"] == "disconnected"


def test_structured_http_error_does_not_become_connection_failure(tmp_path, wait_until):
    def handle(request):
        if request.url.path == "/experiments":
            return httpx2.Response(422, json={"errors": [{"field": "seed", "message": "invalid"}]})
        return response_for(request)
    with controller_for(tmp_path, wait_until, handle) as controller:
        received = QSignalSpy(controller.result_received)
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: controller.state["state"] == "connected")
        controller.request("freeze", "POST", "/experiments", json={})
        wait_until(lambda: any(received.at(i)[1] == "freeze" for i in range(received.count())))
        result = next(received.at(i)[2] for i in range(received.count()) if received.at(i)[1] == "freeze")
        assert result["error"]["kind"] == "http"
        assert result["error"]["status_code"] == 422
        assert result["error"]["detail"] == {"errors": [{"field": "seed", "message": "invalid"}]}
        assert controller.state["state"] == "connected"


def test_offline_cache_is_not_resolved_input(tmp_path, wait_until):
    with controller_for(tmp_path, wait_until, response_for) as controller:
        controller.connect({"base_url": "http://a", "catalog_id": "catalog-a"})
        wait_until(lambda: controller.cached("/catalog/schema") is not None)
        controller.disconnect()
        cached = controller.cached("/catalog/schema")
        assert cached["offline"] is True
        assert cached["catalog_id"] == "catalog-a"
        assert cached["api_revision"] == 2
        assert cached["saved_at"]
        cached["data"]["title"] = "modified display copy"
        assert controller.cached("/catalog/schema")["data"]["title"] == "catalog-a"
        received = QSignalSpy(controller.result_received)
        controller.request("run", "POST", "/runs", json={"experiment_id": "e-1"})
        assert received.at(0)[2]["error"]["kind"] == "offline"
        assert received.at(0)[2]["ok"] is False
    with controller_for(tmp_path, wait_until, response_for) as reopened:
        cached = reopened.cached("/catalog/schema", catalog_id="catalog-a")
        assert cached["offline"] is True
        assert cached["source"] == "http://a"
        assert reopened.cached("/catalog/schema", catalog_id="catalog-b") is None


def test_restart_refreshes_runs_and_catalog_without_changing_run_state(tmp_path, wait_until):
    instance = ["instance-1"]
    seen = []
    worker_threads, signal_threads = [], []
    def handle(request):
        seen.append((instance[0], request.url.path))
        worker_threads.append(get_ident())
        return response_for(request, instance=instance[0])
    with controller_for(tmp_path, wait_until, handle) as controller:
        controller.state_changed.connect(lambda state: signal_threads.append(get_ident()))
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: not controller.has_pending_requests)
        first = controller.state["generation"]
        controller.disconnect()
        instance[0] = "instance-2"
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: not controller.has_pending_requests)
        assert controller.state["instance_id"] == "instance-2"
        assert controller.state["generation"] > first
        assert ("instance-2", "/catalog/schema") in seen
        assert ("instance-2", "/runs") in seen
        assert controller.state["last_seen"]
        assert "run_state" not in controller.state
        assert set(signal_threads) == {get_ident()}
        assert get_ident() not in worker_threads


def test_subscription_delivers_terminal_and_releases_thread(tmp_path, wait_until, websocket_server):
    def handle(ws):
        ws.send('{"kind":"terminal","payload":{"state":"COMPLETED"}}')
        ws.wait_closed()
    with websocket_server(handle) as port:
        with controller_for(tmp_path, wait_until, response_for) as controller:
            received = QSignalSpy(controller.result_received)
            controller.connect({"base_url": f"http://127.0.0.1:{port}"})
            wait_until(lambda: controller.state["state"] == "connected")
            controller.subscribe("events:r-1", "/streams/runs/r-1/events")
            wait_until(lambda: any(received.at(i)[1] == "events:r-1" for i in range(received.count())))
            terminal = next(received.at(i)[2] for i in range(received.count()) if received.at(i)[1] == "events:r-1")
            assert terminal == {"ok": True, "data": {"kind": "terminal", "payload": {"state": "COMPLETED"}}}
            wait_until(lambda: not controller.has_pending_requests)


def test_cache_keeps_pages_separate(tmp_path, wait_until):
    def handle(request):
        if request.url.path == "/catalog/environments" and "offset" in request.url.params:
            return httpx2.Response(200, json={"items": [{"id": "page-two"}], "next_offset": None})
        return response_for(request)
    with controller_for(tmp_path, wait_until, handle) as controller:
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: controller.cached("/catalog/environments") is not None)
        controller.request("page-two", "GET", "/catalog/environments", params={"offset": 10})
        wait_until(lambda: controller.cached("/catalog/environments", params={"offset": 10}) is not None)
        controller.disconnect()
        assert controller.cached("/catalog/environments")["data"]["items"][0]["id"] == "catalog-a"
        assert controller.cached("/catalog/environments", params={"offset": 10})["data"]["items"][0]["id"] == "page-two"


@pytest.mark.parametrize("identity", [None, 5, ""])
def test_invalid_health_identity_is_protocol_error(tmp_path, wait_until, identity):
    def handle(request):
        return httpx2.Response(200, json=health(catalog=identity))
    with controller_for(tmp_path, wait_until, handle) as controller:
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: controller.state["state"] == "failed")
        assert controller.state["error"]["kind"] == "protocol"


def test_close_is_bounded_while_request_is_pending(tmp_path, wait_until):
    entered, release = Event(), Event()
    def handle(request):
        if request.url.path == "/slow":
            entered.set()
            assert release.wait(5)
            return httpx2.Response(200, json={"state": "COMPLETED"})
        return response_for(request)
    with controller_for(tmp_path, wait_until, handle) as controller:
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: not controller.has_pending_requests)
        received = QSignalSpy(controller.result_received)
        controller.request("slow", "GET", "/slow")
        wait_until(entered.is_set)
        try:
            assert controller.close(wait_ms=0) is False
        finally:
            release.set()
        wait_until(controller.close)
        assert received.count() == 0


def test_unsubscribe_stops_idle_stream_without_disconnect(tmp_path, wait_until, websocket_server):
    entered, closed = Event(), Event()
    def handle(ws):
        entered.set()
        ws.wait_closed()
        closed.set()
    with websocket_server(handle) as port:
        with controller_for(tmp_path, wait_until, response_for) as controller:
            controller.connect({"base_url": f"http://127.0.0.1:{port}"})
            wait_until(lambda: not controller.has_pending_requests)
            controller.subscribe("events:r-1", "/streams/runs/r-1/events")
            wait_until(entered.is_set)
            controller.unsubscribe("events:r-1")
            wait_until(lambda: not controller.has_pending_requests)
            wait_until(closed.is_set)
            assert controller.state["state"] == "connected"


def test_cache_from_previous_instance_is_stale_until_refetched(tmp_path, wait_until):
    with controller_for(tmp_path, wait_until, response_for) as controller:
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: controller.cached("/catalog/schema") is not None)
    entered, release = Event(), Event()
    def restarted(request):
        if request.url.path == "/catalog/schema":
            entered.set()
            assert release.wait(5)
        return response_for(request, instance="instance-2")
    with controller_for(tmp_path, wait_until, restarted) as controller:
        controller.connect({"base_url": "http://a"})
        try:
            wait_until(entered.is_set)
            assert controller.cached("/catalog/schema")["offline"] is True
            release.set()
            wait_until(lambda: not controller.has_pending_requests)
            assert controller.cached("/catalog/schema")["offline"] is False
        finally:
            release.set()


def test_cache_write_error_is_reported_not_hidden(tmp_path, wait_until):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked", encoding="utf-8")
    with controller_for(blocked, wait_until, response_for) as controller:
        received = QSignalSpy(controller.result_received)
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: not controller.has_pending_requests)
        results = [received.at(i)[2] for i in range(received.count())
                   if received.at(i)[1] == "refresh:/catalog/schema"]
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["error"]["kind"] == "local"
        assert controller.state["state"] == "connected"


def test_subscription_connection_refused_is_not_a_local_file_error(tmp_path, wait_until):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        with controller_for(tmp_path, wait_until, response_for) as controller:
            received = QSignalSpy(controller.result_received)
            controller.connect({"base_url": f"http://127.0.0.1:{port}"})
            wait_until(lambda: not controller.has_pending_requests)
            controller.subscribe("events:r-1", "/streams/runs/r-1/events")
            wait_until(lambda: any(received.at(i)[1] == "events:r-1" for i in range(received.count())))
            result = next(received.at(i)[2] for i in range(received.count()) if received.at(i)[1] == "events:r-1")
            assert result["error"]["kind"] == "connection"
            assert controller.state["state"] == "disconnected"


def test_download_http_error_preserves_structured_detail(tmp_path, wait_until):
    class Body(httpx2.SyncByteStream):
        def __iter__(self):
            yield b'{"detail":"artifact unavailable"}'
    def handle(request):
        if request.url.path == "/artifact":
            return httpx2.Response(404, headers={"content-type": "application/json"}, stream=Body())
        return response_for(request)
    with controller_for(tmp_path, wait_until, handle) as controller:
        received = QSignalSpy(controller.result_received)
        controller.connect({"base_url": "http://a"})
        wait_until(lambda: not controller.has_pending_requests)
        destination = tmp_path / "download.bin"
        controller.download("artifact", "/artifact", destination)
        wait_until(lambda: any(received.at(i)[1] == "artifact" for i in range(received.count())))
        result = next(received.at(i)[2] for i in range(received.count()) if received.at(i)[1] == "artifact")
        assert result["error"]["kind"] == "http"
        assert result["error"]["detail"] == {"detail": "artifact unavailable"}
        assert not destination.exists()
