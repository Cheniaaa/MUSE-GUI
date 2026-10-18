"""有界显示模型；完整原始记录始终属于 Server artifact。"""

import base64
import codecs
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

import numpy as np


Axis = Literal["steps", "updates", "sim_time", "wall_time"]
Statistic = Literal["mean", "min", "max"]
AXIS_LABELS = {"steps": "decision steps", "updates": "learning updates",
               "sim_time": "simulation time (s)", "wall_time": "Unix wall time (s)"}
MAX_LOG_CHARACTERS = 1_048_576


@dataclass(frozen=True)
class MetricSelection:
    path: tuple
    axis: Axis
    indices: tuple[int, ...] | None = None
    statistic: Statistic | None = None
    unit: str | None = None

    def can_overlay(self, other: "MetricSelection") -> bool:
        return (self.unit is not None and self.unit == other.unit and self.path == other.path
                and self.axis == other.axis and self.indices == other.indices
                and self.statistic == other.statistic)


class MetricSeries:
    def __init__(self):
        self.records = deque(maxlen=10000)
        self.total_records = 0
        self.next_offset = 0

    def append_page(self, page: dict) -> None:
        self.records.extend(deepcopy(page["records"]))
        self.total_records += len(page["records"])
        self.next_offset = page["next_offset"]

    @property
    def paths(self) -> list[tuple]:
        paths = set()

        def visit(value, path):
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, (*path, key))
            elif isinstance(value, (int, float, list)) and not isinstance(value, bool):
                paths.add(path)

        for record in self.records:
            visit(record, ())
        return sorted(paths)

    def select(self, path: tuple, axis: Axis, indices: tuple[int, ...] | None,
               statistic: Statistic | None) -> tuple[np.ndarray, np.ndarray]:
        if axis not in AXIS_LABELS:
            raise ValueError("未知横轴语义")
        if path not in self.paths:
            raise ValueError("指标路径尚未提供")
        if indices is not None and statistic is not None:
            raise ValueError("索引和统计只能选择一种")
        if statistic not in (None, "mean", "min", "max"):
            raise ValueError("未知统计方式")
        x, y = [], []
        for record in self.records:
            axis_record = record["payload"] if axis in ("steps", "updates") else record
            x.append(axis_record.get(axis, np.nan))
            value = record
            for key in path:
                if not isinstance(value, dict) or key not in value:
                    value = None
                    break
                value = value[key]
            if value is None:
                y.append(np.nan)
                continue
            array = np.asarray(value, dtype=float)
            if array.ndim:
                if indices is None and statistic is None:
                    raise ValueError("数组指标需明确索引或 mean/min/max 统计")
                if indices is not None:
                    if len(indices) != array.ndim or any(i < 0 or i >= n for i, n in zip(indices, array.shape)):
                        raise ValueError(f"数组索引越界，shape={array.shape}")
                    value = array[indices]
                else:
                    if not array.size:
                        value = np.nan
                    else:
                        value = {"mean": np.mean, "min": np.min, "max": np.max}[statistic](array)
            elif indices is not None or statistic is not None:
                raise ValueError("标量指标不接受数组索引或统计")
            y.append(float(value))
        return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


class LogBuffer:
    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lines = deque(maxlen=5000)
        self.diagnostics = deque(maxlen=200)
        self.next_offset = 0
        self.truncated = False
        self.availability = "not_started"

    @property
    def text(self) -> str:
        return "".join(self._lines)

    def _append_text(self, text):
        if not text:
            return
        if self._lines and not self._lines[-1].endswith(("\n", "\r")):
            text = self._lines.pop() + text
        lines = text.splitlines(keepends=True)
        if len(self._lines) + len(lines) > 5000:
            self.truncated = True
        self._lines.extend(lines)
        excess = sum(map(len, self._lines)) - MAX_LOG_CHARACTERS
        if excess > 0:
            self.truncated = True
            while excess > 0:
                first = self._lines.popleft()
                if len(first) > excess:
                    self._lines.appendleft(first[excess:])
                    break
                excess -= len(first)

    def _decode(self, data, start, final=False):
        state = self._decoder.getstate()
        strict = codecs.getincrementaldecoder("utf-8")()
        strict.setstate(state)
        try:
            strict.decode(data, final=final)
        except UnicodeDecodeError as error:
            self.diagnostics.append(
                f"UTF-8 错误 byte {start - len(state[0]) + error.start}: "
                f"{error.object[error.start:error.end].hex()} ({error.reason})；显示替换字符，原始 bytes 请下载 artifact")
        self._append_text(self._decoder.decode(data, final=final))

    def append_page(self, page: dict) -> None:
        self.availability = page["availability"]
        if "gap" in page:
            gap = page["gap"]
            self._decode(b"", self.next_offset, final=True)
            self._decoder.reset()
            message = f"日志缺口 bytes [{gap['previous_offset']}, {gap['next_offset']})；可用 HTTP 历史分页或原始 artifact 补查"
            self.diagnostics.append(message)
            self._append_text(f"\n[{message}]\n")
        data = base64.b64decode(page["data_base64"], validate=True)
        start = page["next_offset"] - len(data)
        self._decode(data, start, final=page.get("terminal", False) and page["eof"])
        self.next_offset = page["next_offset"]
