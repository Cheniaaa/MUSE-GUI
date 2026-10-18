# -*- coding: utf-8 -*-
import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QToolBar, QLineEdit, QStatusBar
from PySide6.QtWebEngineWidgets import QWebEngineView


class BrowserWindow(QMainWindow):
    def __init__(self, home: str = "https://pywebview.flowrl.com"):
        super().__init__()
        self.home = home

        self.setWindowTitle("PySide6 QWebEngineView 示例")
        self.resize(1200, 800)

        # 核心控件：网页视图
        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)

        # 地址栏
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("输入网址后回车")
        self.url_edit.returnPressed.connect(self.navigate_to_input)
        self.view.urlChanged.connect(self.on_url_changed)

        # 工具栏
        toolbar = QToolBar("导航", self)
        self.addToolBar(toolbar)
        self._add_action(toolbar, "←", self.view.back)
        self._add_action(toolbar, "→", self.view.forward)
        self._add_action(toolbar, "⟳", self.view.reload)
        self._add_action(toolbar, "⌂", lambda: self.view.load(QUrl(self.home)))
        toolbar.addWidget(self.url_edit)

        # 状态栏
        self.setStatusBar(QStatusBar())
        self.view.loadStarted.connect(lambda: self.statusBar().showMessage("加载中…"))
        self.view.loadFinished.connect(self.on_load_finished)

        # 加载首页
        self.view.load(QUrl(self.home))

    def _add_action(self, toolbar: QToolBar, text: str, slot):
        action = QAction(text, self)
        action.triggered.connect(slot)
        toolbar.addAction(action)

    def navigate_to_input(self):
        text = self.url_edit.text().strip()
        if not text:
            return
        if "://" not in text:
            text = "https://" + text
        self.view.load(QUrl(text))

    def on_url_changed(self, url: QUrl):
        self.url_edit.setText(url.toString())

    def on_load_finished(self, ok: bool):
        self.statusBar().showMessage("加载完成" if ok else "加载失败")
        title = self.view.title()
        self.setWindowTitle(title or "PySide6 QWebEngineView 示例")

        # 演示：页面加载完成后执行 JavaScript
        if ok:
            self.view.page().runJavaScript(
                "document.title", lambda result: print("页面标题:", result)
            )


def main():
    # 某些 Linux 虚拟机/远程桌面环境下如果报 OpenGL 相关错误，
    # 可以取消下面这行注释：
    # QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv)
    window = BrowserWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
