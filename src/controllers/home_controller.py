import os

from PySide6.QtCore import QObject, Signal, QThread
from PySide6.QtWidgets import QFileDialog
import subprocess
import sys


class PythonScriptController(QObject):
    """脚本控制器"""

    execution_started = Signal()
    execution_stopped = Signal()
    status_updated = Signal(str)

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.runner = None

    def load_script_file(self) -> bool:
        file_path, _ = QFileDialog.getOpenFileName(
            None, "选择Python脚本", "", "Python文件 (*.py);;所有文件 (*.*)"
        )
        if file_path:
            success = self.model.load_script(file_path)
            if success:
                self.status_updated.emit(f"已加载: {os.path.basename(file_path)}")
            return success
        return False

    def execute_script(self, args: str = ""):
        script_path = self.model.get_current_script()
        if not script_path:
            self.status_updated.emit("错误: 请先加载脚本文件")
            return

        if self.runner and self.runner.isRunning():
            self.status_updated.emit("错误: 已有脚本正在运行")
            return

        self.runner = ScriptRunner(script_path, args)
        self.execution_started.emit()
        self.status_updated.emit("正在执行...")
        self.runner.start()

    def stop_execution(self):
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self.execution_stopped.emit()
            self.status_updated.emit("已停止执行")

    def get_runner(self):
        return self.runner


class ScriptRunner(QThread):
    """脚本执行线程"""

    output_received = Signal(str)
    error_received = Signal(str)
    execution_finished = Signal(int, str)

    def __init__(self, script_path: str, args: str = ""):
        super().__init__()
        self.script_path = script_path
        self.args = args
        self.process = None

    def run(self):
        try:
            cmd = [sys.executable, self.script_path]
            if self.args:
                cmd.extend(self.args.split())

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            for line in self.process.stdout:
                self.output_received.emit(line)

            self.process.wait()
            errors = self.process.stderr.read()
            if errors:
                self.error_received.emit(errors)

            status = "成功" if self.process.returncode == 0 else "失败"
            self.execution_finished.emit(self.process.returncode, status)

        except (OSError, subprocess.SubprocessError) as e:
            self.error_received.emit(f"执行错误: {str(e)}")
            self.execution_finished.emit(-1, "错误")

    def stop(self):
        if self.process:
            self.process.terminate()
