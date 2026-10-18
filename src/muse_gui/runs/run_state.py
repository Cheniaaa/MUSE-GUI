"""Server Run 的只读镜像；断连/暂停/页面失败不产生虚构的 Run 生命周期。"""
from copy import deepcopy


TERMINAL_STATES = {"COMPLETED", "CANCELLED", "FAILED", "LOST"}
WORKER_STATES = {"PREPARING", "COMPILING", "RUNNING"}


class RunState:
    def __init__(self, run_id: str, generation: int):
        self.run_id, self.generation = run_id, generation
        self.record = None
        self.display = None
        self.source = self.definition = None
        self.artifacts = []
        self.entities = {"objects": [], "frame": None, "available": False, "reason": "尚无当前帧"}
        self.pairing = {}
        self.selected_object = None
        self.selection_frame = None
        self.connected = True
        self.submitting_command = False
        self.display_pending = False
        self.command_sync_required = False
        self.error = None
        self.viewer_error = None
        self.page_loaded = False
        self._event_seq = 0
        self._scene_hash = None
        self._latest_frame = None

    @property
    def terminal(self):
        return self.record is not None and self.record["state"] in TERMINAL_STATES

    @property
    def interaction(self):
        return self.record["interaction"] if self.record else {"command": None, "result": None}

    @property
    def command_status(self):
        if self.submitting_command or self.command_sync_required:
            return "pending"
        interaction = self.interaction
        if interaction["command"] is None:
            return "none"
        return interaction["result"]["status"] if interaction["result"] else "pending"

    @property
    def paused(self):
        result = self.interaction["result"]
        return result["paused"] if result else None

    def apply_interaction(self, interaction: dict) -> None:
        if self.record is None:
            return
        previous, incoming = self.interaction["command"], interaction["command"]
        if previous is not None:
            if incoming is None or incoming["command_seq"] < previous["command_seq"]:
                return
            if incoming["command_seq"] == previous["command_seq"] and self.interaction["result"] is not None:
                return
        self.record["interaction"] = deepcopy(interaction)

    def apply_snapshot(self, record: dict) -> None:
        if record["run_id"] != self.run_id:
            return
        incoming = deepcopy(record)
        interaction = incoming.pop("interaction")
        if self.record is None:
            self.record = {"interaction": {"command": None, "result": None}}
        if incoming["event_seq"] >= self._event_seq:
            self.record.update(incoming)
            self._event_seq = incoming["event_seq"]
        self.apply_interaction(interaction)
        if self.terminal:
            self.invalidate_entities("Run 已结束；只保留静态对象身份")

    def apply_event(self, event: dict) -> None:
        if event["run_id"] != self.run_id or event["event_seq"] <= self._event_seq or self.record is None:
            return
        self._event_seq = self.record["event_seq"] = event["event_seq"]
        kind, payload = event["kind"], event["payload"]
        if kind in {"preparing", "compiling", "running"}:
            self.record["state"] = kind.upper()
        elif kind == "terminal":
            self.record["state"] = payload["state"]
            self.record["worker_terminal"] = deepcopy(payload)
            self.invalidate_entities("Run 已结束；只保留静态对象身份")
        elif kind == "failure":
            self.record["failure"] = deepcopy(payload)
        elif kind == "command_result":
            command = {key: payload[key] for key in ("command_seq", "command", "world_id")}
            self.apply_interaction({"command": command, "result": payload})
        elif kind == "metric":
            self.record["metrics"] = deepcopy(payload)

    def apply_display(self, snapshot: dict) -> None:
        if self.display is not None and (snapshot["selection_seq"], snapshot["effective_seq"]) < (
                self.display["selection_seq"], self.display["effective_seq"]):
            return
        previous = self.display
        self.display = deepcopy(snapshot)
        frame, entity_frame = snapshot["frame"], self.entities["frame"]
        new_episode = (frame is not None and entity_frame is not None
                       and frame["selection_seq"] == entity_frame["selection_seq"]
                       and frame["world_id"] == entity_frame["world_id"]
                       and frame["episode"] > entity_frame["episode"])
        if (previous is None or previous["selection_seq"] != snapshot["selection_seq"]
                or new_episode
                or not snapshot["requested"]["enabled"]
                or snapshot["selection_seq"] != snapshot["effective_seq"]):
            self.invalidate_entities("显示选择变更；等待当前 world/selection 的新帧")
        if "interaction" in snapshot:
            self.apply_interaction(snapshot["interaction"])

    def invalidate_entities(self, reason: str) -> None:
        self.entities = {"objects": [{key: value for key, value in obj.items() if key != "pose"}
                                     for obj in self.entities["objects"]],
                         "frame": None, "available": False, "reason": reason}
        self.selection_frame = None

    def accepts_frame(self, frame: dict | None) -> bool:
        if self.display_pending or frame is None or self.display is None or frame["run_id"] != self.run_id:
            return False
        display = self.display
        if (frame["selection_seq"] != display["selection_seq"]
                or frame["selection_seq"] != display["effective_seq"]
                or frame["world_id"] != display["requested"]["world_id"]
                or frame["world_id"] != display["effective"]["world_id"]):
            return False
        if self._scene_hash is not None and frame["scene_hash"] != self._scene_hash:
            return False
        for previous in (self._latest_frame, display["frame"], self.selection_frame):
            if (previous is not None and previous["selection_seq"] == frame["selection_seq"]
                    and previous["world_id"] == frame["world_id"]):
                if previous["scene_hash"] != frame["scene_hash"] or (
                        frame["episode"], frame["decision"], frame["tick"]) < (
                        previous["episode"], previous["decision"], previous["tick"]):
                    return False
        return True

    def apply_entities(self, snapshot: dict) -> None:
        if snapshot["available"]:
            if (not self.connected or self.terminal or not self.accepts_frame(snapshot["frame"])
                    or not self.display["requested"]["enabled"] or not self.display["effective"]["enabled"]):
                return
            self._scene_hash = snapshot["frame"]["scene_hash"]
            self._latest_frame = deepcopy(snapshot["frame"])
            self.entities = deepcopy(snapshot)
        else:
            if self.accepts_frame(snapshot["frame"]):
                self._latest_frame = deepcopy(snapshot["frame"])
            self.entities["objects"] = deepcopy(snapshot["objects"])
            self.invalidate_entities(snapshot["reason"] or "实时对象不可用")

    def apply_view_event(self, event: dict, view_id: str) -> None:
        if event["view_id"] != view_id:
            return
        if event["kind"] == "pairing":
            self.pairing = deepcopy(event)
        elif event["kind"] == "selection" and self.accepts_frame(event.get("frame")):
            self.selected_object = event.get("object_id")
            self.selection_frame = deepcopy(event["frame"])
        elif event["kind"] == "error":
            self.viewer_error = event["message"]
