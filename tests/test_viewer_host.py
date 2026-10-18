from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from PySide6.QtCore import QUrl
from PySide6.QtTest import QSignalSpy


@contextmanager
def webpage():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<!doctype html><html><body>real WebEngine identity page</body></html>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join(3)


def javascript(page, script, wait_until):
    values = []
    page.runJavaScript(script, values.append)
    wait_until(lambda: bool(values))
    return values[0]


def test_real_profiles_isolate_identity_and_only_inject_current_origin(qapp, wait_until):
    from muse_gui.widgets.viewer_host import ViewerHost
    with webpage() as origin, webpage() as other:
        left, right = ViewerHost(), ViewerHost()
        a, b = QSignalSpy(left.page_ready), QSignalSpy(right.page_ready)
        left.open(origin, 'left-id')
        right.open(origin, 'right-id')
        wait_until(lambda: a.count() == b.count() == 1, 20000)
        assert left.profile.isOffTheRecord() and right.profile.isOffTheRecord()
        assert left.profile is not right.profile
        assert javascript(left.page, 'localStorage.getItem("viser-user:muse_view_id")', wait_until) == 'left-id'
        assert javascript(right.page, 'localStorage.getItem("viser-user:muse_view_id")', wait_until) == 'right-id'
        left.page.load(QUrl(other))
        wait_until(lambda: left.page.url().toString().rstrip('/') == other and a.count() == 2, 20000)
        assert javascript(left.page, 'JSON.stringify(localStorage.getItem("viser-user:muse_view_id"))', wait_until) == 'null'
        left.unload()
        right.unload()
        wait_until(lambda: not left.releasing and not right.releasing)
        left.close()
        right.close()


def test_new_viewer_origin_reinjects_same_session_and_page_dies_before_profile(qapp, wait_until):
    from muse_gui.widgets.viewer_host import ViewerHost
    with webpage() as first, webpage() as second:
        host = ViewerHost()
        ready = QSignalSpy(host.page_ready)
        host.open(first, 'same-session')
        wait_until(lambda: ready.count() == 1, 20000)
        destroyed = []
        host.page.destroyed.connect(lambda: destroyed.append('page'))
        host.profile.destroyed.connect(lambda: destroyed.append('profile'))
        host.open(second, 'same-session')
        wait_until(lambda: ready.count() == 2 and destroyed == ['page', 'profile'], 20000)
        assert javascript(host.page, 'localStorage.getItem("viser-user:muse_view_id")', wait_until) == 'same-session'
        assert host.url == second
        host.unload()
        wait_until(lambda: not host.releasing)
        host.close()
