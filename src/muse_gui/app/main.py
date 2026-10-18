import sys

from PySide6.QtWidgets import QApplication
from qfluentwidgets import setThemeColor

from muse_gui.experiments.experiment_draft import ExperimentDraft
from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.experiments.configuration_controller import ConfigurationController
from muse_gui.experiments.research_config_view import ResearchConfigView
from .main_window import MuseWindow
from .workbench_settings import WorkbenchSettings
from .workbench_controller import WorkbenchController


def mount_configuration(window, draft, connection):
    view = ResearchConfigView(window.workbench)
    window.workbench.set_configuration_view(view)
    controller = ConfigurationController(draft, connection, view)
    controller.flush_requested.connect(window.workbench.flush_inputs)
    def locate_field(widget):
        window.workbench.set_page("config")
        window.workbench.config_scroll.ensureWidgetVisible(widget)

    view.field_located.connect(locate_field)

    def show_report(report):
        complete = report["resolved_experiment"] is not None
        window.workbench.show_message(f"revision {draft.validated_revision} · " + ("Experiment 解析完整；RunOptions 尚未提交校验，尚未创建 Run。" if complete else "仍有缺项或错误；草稿可继续保存。"))
        if report["errors"] or any(section["missing"] or section["errors"] for section in report["sections"]):
            window.workbench.set_output_collapsed(False)

    controller.report_received.connect(show_report)

    def show_connection(state):
        names = {"unconnected": "未连接", "checking": "检查中", "connected": "已连接", "disconnected": "已断开", "failed": "连接失败"}
        label = names.get(state["state"], state["state"])
        detail = "catalog " + state["catalog_id"][:8] if state["state"] == "connected" else "本地草稿 · 可离线保存"
        window.workbench.connection_label.setText(label + "\n" + detail)
        window.workbench.connection_label.setToolTip(f"catalog_id: {state['catalog_id']}\ninstance_id: {state['instance_id']}\ngeneration: {state['generation']}")

    connection.state_changed.connect(show_connection)
    show_connection(connection.state)
    window.configuration_view = view
    window.configuration_controller = controller
    return controller


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("Muse")
    app.setApplicationName("ResearchWorkbench")
    setThemeColor("#2878cc", save=False)
    draft = ExperimentDraft()
    window = MuseWindow(draft, WorkbenchSettings())
    connection = ConnectionController(app)
    configuration = mount_configuration(window, draft, connection)
    window.connection_controller = connection
    window.controller = WorkbenchController(window, draft, connection, configuration)
    window.controller.mount_views()
    window.show()
    # 首次配置可直接访问；不猜测机器路径或在连接时安装环境。
    if not window.preferences.settings.contains('backend/profile'):
        window.switchTo(window.settings_host)
    return app.exec()
