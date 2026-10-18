"""Muse HTTP/WS 传输；不重试写请求，不持有研究或 Run 状态。"""

import asyncio
import json as json_module
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from urllib.parse import urlsplit, urlunsplit

import httpx2
from websockets.asyncio.client import connect


class MuseClient:
    def __init__(
        self, base_url: str, timeout: float = 30.0, *,
        client: httpx2.Client | None = None, connect_factory: Callable | None = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("base_url 必须是无 query/fragment 的 HTTP(S) 地址")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client if client is not None else httpx2.Client(timeout=timeout)
        self._connect_factory = connect_factory if connect_factory is not None else connect

    def request(
        self, method: str, path: str, *, json: dict | None = None,
        content: bytes | None = None, params: dict | None = None,
    ) -> dict | list:
        timeout = 3.0 if path == "/health" else 120.0 if path.endswith("/derive") else self.timeout
        return self._request(method, path, json=json, content=content, params=params, timeout=timeout)

    def upload(self, path: str, archive: Path, resource_id: str | None = None) -> dict:
        return self._request(
            "POST", path, content=archive.read_bytes(), headers={"Content-Type": "application/zip"},
            params={"resource_id": resource_id} if resource_id is not None else None, timeout=120.0,
        )

    def download(self, path: str, destination: Path) -> None:
        with self._client.stream("GET", self._url(path), timeout=self.timeout) as response:
            if not response.is_success:
                response.read()
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)

    async def stream(self, path: str, stop_event: Event, *, outbound: Queue | None = None) -> AsyncIterator[dict]:
        if stop_event.is_set():
            return
        parsed = urlsplit(self._url(path))
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                          parsed.path, parsed.query, ""))
        async with self._connect_factory(url, open_timeout=3.0, close_timeout=1.0) as websocket:
            while not stop_event.is_set():
                if outbound is not None:
                    while True:
                        try:
                            message = outbound.get_nowait()
                        except Empty:
                            break
                        await websocket.send(json_module.dumps(message, ensure_ascii=False, allow_nan=False))
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=0.25)
                except TimeoutError:
                    continue
                event = json_module.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("WS 消息必须为 JSON object")
                yield event
                if event.get("kind") == "terminal":
                    return

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        response = self._client.request(method, self._url(path), **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return {}
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise ValueError("HTTP 响应必须为 JSON object/array")
        return data
