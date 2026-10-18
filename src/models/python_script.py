from PySide6.QtCore import QObject, Signal
from typing import List, Dict
import os


class PythonScriptModel(QObject):
    """ Python脚本模型 """
    script_loaded = Signal(str, str)
    script_added_to_history = Signal(dict)
    data_changed = Signal()

    def __init__(self):
        super().__init__()
        self.current_script_path = ""
        self.current_script_name = ""
        self.script_history: List[Dict] = []

    def load_script(self, file_path: str):
        if os.path.exists(file_path):
            self.current_script_path = file_path
            self.current_script_name = os.path.basename(file_path)
            self.script_loaded.emit(file_path, self.current_script_name)
            return True
        return False

    def add_to_history(self, script_path: str, status: str, output: str):
        history_item = {
            'path': script_path,
            'name': os.path.basename(script_path),
            'status': status,
            'output': output[:200]
        }
        self.script_history.insert(0, history_item)
        if len(self.script_history) > 10:
            self.script_history.pop()
        self.script_added_to_history.emit(history_item)

    def get_current_script(self) -> str:
        return self.current_script_path
