import json
from pathlib import Path
import sys

import httpx2
import pytest
from PySide6.QtCore import QProcess, QSettings
from PySide6.QtTest import QSignalSpy

from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.backend.muse_client import MuseClient


def profile():
    return dict(mode="local", distribution="Ubuntu test", user="researcher", project_dir="/研究 目录; $(echo NO)",
                venv_dir='/研究 env "quoted"', data_dir="/数据 空格 & untouched", host="127.0.0.1",
                http_port=18000, viewer_port=18080, base_url="http://127.0.0.1:18000", catalog_id="catalog-a")


RUNNING = dict(state="running", instance_id="instance-1", pid=123, process_start="boot:123",
               address="http://127.0.0.1:18000", shutdown_timeout=15.)


@pytest.fixture
def connection(tmp_path, wait_until):
    def handle(request):
        if request.url.path == "/health":
            return httpx2.Response(200, json=dict(status="ok", api_revision=2, catalog_id="catalog-a",
                                                 instance_id="instance-1", platform_revision="test", gpu_busy=False))
        return httpx2.Response(200, json={"items": [], "next_offset": None})
    transports = []
    def factory(url):
        transport = httpx2.Client(transport=httpx2.MockTransport(handle))
        transports.append(transport)
        return MuseClient(url, client=transport)
    controller = ConnectionController(client_factory=factory, cache_dir=tmp_path / "cache")
    yield controller
    wait_until(controller.close)
    for transport in transports:
        transport.close()


@pytest.fixture
def commands(monkeypatch):
    from muse_gui.backend import local_backend
    responses = []
    calls, detached = [], []
    class Process(QProcess):
        def setProgram(self, program):
            assert program == "wsl.exe"
            super().setProgram(sys.executable)

        def setArguments(self, arguments):
            calls.append(arguments)
            command = arguments[arguments.index("muse-launcher") + 2]
            if command == "start":
                detached.append(arguments)
                code = "print('detached server log')"
            else:
                output, error, exit_code = responses.pop(0)
                code = f"import sys; print({output!r}); print({error!r},file=sys.stderr); sys.exit({exit_code})"
            super().setArguments(["-u", "-c", code])
    monkeypatch.setattr(local_backend, "QProcess", Process)
    return responses, calls, detached


def test_launcher_quotes_paths_as_arguments():
    from muse_gui.backend.local_backend import wsl_arguments
    value = profile()
    arguments = wsl_arguments(value, "start")
    assert arguments == ["--distribution", value["distribution"], "--user", value["user"],
        "--cd", value["project_dir"], "--exec", "bash", "-c",
        'source "$1/bin/activate" && shift && exec python -m muse_server.local_backend "$@"',
        "muse-launcher", value["venv_dir"], "start", "--data-dir", value["data_dir"],
        "--host", "127.0.0.1", "--port", "18000", "--access-host", "127.0.0.1", "--viewer-port", "18080"]
    stop = wsl_arguments(value, "stop", "identity ; $()")
    assert stop[-5:] == ["stop", "--data-dir", value["data_dir"], "--instance-id", "identity ; $()"]


def test_start_connects_existing_matching_instance(connection, commands, tmp_path, wait_until):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, calls, detached = commands
    responses.append((json.dumps(RUNNING), "", 0))
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path / "logs")
    received = QSignalSpy(launcher.result_received)
    launcher.start(profile())
    wait_until(lambda: received.count() > 0)
    assert received.at(0)[0] == "start" and received.at(0)[1]["ok"]
    assert launcher.state["state"] == "running"
    assert connection.state["instance_id"] == "instance-1"
    assert not detached
    assert launcher.close()


def test_existing_different_catalog_is_refused(connection, commands, tmp_path, wait_until):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, _, detached = commands
    responses.append((json.dumps(RUNNING), "", 0))
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path)
    value = {**profile(), "catalog_id": "other-catalog"}
    received = QSignalSpy(launcher.result_received)
    launcher.start(value)
    wait_until(lambda: received.count() > 0)
    assert not received.at(0)[1]["ok"]
    assert launcher.state["error"]["kind"] == "catalog_mismatch"
    assert not detached and connection.state["state"] == "failed"
    assert launcher.close()


def test_health_must_match_linux_status_instance(connection, commands, tmp_path, wait_until):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, _, detached = commands
    responses.append((json.dumps({**RUNNING, "instance_id": "different-process"}), "", 0))
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path)
    received = QSignalSpy(launcher.result_received)
    launcher.start(profile())
    wait_until(lambda: received.count() > 0)
    assert not received.at(0)[1]["ok"]
    assert launcher.state["error"]["kind"] == "instance_mismatch"
    assert not detached and connection.state["state"] != "connected"
    assert launcher.close()


def test_detached_start_is_bool_and_close_never_stops_backend(connection, commands, tmp_path, wait_until):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, calls, detached = commands
    responses.extend([(json.dumps({"state": "stopped"}), "", 0), (json.dumps(RUNNING), "", 0)])
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path / "logs")
    received = QSignalSpy(launcher.result_received)
    launcher.start(profile())
    wait_until(lambda: received.count() > 0)
    assert received.at(0)[1]["ok"] and len(detached) == 1
    assert launcher.close()
    assert not any("stop" in command for command in calls)
    wait_until(lambda: any(path.read_text() for path in (tmp_path / "logs").glob("*start*.stdout.log")))


@pytest.mark.parametrize("output,error,exit_code,kind", [
    ("not-json", "", 0, "protocol"),
    (json.dumps({"state": "stopped"}), json.dumps({"errors": [{"field": "instance_id", "message": "wrong"}]}), 2, "process"),
    (json.dumps({"state": "unconfirmed", "reason": "still cleaning"}), "", 1, "process"),
])
def test_stop_failure_does_not_claim_stopped(connection, commands, tmp_path, wait_until, output, error, exit_code, kind):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, _, _ = commands
    responses.append((output, error, exit_code))
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path)
    received = QSignalSpy(launcher.result_received)
    launcher.stop(profile(), "instance-1")
    wait_until(lambda: received.count() > 0)
    assert not received.at(0)[1]["ok"] and received.at(0)[1]["error"]["kind"] == kind
    assert launcher.state["state"] != "stopped"
    assert launcher.close()


def test_settings_persist_profile_and_emit_intents(qapp, tmp_path):
    from muse_gui.app.settings_view import SettingsView
    from muse_gui.app.workbench_settings import WorkbenchSettings
    settings = WorkbenchSettings(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    view = SettingsView(settings)
    view.set_profile(profile())
    saved, started, stopped = QSignalSpy(view.profile_changed), QSignalSpy(view.start_requested), QSignalSpy(view.stop_requested)
    view.save_button.click()
    assert saved.at(0)[0] == profile()
    view.start_button.click()
    assert started.at(0)[0] == profile()
    view.set_connection_state({"state": "connected", "instance_id": "verified-instance", "catalog_id": "catalog-a"})
    view.stop_button.click()
    assert stopped.at(0) == [profile(), "verified-instance"]
    reopened = SettingsView(settings)
    assert reopened.profile() == profile()
    reopened.set_profile({**profile(), "mode": "connect", "base_url": "http://remote"})
    assert reopened.stop_button.isHidden() and reopened.start_button.isHidden()
    assert "http://remote" == reopened.profile()["base_url"]
    view.close()
    reopened.close()


def test_close_preserves_inflight_cli_until_natural_completion(connection, commands, tmp_path, wait_until):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, calls, detached = commands
    responses.append((json.dumps(RUNNING), "", 0))
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path)
    received = QSignalSpy(launcher.result_received)
    launcher.status(profile())
    assert launcher.close() is False
    wait_until(launcher.close)
    assert received.count() == 0 and not detached
    assert json.loads(next(tmp_path.glob("*.stdout.log")).read_text()) == RUNNING
    assert not any("stop" in command for command in calls)


def test_local_profile_derives_http_address_from_host_and_port(connection, commands, tmp_path, wait_until):
    from muse_gui.backend.local_backend import LocalBackendLauncher
    responses, _, _ = commands
    responses.append((json.dumps(RUNNING), "", 0))
    launcher = LocalBackendLauncher(connection, log_dir=tmp_path)
    received = QSignalSpy(launcher.result_received)
    value = profile()
    del value["base_url"]
    launcher.start(value)
    wait_until(lambda: received.count() > 0)
    assert received.at(0)[1]["ok"]
    assert launcher.close()


def test_settings_stop_can_use_verified_linux_status(qapp, tmp_path):
    from muse_gui.app.settings_view import SettingsView
    from muse_gui.app.workbench_settings import WorkbenchSettings
    view = SettingsView(WorkbenchSettings(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)))
    view.set_profile(profile())
    stopped = QSignalSpy(view.stop_requested)
    view.set_backend_state(RUNNING)
    assert view.stop_button.isEnabled()
    view.stop_button.click()
    assert stopped.at(0)[1] == "instance-1"
    view.close()


def test_port_editors_display_the_full_port_value(qapp, tmp_path):
    from muse_gui.app.settings_view import SettingsView
    from muse_gui.app.workbench_settings import WorkbenchSettings
    view = SettingsView(WorkbenchSettings(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)))
    view.set_profile(profile())
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    for name in ("http_port", "viewer_port"):
        field = view.fields[name]
        assert field.lineEdit().width() >= field.fontMetrics().horizontalAdvance(field.text())
    view.close()
