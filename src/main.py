from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from src.views.main_window import FlySimWindow


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    fly_sim_window = FlySimWindow()
    fly_sim_window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
