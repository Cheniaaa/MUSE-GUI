"""Windows→WSL 本机进程入口；只管理客户端命令，Server 始终由后端拥有。"""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QStandardPaths, QTimer, Signal


def wsl_arguments(profile: dict, command: str, instance_id: str | None = None) -> list[str]:
    arguments = ["--distribution", profile["distribution"], "--user", profile["user"],
        "--cd", profile["project_dir"], "--exec", "bash", "-c",
        'source "$1/bin/activate" && shift && exec python -m muse_server.local_backend "$@"',
        "muse-launcher", profile["venv_dir"], command, "--data-dir", profile["data_dir"]]
    if command == "start":
        arguments += ["--host", profile["host"], "--port", str(profile["http_port"]),
                      "--access-host", profile["host"], "--viewer-port", str(profile["viewer_port"])]
    elif command == "stop":
        arguments += ["--instance-id", instance_id]
    return arguments


class LocalBackendLauncher(QObject):
    state_changed = Signal(dict)
    result_received = Signal(str, dict)

    def __init__(self, connection, parent=None, *, log_dir: Path | None = None):
        super().__init__(parent)
        self.connection = connection
        self.log_dir = Path(log_dir) if log_dir is not None else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "backend-logs"
        self._state = {"state": "idle", "error": None}
        self._process = None
        self._action = None
        self._profile = None
        self._closing = False
        self._detached = False
        self._generation = None
        self._expected_instance = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.setInterval(200)
        self._poll_timer.timeout.connect(lambda: self._run("status"))
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.setInterval(30000)
        self._startup_timer.timeout.connect(lambda: self._fail({
            "kind": "timeout", "message": "本机后端启动/连接未在 30 秒内确认，请查看启动日志；不会强杀服务。"}))
        connection.state_changed.connect(self._connection_changed)

    @property
    def state(self) -> dict:
        return deepcopy(self._state)

    def start(self, profile: dict) -> None:
        self._begin("start", profile)
        self._detached = False
        self._startup_timer.start()
        self._run("status")

    def status(self, profile: dict) -> None:
        self._begin("status", profile)
        self._run("status")

    def stop(self, profile: dict, instance_id: str) -> None:
        if not instance_id:
            raise ValueError("停止本机后端需要已确认的 instance_id")
        self._begin("stop", profile)
        self._run("stop", instance_id)

    def close(self) -> bool:
        """只结束本客户端流程；False 表示宿主须保留 launcher，等待在途 CLI 自然结束。"""
        self._closing = True
        self._poll_timer.stop()
        self._startup_timer.stop()
        self._action = None
        return self._process is None

    def _begin(self, action, profile):
        if self._closing:
            raise ValueError("本机客户端已关闭")
        if self._action is not None or self._process is not None:
            raise ValueError("已有本机操作尚未结束")
        if profile.get("mode", "local") != "local":
            raise ValueError("仅连接模式不提供本机启动或停止")
        for name in ("distribution", "user", "project_dir", "venv_dir", "data_dir", "host"):
            if not isinstance(profile.get(name), str) or not profile[name]:
                raise ValueError(f"本机配置缺少 {name}")
        for name in ("http_port", "viewer_port"):
            if type(profile.get(name)) is not int or not 1 <= profile[name] <= 65535:
                raise ValueError(f"{name} 必须为有效端口")
        self._action, self._profile = action, dict(profile)
        host = profile["host"]
        self._profile["base_url"] = f"http://{'[' + host + ']' if ':' in host else host}:{profile['http_port']}"
        self._generation = self._expected_instance = None
        self._set_state(state={"start": "starting", "status": "checking", "stop": "stopping"}[action], error=None)

    def _new_process(self, command, instance_id=None):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        token = f"muse-{command}-{uuid4().hex}"
        out, err = self.log_dir / (token + ".stdout.log"), self.log_dir / (token + ".stderr.log")
        process = QProcess(self)
        process.setProgram("wsl.exe")
        process.setArguments(wsl_arguments(self._profile, command, instance_id))
        process.setStandardInputFile(QProcess.nullDevice())
        self._set_state(stdout_log=str(out), stderr_log=str(err))
        return process, out, err

    def _run(self, command, instance_id=None):
        if self._closing or self._action is None:
            return
        try:
            process, out, err = self._new_process(command, instance_id)
            out.write_bytes(b"")
            err.write_bytes(b"")
        except OSError as error:
            self._fail({"kind": "local", "message": str(error)})
            return
        self._process = process
        self._stdout, self._stderr = bytearray(), bytearray()
        process.readyReadStandardOutput.connect(lambda: self._drain(process, out, False))
        process.readyReadStandardError.connect(lambda: self._drain(process, err, True))
        process.finished.connect(lambda code, status: self._finished(process, out, err, code, status))
        process.errorOccurred.connect(lambda error: self._process_error(process, error))
        process.start()

    def _drain(self, process, path, stderr):
        data = bytes(process.readAllStandardError() if stderr else process.readAllStandardOutput())
        (self._stderr if stderr else self._stdout).extend(data)
        with path.open("ab") as output:
            output.write(data)

    def _process_error(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._process = None
            process.deleteLater()
            if not self._closing:
                self._fail({"kind": "process", "message": process.errorString()})

    def _finished(self, process, out, err, code, exit_status):
        self._drain(process, out, False)
        self._drain(process, err, True)
        self._process = None
        process.deleteLater()
        if self._closing or self._action is None:
            return
        try:
            text = self._stdout.decode("utf-8")
            stderr = self._stderr.decode("utf-8")
            if code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
                self._fail({"kind": "process", "exit_code": code, "stdout": text, "stderr": stderr,
                            "message": "本机命令失败；后端状态未确认，不能视为已停止。"})
                return
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("state") not in ("running", "stopped", "stale"):
                raise ValueError("本机命令返回的状态格式错误")
            if data["state"] == "running" and not isinstance(data.get("instance_id"), str):
                raise ValueError("本机身份缺少 instance_id")
        except (UnicodeDecodeError, ValueError) as error:
            self._fail({"kind": "protocol", "message": str(error)})
            return
        if self._action == "start":
            if data["state"] == "running":
                self._expected_instance = data["instance_id"]
                self._generation = None
                self.connection.connect(self._profile)
                self._generation = self.connection.state["generation"]
            elif not self._detached:
                self._detach()
            else:
                self._poll_timer.start()
        else:
            if self._action == "stop" and data["state"] != "stopped":
                self._fail({"kind": "protocol", "message": "stop 未确认后端停止", "data": data})
                return
            if self._action == "stop" and self.connection.state["instance_id"] == data.get("instance_id"):
                self.connection.disconnect()
            self._complete(data)

    def _detach(self):
        try:
            process, out, err = self._new_process("start")
            process.setStandardOutputFile(str(out))
            process.setStandardErrorFile(str(err))
            started = process.startDetached()
        except OSError as error:
            self._fail({"kind": "local", "message": str(error)})
            return
        process.deleteLater()
        if not started:
            self._fail({"kind": "process", "message": "WSL 脱离启动失败，请查看启动日志"})
            return
        self._detached = True
        self._set_state(start_stdout_log=str(out), start_stderr_log=str(err))
        self._poll_timer.start()

    def _connection_changed(self, state):
        if (self._closing or self._action != "start" or self._generation is None
                or state["generation"] != self._generation):
            return
        if state["state"] == "connected":
            if state["instance_id"] != self._expected_instance:
                self.connection.disconnect()
                self._fail({"kind": "instance_mismatch", "message": "此地址的实例与 Linux 数据目录身份不同，拒绝连接。"})
                return
            self._complete({**state, "state": "running"})
        elif state["state"] == "failed":
            if self._detached and state["error"]["kind"] == "connection":
                self._generation = None
                self._poll_timer.start()
            else:
                self._fail(state["error"])

    def _set_state(self, **updates):
        self._state.update(updates)
        self.state_changed.emit(self.state)

    def _complete(self, data):
        action, self._action = self._action, None
        self._startup_timer.stop()
        self._poll_timer.stop()
        self._set_state(**{**data, "error": None})
        self.result_received.emit(action, {"ok": True, "data": data})

    def _fail(self, error):
        action, self._action = self._action, None
        self._startup_timer.stop()
        self._poll_timer.stop()
        self._set_state(state="failed", error=error)
        if action is not None:
            self.result_received.emit(action, {"ok": False, "error": error})
