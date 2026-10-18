import sys

from PySide6.QtWidgets import QApplication
from qfluentwidgets import setThemeColor

from muse_gui.experiments.experiment_draft import ExperimentDraft
from .main_window import MuseWindow
from .workbench_settings import WorkbenchSettings


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("Muse")
    app.setApplicationName("ResearchWorkbench")
    setThemeColor("#2878cc", save=False)
    draft = ExperimentDraft()
    window = MuseWindow(draft, WorkbenchSettings())
    window.show()
    return app.exec()
