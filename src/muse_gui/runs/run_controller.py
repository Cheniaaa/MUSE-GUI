"""每个可见 Run 的 G1 请求/订阅协调；只控制后端公开接口。"""
from copy import deepcopy
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from .run_state import RunState, WORKER_STATES


class RunController(QObject):
    changed = Signal()

    def __init__(self, connection, viewer_host, parent=None):
        super().__init__(parent)
        self.connection, self.viewer_host = connection, viewer_host
        self.state = None
        self.active = False
        self.view_id = None
        self._prefix = f"run:{uuid4().hex}:"
        self._serial = self._epoch = 0
        self._pending, self._streams = {}, {}
        self._new_run = False
        self._terminal_seen = False
        self._opened_url = None
        self._view_visible = True
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.refresh)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)
        viewer_host.page_ready.connect(self._page_ready)
        viewer_host.page_failed.connect(self._page_failed)

    @property
    def can_command(self):
        state = self.state
        return bool(self.active and state and state.connected and state.record
                    and state.record["mode"] == "INTERACTIVE" and state.record["state"] in WORKER_STATES
                    and not state.submitting_command and not state.command_sync_required
                    and state.command_status != "pending")

    def can_set_display(self, viewer: str) -> bool:
        return (self._worker_available() and not self.state.display_pending[viewer]
                and self.state.definition is not None)

    def open(self, run_id: str, *, new_run: bool = False) -> None:
        if self.state is not None:
            self.close_tab(self.state.run_id)
        self._epoch += 1
        self.state = RunState(run_id, self.connection.state["generation"])
        self.state.connected = self.connection.state["state"] == "connected"
        self.active, self._new_run = True, new_run
        self._terminal_seen = False
        self.refresh()
        self.poll_timer.start()
        self.changed.emit()

    def close_tab(self, run_id: str) -> None:
        if self.state is None or self.state.run_id != run_id or not self.active:
            return
        self.active = False
        self._new_run = False
        self.poll_timer.stop()
        self._release_view()
        for token in list(self._streams):
            self.connection.unsubscribe(token)
        self._streams.clear()
        # 保留在途 register 的路由元数据，晚回包仍须 DELETE 私有 ID。
        self._epoch += 1
        self._pending = {token: value for token, value in self._pending.items() if value[2] == "register"}
        self.state.invalidate_entities("标签已关闭；Run 继续在后端运行")
        self.changed.emit()

    def refresh(self) -> None:
        if not self.active or not self.state.connected:
            return
        self._request("run", "GET", self._path())
        if self.state.record is not None:
            self._request("display", "GET", self._path("/display"))

    def set_display(self, viewer: str, patch: dict) -> None:
        operation = "display_patch:" + viewer
        if not self._worker_available() or self._has_pending(operation):
            return
        if viewer == "viser":
            self.state.invalidate_entities("Viser 显示请求在途；等待当前选择的新帧")
            self._pending = {token: value for token, value in self._pending.items()
                             if value[1:3] != (self._epoch, "entities_get")}
        self.state.display_pending[viewer] = True
        self._request(operation, "PATCH", self._path("/display"), json={viewer: patch})
        self.changed.emit()

    def command(self, command: str, world_id: int | None = None) -> None:
        if not self.can_command:
            return
        if command not in {"pause", "resume", "step", "reset_world"}:
            raise ValueError("未知 Run 命令")
        previous = self.state.interaction["command"]
        payload = {"command_seq": previous["command_seq"] + 1 if previous else 1, "command": command}
        if command == "reset_world":
            payload["world_id"] = world_id
        self.state.submitting_command = True
        self.state.error = None
        self._request("command", "POST", self._path("/commands"), json=payload)
        self.changed.emit()

    def cancel(self) -> None:
        if self.active and self.state.connected and self.state.record and not self.state.terminal:
            self._request("cancel", "POST", self._path("/cancel"))

    def view_intent(self, intent: dict) -> None:
        if intent.get("kind") not in {"select", "focus", "visibility", "reset_camera"}:
            return
        if (not self.active or self.view_id is None or not self.state.pairing.get("paired")
                or not self.state.pairing.get("connected") or self.state.terminal):
            return
        token = next((token for token, value in self._streams.items() if value[2] == "view"), None)
        if token is not None:
            self.connection.send_stream(token, intent)

    def reconnect_view(self) -> None:
        """只重建本客户端配对/网页，不覆盖共享显示选择。"""
        if self.active and self.state.connected and not self.state.terminal:
            self._release_view()
            self.state.viewer_error = None
            self._sync_view()
            self.changed.emit()

    def set_view_visible(self, visible: bool) -> None:
        """隐藏仅回收本客户端网页，Run观察、命令和共享显示继续。"""
        if visible == self._view_visible:
            return
        self._view_visible = visible
        if visible:
            self._sync_view()
        else:
            self._release_view()

    def navigation_context(self, checkpoint_artifact_id: str | None = None) -> dict:
        return deepcopy({"run_id": self.state.run_id, "experiment_id": self.state.record["experiment_id"],
                         "record": self.state.record, "source": self.state.source,
                         "definition": self.state.definition, "checkpoint_artifact_id": checkpoint_artifact_id,
                         "checkpoint": next((item for item in self.state.artifacts
                                             if item["id"] == checkpoint_artifact_id), None)})

    def _worker_available(self):
        return bool(self.active and self.state and self.state.connected and self.state.record
                    and self.state.record["state"] in WORKER_STATES)

    def _path(self, suffix=""):
        return f"/runs/{quote(self.state.run_id, safe='')}{suffix}"

    def _token(self):
        self._serial += 1
        return self._prefix + str(self._serial)

    def _has_pending(self, operation):
        return any(data[1:3] == (self._epoch, operation) for data in self._pending.values())

    def _request(self, operation, method, path, **kwargs):
        if self._has_pending(operation):
            return
        token = self._token()
        self._pending[token] = (self.connection.state["generation"], self._epoch, operation, self.state.run_id)
        self.connection.request(token, method, path, **kwargs)

    def _subscribe(self, operation, path):
        if any(data[2] == operation for data in self._streams.values()):
            return
        token = self._token()
        self._streams[token] = (self.state.generation, self._epoch, operation)
        self.connection.subscribe(token, path)

    def _sync_view(self):
        state = self.state
        if not self.active or not self._view_visible or not state.connected or state.terminal:
            return
        display = state.display["viser"] if state.display is not None else None
        if display is None or not display["effective"]["enabled"] or not display["viewer_url"]:
            self._release_view()
            return
        if self.view_id is None:
            self._request("register", "POST", self._path("/display/clients"))
        elif self._opened_url != display["viewer_url"]:
            self.state.page_loaded = False
            self._opened_url = display["viewer_url"]
            self.viewer_host.open(self._opened_url, self.view_id)

    def _release_view(self):
        for token, metadata in list(self._streams.items()):
            if metadata[2] == "view":
                self.connection.unsubscribe(token)
                del self._streams[token]
        if self.view_id is not None:
            identity, self.view_id = self.view_id, None
            if self.connection.state["state"] == "connected":
                self._request("delete:" + identity, "DELETE", self._path("/display/clients/") + quote(identity, safe=""))
        if self._opened_url is not None:
            self.viewer_host.unload()
        self._opened_url = None
        if self.state is not None:
            self.state.pairing = {}
            self.state.page_loaded = False

    def _received(self, generation, token, result):
        pending = self._pending.pop(token, None)
        if pending is None:
            stream = self._streams.get(token)
            if (stream is None or stream[:2] != (generation, self._epoch) or not self.active
                    or generation != self.connection.state["generation"]):
                return
            operation = stream[2]
        else:
            expected_generation, epoch, operation, run_id = pending
            if generation != expected_generation or generation != self.connection.state["generation"]:
                return
            if epoch != self._epoch or not self.active:
                if operation == "register" and result["ok"]:
                    identity = result["data"]["view_id"]
                    self.connection.request(self._token(), "DELETE",
                        f"/runs/{quote(run_id, safe='')}/display/clients/{quote(identity, safe='')}")
                return
        state = self.state
        if not result["ok"]:
            if operation.startswith("display_patch:"):
                state.display_pending[operation.split(":")[1]] = False
            if operation == "command":
                state.submitting_command = False
                state.command_sync_required = True
                # 冲突后的真值读取不能复用发命令前已在途的旧轮询。
                self._pending = {key: value for key, value in self._pending.items()
                                 if value[1:3] != (self._epoch, "run")}
                if state.connected:
                    self._request("run", "GET", self._path())
            if operation in {"view", "register"}:
                state.viewer_error = str(result["error"])
            else:
                state.error = {"operation": operation, **result["error"]}
            if operation in {"run", "display", "entities", "events"}:
                self.poll_timer.stop()
            self.changed.emit()
            return
        data = result["data"]
        if operation == "run":
            first = state.record is None
            state.apply_snapshot(data)
            state.command_sync_required = False
            if first:
                experiment = quote(data["experiment_id"], safe="")
                self._request("source", "GET", f"/experiments/{experiment}/source")
                self._request("definition", "GET", f"/experiments/{experiment}")
                if not state.terminal:
                    self._request("artifacts", "GET", self._path("/artifacts"))
                self._request("display", "GET", self._path("/display"))
                if not state.terminal:
                    self._subscribe("events", f"/streams/runs/{quote(state.run_id, safe='')}/events")
        elif operation == "events":
            state.apply_event(data)
            if data["kind"] in {"preview_applied", "preview_model", "preview_error"}:
                self._request("display", "GET", self._path("/display"))
            elif data["kind"] == "checkpoint":
                self._request("artifacts", "GET", self._path("/artifacts"))
        elif operation == "display" or operation.startswith("display_patch:"):
            was_pending = state.display_pending["viser"]
            if operation.startswith("display_patch:"):
                state.display_pending[operation.split(":")[1]] = False
            previous = state.display
            state.apply_display(data)
            changed = previous is None or any(previous["viser"][key] != state.display["viser"][key]
                                              for key in ("selection_seq", "effective_seq"))
            if changed or (was_pending and not state.display_pending["viser"]):
                # WS 新帧可能先于 display ACK 到达并被拒；暂停时不会再推送相同快照。
                # 旧 GET 也不能复用：它可能返回旧选择的 unavailable，清掉当前实体。
                self._pending = {token: value for token, value in self._pending.items()
                                 if value[1:3] != (self._epoch, "entities_get")}
                if not state.display_pending["viser"]:
                    self._request("entities_get", "GET", self._path("/entities"))
            if previous is None:
                if not state.terminal:
                    self._subscribe("entities", f"/streams/runs/{quote(state.run_id, safe='')}/entities")
            if operation != "display_patch:native" or changed:
                self._sync_view()
        elif operation in {"entities", "entities_get"}:
            state.apply_entities(data)
            if (operation == "entities" and data["available"] and state.entities["available"]
                    and state.entities["frame"] == data["frame"]):
                # 当前有效 WS 快照已完成协调；在途 GET 的旧 unavailable 不能再覆盖它。
                self._pending = {token: value for token, value in self._pending.items()
                                 if value[1:3] != (self._epoch, "entities_get")}
        elif operation == "command":
            state.submitting_command = False
            state.command_sync_required = False
            state.error = None
            state.apply_interaction(data)
        elif operation == "cancel":
            self._pending = {key: value for key, value in self._pending.items()
                             if value[1:3] != (self._epoch, "run")}
            state.apply_snapshot(data)
        elif operation == "register":
            if not self._view_visible:
                identity = data["view_id"]
                self._request("delete:" + identity, "DELETE", self._path("/display/clients/") + quote(identity, safe=""))
                return
            self.view_id = data["view_id"]
            self._subscribe("view", f"/streams/runs/{quote(state.run_id, safe='')}/view?view_id={quote(self.view_id, safe='')}")
            self._sync_view()
        elif operation == "view":
            state.apply_view_event(data, self.view_id)
        elif operation in {"source", "definition", "artifacts"}:
            setattr(state, operation, deepcopy(data))
        if self._new_run and self._worker_available() and state.display is not None:
            self._new_run = False
            self.set_display("viser", {"enabled": True})
        if state.terminal:
            if not self._terminal_seen:
                self._terminal_seen = True
                self._pending = {key: value for key, value in self._pending.items()
                                 if value[1] != self._epoch or value[2] not in {"run", "artifacts"}}
                if operation != "run":
                    self._request("run", "GET", self._path())
                self._request("artifacts", "GET", self._path("/artifacts"))
            self.poll_timer.stop()
            self._release_view()
            for stream_token in list(self._streams):
                self.connection.unsubscribe(stream_token)
            self._streams.clear()
        self.changed.emit()

    def _connection_changed(self, connection_state):
        if self.state is None:
            return
        if (connection_state["state"] != "connected"
                or connection_state["generation"] != self.state.generation):
            self.poll_timer.stop()
            self.state.connected = False
            if self.state.submitting_command:
                self.state.command_sync_required = True
                self.state.error = {"operation": "command", "kind": "connection", "outcome": "unknown",
                                    "message": "连接已中断，命令结果未确认；重新打开 Run 读取真值"}
            self.state.invalidate_entities("连接断开；请重新打开 Run 读取 Server 真值")
            self._release_view()
            for token in list(self._streams):
                self.connection.unsubscribe(token)
            self._streams.clear()
            # G1 会先发断连状态，再发当前 generation 的 POST 错误；不能丢掉该错误。
            self._pending = {token: value for token, value in self._pending.items()
                             if value[0] == connection_state["generation"] == self.state.generation
                              and value[2] in {"command", "cancel", "display_patch:viser", "display_patch:native"}}
            self.changed.emit()

    def _page_ready(self, url, view_id):
        if self.active and view_id == self.view_id and url == self._opened_url:
            self.state.page_loaded = True
            self.state.viewer_error = None
            self.changed.emit()

    def _page_failed(self, message):
        if self.active and self.view_id is not None:
            self.state.page_loaded = False
            self.state.viewer_error = message
            self.changed.emit()
