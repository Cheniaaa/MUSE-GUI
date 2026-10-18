import asyncio
import json
from threading import Event
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx2
import websockets


STREAM_CLOSE_TYPE = "gateway_stream_close"
STREAM_NAMES = {"simulation", "training", "logs", "entities"}


class GatewayClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        timeout: float = 30.0,
        *,
        client: httpx2.Client | None = None,
        connect_factory: Callable | None = None,
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx2.Client(timeout=timeout)
        self._connect_factory = connect_factory or websockets.connect

    def close(self):
        if self._owns_client:
            self._client.close()

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=3.0)

    def start_backend(self) -> dict:
        return self._request("POST", "/backend/start", timeout=60.0)

    def stop_backend(self) -> dict:
        return self._request("POST", "/backend/stop", timeout=120.0)

    def list_environments(self) -> dict:
        return self._request("GET", "/simulation/environments")

    def upload_environment(self, zip_path: str) -> dict:
        return self._upload_zip("/simulation/environments", zip_path)

    def get_simulation_environment_semantics(self, environment_id: str) -> dict:
        return self._request("GET", f"/simulation/environments/{environment_id}/semantics")

    def start_simulation(self, payload: dict) -> dict:
        return self._request("POST", "/simulation/start", json=payload, timeout=240.0)

    def stop_simulation(self) -> dict:
        return self._request("POST", "/simulation/stop", timeout=90.0)

    def simulation_status(self) -> dict:
        return self._request("GET", "/simulation/status")

    def runtime_entity_status(self) -> dict:
        return self._request("GET", "/runtime/entities/status")

    def upload_training_package(self, zip_path: str) -> dict:
        return self._upload_zip("/training/packages", zip_path)

    def list_training_packages(self) -> dict:
        return self._request("GET", "/training/packages")

    def get_training_package(self, package_id: str) -> dict:
        return self._request("GET", f"/training/packages/{package_id}")

    def list_training_recipes(self) -> dict:
        return self._request("GET", "/training/recipes")

    def get_training_recipe(self, recipe_id: str) -> dict:
        return self._request("GET", f"/training/recipes/{recipe_id}")

    def register_training_recipe(self, payload: dict) -> dict:
        return self._request("POST", "/training/recipes", json=payload)

    def start_training_run(self, payload: dict) -> dict:
        return self._request("POST", "/training/runs", json=payload)

    def stop_training_run(self, run_id: str) -> dict:
        return self._request("POST", f"/training/runs/{run_id}/stop")

    def get_training_run_status(self, run_id: str) -> dict:
        return self._request("GET", f"/training/runs/{run_id}")

    def stream_url(self, stream_name: str) -> str:
        if stream_name not in STREAM_NAMES:
            raise ValueError(f"unsupported gateway stream: {stream_name}")
        parsed = urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/streams/{stream_name}"
        return urlunsplit((scheme, parsed.netloc, path, "", ""))

    async def stream_envelopes(
        self,
        stream_name: str,
        *,
        stop_event: Event | None = None,
        recv_timeout: float = 0.5,
    ) -> AsyncIterator[dict]:
        async with self._connect_factory(self.stream_url(stream_name)) as websocket:
            while stop_event is None or not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout)
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                envelope = json.loads(raw)
                if envelope.get("type") == STREAM_CLOSE_TYPE:
                    break
                yield envelope

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self._client.request(method, self._url(path), **kwargs)
        response.raise_for_status()
        return response.json()

    def _upload_zip(self, path: str, zip_path: str) -> dict:
        with open(zip_path, "rb") as file:
            return self._request("POST", path, files={"file": file}, timeout=120.0)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{path}"

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        return base_url.rstrip("/")
