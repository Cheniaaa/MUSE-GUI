from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from qfluentwidgets import (
    FluentWindow, FluentIcon, setTheme, Theme
)

from src.views.home_view import HomePage
from src.views.drone_view import DroneConfigPage


class FlySimWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FlySim")
        self.resize(1200, 900)
        icon_path = Path(__file__).resolve().parents[2] / "resources" / "Icon" / "AppIcon.png"
        self.app_icon = QIcon(str(icon_path))
        self.setWindowIcon(self.app_icon)

        # 设置主题色
        setTheme(Theme.AUTO)


        # 创建首页
        self.home_page = HomePage()
        self.addSubInterface(self.home_page, FluentIcon.HOME, "无人机仿真")

        self.model_page = DroneConfigPage()
        self.addSubInterface(self.model_page, FluentIcon.AIRPLANE, "模型配置")

        # self.setting_page = Setting()
        # self.addSubInterface(self.setting_page, FluentIcon.SETTING, "设置")

        # 设置启动时的窗口位置居中
        desktop = QApplication.screens()[0].availableGeometry()
        self.move(desktop.width() // 2 - self.width() // 2, desktop.height() // 2 - self.height() // 2)
        # self.showFullScreen()
        # self.themeListener.start()

    def closeEvent(self, event):
        if self.home_page.prepare_close():
            super().closeEvent(event)
            event.accept()
            return
        event.ignore()
