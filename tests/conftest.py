import os
import time
from contextlib import contextmanager
from threading import Thread

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from websockets.sync.server import serve


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Muse-G1-tests")
    return app


@pytest.fixture
def wait_until(qapp):
    def wait(predicate, timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate():
            assert time.monotonic() < deadline, "等待 Qt 条件超时"
            QTest.qWait(5)
        qapp.processEvents()
    return wait


@pytest.fixture
def websocket_server():
    @contextmanager
    def start(handler):
        with serve(handler, "127.0.0.1", 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield server.socket.getsockname()[1]
            finally:
                server.shutdown()
                thread.join(3)
                assert not thread.is_alive()
    return start
