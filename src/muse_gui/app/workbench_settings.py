from pathlib import Path

from PySide6.QtCore import QSettings
from qfluentwidgets import Theme, setTheme


class WorkbenchSettings:
    def __init__(self, settings: QSettings | None = None):
        self.settings = settings if settings is not None else QSettings("Muse", "ResearchWorkbench")

    @property
    def theme(self) -> str:
        return self.settings.value("appearance/theme", "auto")

    def set_theme(self, theme: str) -> None:
        setTheme({"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}[theme], save=False)
        self.settings.setValue("appearance/theme", theme)

    @property
    def recent_files(self) -> list[str]:
        return self.settings.value("files/recent", [], type=list)

    def remember_file(self, path: Path) -> None:
        path = str(Path(path).absolute())
        self.settings.setValue("files/recent", [path] + [item for item in self.recent_files if item != path][:9])

    def save_window(self, window, view) -> None:
        self.settings.setValue("window/geometry", window.saveGeometry())
        self.settings.setValue("layout/horizontal", view.horizontal_splitter.saveState())
        self.settings.setValue("layout/vertical", view.vertical_splitter.saveState())
        self.settings.setValue("layout/output_collapsed", view.output_collapsed)
        self.settings.setValue("layout/tree_visible", not view.tree_panel.isHidden())
        self.settings.setValue("layout/inspector_visible", not view.inspector_panel.isHidden())
        self.settings.sync()

    def restore_window(self, window, view) -> None:
        self.set_theme(self.theme)
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            window.restoreGeometry(geometry)
        else:
            window.move(window.screen().availableGeometry().center() - window.rect().center())
        for name, splitter in (("horizontal", view.horizontal_splitter), ("vertical", view.vertical_splitter)):
            state = self.settings.value(f"layout/{name}")
            if state is not None:
                splitter.restoreState(state)
        view.set_output_collapsed(self.settings.value("layout/output_collapsed", True, type=bool))
        view.tree_panel.setVisible(self.settings.value("layout/tree_visible", True, type=bool))
        view.inspector_panel.setVisible(self.settings.value("layout/inspector_visible", True, type=bool))
