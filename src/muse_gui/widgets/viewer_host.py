"""隔离的真实WebEngine视口；不拥有Server或共享Viser生命周期。"""
import json
from urllib.parse import urlsplit

from PySide6.QtCore import QCoreApplication, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget


class ViewerHost(QWidget):
    page_ready = Signal(str, str)
    page_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.profile = self.page = self.web_view = None
        self.url = self.view_id = None
        self._retired = []

    @property
    def releasing(self) -> bool:
        return bool(self._retired)

    def open(self, url: str, view_id: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or not view_id:
            raise ValueError('视口需要HTTP(S)地址和后端view_id')
        self.unload()
        self.url, self.view_id = url, view_id
        # app宿主保证窗口关闭后的DeferredDelete仍有生命周期所有者。
        self.profile = profile = QWebEngineProfile(QCoreApplication.instance())
        self.page = page = QWebEnginePage(profile, QCoreApplication.instance())
        script = QWebEngineScript()
        script.setName('Muse view identity')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        script.setSourceCode('if(location.origin===' + json.dumps(origin) + ') localStorage.setItem('
                             '"viser-user:muse_view_id",' + json.dumps(view_id) + ');')
        page.scripts().insert(script)
        self.web_view = view = QWebEngineView(self)
        view.setPage(page)
        view.loadFinished.connect(lambda ok: self._loaded(page, ok))
        self.layout().addWidget(view)
        view.load(QUrl(url))

    def _loaded(self, page, ok):
        if page is not self.page:
            return
        if ok:
            self.page_ready.emit(self.url, self.view_id)
        else:
            self.page_failed.emit('视口页面加载失败：' + self.url)

    def unload(self) -> None:
        if self.page is None:
            return
        page, profile, view = self.page, self.profile, self.web_view
        self.page = self.profile = self.web_view = None
        self.url = self.view_id = None
        view.stop()
        view.hide()
        self.layout().removeWidget(view)
        retired = (page, profile, view)
        self._retired.append(retired)
        page.destroyed.connect(profile.deleteLater)
        profile.destroyed.connect(lambda: self._retired.remove(retired))
        page.deleteLater()
        view.deleteLater()

    def closeEvent(self, event):
        self.unload()
        super().closeEvent(event)
