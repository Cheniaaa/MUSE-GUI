"""持久曲线、分来源日志、来源 diff 与完整 artifact 下载。"""

import difflib
import json
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PushButton

from .metric_series import AXIS_LABELS, MAX_LOG_CHARACTERS, MetricSelection
from .results_controller import ResultsController


class ResultsView(QWidget):
    def __init__(self, connection, parent=None, *, source_url: str):
        super().__init__(parent)
        self.controller = ResultsController(connection, self, source_url=source_url)
        self._dirty = False
        self._run_ids = []
        self._selectors = {}
        self.curves = {}
        self._plots = []
        self._artifact_entries = []
        layout = QVBoxLayout(self)
        self.summary = BodyLabel("选择一个 Run 或最多两个 Run 比较。", self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        metric_tab = QWidget(self)
        metric_layout = QVBoxLayout(metric_tab)
        self.selector_host = QWidget(metric_tab)
        self.selector_host.setLayout(QVBoxLayout())
        metric_layout.addWidget(self.selector_host)
        cursor_row = QHBoxLayout()
        self.metric_run = ComboBox(self)
        cursor_row.addWidget(self.metric_run)
        self.metric_cursor = LineEdit(self)
        self.metric_cursor.setPlaceholderText("Server next_offset（字节），0=起点")
        cursor_row.addWidget(self.metric_cursor)
        scan = PushButton("从 cursor 读取一页", self)
        scan.clicked.connect(self._scan_metrics)
        cursor_row.addWidget(scan)
        next_page = PushButton("下一页", self)
        next_page.clicked.connect(lambda: self.controller.next_metric_page(self.metric_run.currentText()) if self.metric_run.currentText() else None)
        cursor_row.addWidget(next_page)
        follow = PushButton("继续读取至最新", self)
        follow.clicked.connect(lambda: self.controller.follow_metrics(self.metric_run.currentText()) if self.metric_run.currentText() else None)
        cursor_row.addWidget(follow)
        metric_layout.addLayout(cursor_row)
        self.metric_status = BodyLabel("单位：未知；未知单位不能叠加。", self)
        self.metric_status.setWordWrap(True)
        metric_layout.addWidget(self.metric_status)
        self.plot_host = QWidget(metric_tab)
        self.plot_layout = QVBoxLayout(self.plot_host)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        metric_layout.addWidget(self.plot_host, 1)
        self.tabs.addTab(metric_tab, "持久指标")
        log_tab = QWidget(self)
        log_layout = QVBoxLayout(log_tab)
        log_controls = QHBoxLayout()
        self.log_run = ComboBox(self)
        self.log_channel = ComboBox(self)
        self.log_channel.addItems(["worker", "viser"])
        self.log_cursor = LineEdit(self)
        self.log_cursor.setPlaceholderText("日志字节 cursor，默认 0")
        for widget in (self.log_run, self.log_channel, self.log_cursor):
            log_controls.addWidget(widget)
        log_scan = PushButton("重读历史日志", self)
        log_scan.clicked.connect(self._scan_logs)
        log_controls.addWidget(log_scan)
        next_log = PushButton("下一页", self)
        next_log.clicked.connect(lambda: self.controller.next_log_page(self.log_run.currentText(), self.log_channel.currentText()) if self.log_run.currentText() else None)
        log_controls.addWidget(next_log)
        follow_log = PushButton("继续至最新", self)
        follow_log.clicked.connect(lambda: self.controller.follow_logs(self.log_run.currentText(), self.log_channel.currentText()) if self.log_run.currentText() else None)
        log_controls.addWidget(follow_log)
        log_layout.addLayout(log_controls)
        self.log_status = BodyLabel(f"最多显示最近 5000 行 / {MAX_LOG_CHARACTERS:,} Unicode 字符，保留尾部；完整 bytes 使用原始 artifact 下载。", self)
        self.log_status.setWordWrap(True)
        log_layout.addWidget(self.log_status)
        self.log_output = QPlainTextEdit(self)
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_output)
        self.log_diagnostics = QPlainTextEdit(self)
        self.log_diagnostics.setReadOnly(True)
        self.log_diagnostics.setMaximumBlockCount(200)
        self.log_diagnostics.setMaximumHeight(90)
        log_layout.addWidget(self.log_diagnostics)
        self.log_run.currentIndexChanged.connect(self._mark_dirty)
        self.log_channel.currentIndexChanged.connect(self._mark_dirty)
        self.tabs.addTab(log_tab, "Run 日志")
        self.diff_output = QPlainTextEdit(self)
        self.diff_output.setReadOnly(True)
        self.tabs.addTab(self.diff_output, "source / definition diff")
        artifact_tab = QWidget(self)
        artifact_layout = QVBoxLayout(artifact_tab)
        self.artifact_selector = ComboBox(self)
        artifact_layout.addWidget(self.artifact_selector)
        self.artifact_detail = QPlainTextEdit(self)
        self.artifact_detail.setReadOnly(True)
        artifact_layout.addWidget(self.artifact_detail)
        artifact_row = QHBoxLayout()
        refresh = PushButton("刷新已发布 artifact", self)
        refresh.clicked.connect(self.controller.refresh_artifacts)
        download = PushButton("下载完整原始 artifact", self)
        download.clicked.connect(self._download_selected)
        artifact_row.addWidget(refresh)
        artifact_row.addWidget(download)
        artifact_layout.addLayout(artifact_row)
        self.artifact_selector.currentIndexChanged.connect(self._artifact_selected)
        self.tabs.addTab(artifact_tab, "原始 artifact / checkpoint")
        self.message = BodyLabel(self)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.controller.changed.connect(self._mark_dirty)
        self.controller.error.connect(lambda error: self.message.setText(json.dumps(error, ensure_ascii=False)))
        self.controller.download_finished.connect(self._download_finished)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.refresh_timer.timeout.connect(self._refresh)

    def show_runs(self, run_ids: list[str]) -> None:
        if len(run_ids) > 2 or len(set(run_ids)) != len(run_ids):
            raise ValueError("最多选择两个不同 Run")
        self._run_ids = list(run_ids)
        self._selectors.clear()
        selector_layout = self.selector_host.layout()
        while selector_layout.count():
            item = selector_layout.takeAt(0)
            item.widget().deleteLater()
        self.log_run.clear()
        self.log_run.addItems(run_ids)
        self.metric_run.clear()
        self.metric_run.addItems(run_ids)
        for index, run in enumerate(run_ids):
            row = QWidget(self.selector_host)
            row_layout = QHBoxLayout(row)
            row_layout.addWidget(BodyLabel(run, row))
            path = ComboBox(row)
            axis = ComboBox(row)
            axis.addItems(list(AXIS_LABELS))
            indices = LineEdit(row)
            indices.setPlaceholderText("数组索引，如 0,1")
            statistic = ComboBox(row)
            statistic.addItems(["无统计", "mean", "min", "max"])
            for widget in (path, axis, indices, statistic):
                row_layout.addWidget(widget)
            for combo in (path, axis, statistic):
                combo.currentIndexChanged.connect(self._mark_dirty)
            indices.textChanged.connect(self._mark_dirty)
            self._selectors[run] = (path, axis, indices, statistic)
            if index == 0:
                self.path_selector = path
            selector_layout.addWidget(row)
        self._reset_plots(len(run_ids))
        self.controller.show_runs(run_ids)
        self._mark_dirty()
        self.refresh_timer.start()

    def _mark_dirty(self, *args):
        self._dirty = True

    def _reset_plots(self, count):
        while self.plot_layout.count():
            self.plot_layout.takeAt(0).widget().deleteLater()
        self._plots = []
        self.curves = {}
        for _ in range(count):
            plot = pg.PlotWidget(self.plot_host)
            plot.addLegend()
            plot.getPlotItem().setDownsampling(auto=True, mode="peak")
            self.plot_layout.addWidget(plot)
            self._plots.append(plot)

    def _refresh(self):
        if not self._dirty:
            return
        self._dirty = False
        summaries = []
        for run in self._run_ids:
            record = self.controller.runs.get(run, {})
            summaries.append(f"{run}: mode={record.get('mode', '读取中')} state={record.get('state', '未知')} "
                             f"max_decision_steps={record.get('max_decision_steps', '未知')} checkpoint={record.get('checkpoint')}")
        self.summary.setText("\n".join(summaries))
        self._refresh_metrics()
        self._refresh_logs()
        self._refresh_diff()
        entries = [artifact for run in self._run_ids for artifact in self.controller.artifacts.get(run, [])]
        if entries != self._artifact_entries:
            self._artifact_entries = entries
            with QSignalBlocker(self.artifact_selector):
                self.artifact_selector.clear()
                self.artifact_selector.addItems([f"{a['run']} / {a['kind']} / {a['path']}" for a in entries])
            self._artifact_selected()

    def _refresh_metrics(self):
        selections, points, statuses = {}, {}, []
        for run, series in self.controller.series.items():
            path_widget, axis_widget, indices_widget, statistic_widget = self._selectors[run]
            paths = series.paths
            labels = [".".join(map(str, path)) for path in paths]
            current = path_widget.currentText()
            if [path_widget.itemText(i) for i in range(path_widget.count())] != labels:
                with QSignalBlocker(path_widget):
                    path_widget.clear()
                    path_widget.addItems(labels)
                    if current in labels:
                        path_widget.setCurrentText(current)
            if not paths:
                statuses.append(f"{run}：{self.controller.metric_pages.get(run, {}).get('availability', '读取中')}，暂无持久指标")
                continue
            path = paths[path_widget.currentIndex()]
            unit = {("payload", "steps"): "decision", ("payload", "updates"): "update",
                    ("sim_time",): "s", ("wall_time",): "s"}.get(path)
            indices_text = indices_widget.text().strip()
            try:
                indices = tuple(int(part.strip()) for part in indices_text.split(",")) if indices_text else None
                statistic = statistic_widget.currentText() if statistic_widget.currentIndex() else None
                selection = MetricSelection(path, axis_widget.currentText(), indices, statistic, unit)
                points[run] = series.select(path, selection.axis, indices, statistic)
                selections[run] = selection
                statuses.append(f"{run} / {'.'.join(path)} / 单位：{unit or '未知'} / {AXIS_LABELS[selection.axis]} / "
                                f"indices={indices} statistic={statistic}；本次扫描 {series.total_records}，"
                                f"可见记录 {len(series.records)}，cursor={series.next_offset}；缺值为缺口，不填 0。"
                                + (" 超出 10000 记录显示窗；完整数据仍在 Server。" if series.total_records > 10000 else ""))
            except (ValueError, TypeError) as error:
                statuses.append(f"{run}：{error}")
        overlay = len(selections) == 2 and selections[self._run_ids[0]].can_overlay(selections[self._run_ids[1]])
        plot_count = 1 if overlay else len(self._run_ids)
        if len(self._plots) != plot_count:
            self._reset_plots(plot_count)
        for plot in self._plots:
            plot.clear()
        self.curves = {}
        for index, run in enumerate(self._run_ids):
            if run not in selections:
                continue
            selection = selections[run]
            plot = self._plots[0 if overlay else index]
            plot.setLabel("bottom", AXIS_LABELS[selection.axis])
            plot.setLabel("left", ".".join(selection.path) + " / " + (selection.unit or "单位未知"))
            label = f"{run} / {'.'.join(selection.path)} / {selection.unit or '未知'} / indices={selection.indices} / {selection.statistic}"
            curve = plot.plot(*points[run], name=label, pen=pg.intColor(index), connect="finite")
            self.curves[run] = curve
        if len(self._run_ids) == 2:
            statuses.append("同名、同单位及同轴语义：叠加。" if overlay else "未知单位或选择语义不匹配：并列，不推断可比性。")
        self.metric_status.setText("\n".join(statuses))

    def _refresh_logs(self):
        key = (self.log_run.currentText(), self.log_channel.currentText())
        log = self.controller.logs.get(key)
        if log is None:
            self.log_output.clear()
            self.log_diagnostics.clear()
            return
        text = log.text.removesuffix("\n")
        if self.log_output.toPlainText() != text:
            self.log_output.setPlainText(text)
        self.log_diagnostics.setPlainText("\n".join(log.diagnostics))
        self.log_status.setText(f"{key[0]} / {key[1]} / {log.availability} / cursor={log.next_offset}；"
                                f"最多 5000 可见行 / {MAX_LOG_CHARACTERS:,} Unicode 字符，保留尾部，诊断保留最近 200 项；完整 bytes 请下载原始 artifact。"
                                + (" 已截断显示窗口，不代表后端丢失。" if log.truncated else ""))

    def _refresh_diff(self):
        sections = []
        for label, values in (("source", self.controller.sources), ("definition", self.controller.definitions)):
            if len(self._run_ids) == 2 and all(run in values for run in self._run_ids):
                left, right = self._run_ids
                sections.append(f"=== {label} ===\n" + "\n".join(difflib.unified_diff(
                    json.dumps(values[left], ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
                    json.dumps(values[right], ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
                    fromfile=f"{left}/{label}", tofile=f"{right}/{label}", lineterm="")))
            else:
                for run in self._run_ids:
                    if run in values:
                        sections.append(f"=== {run}/{label} ===\n" + json.dumps(values[run], ensure_ascii=False, indent=2))
        self.diff_output.setPlainText("\n\n".join(sections))

    def _scan_metrics(self):
        if not self.metric_run.currentText():
            return
        try:
            self.controller.load_metric_history(int(self.metric_cursor.text().strip() or "0"), run_id=self.metric_run.currentText())
        except ValueError as error:
            self.message.setText(str(error))

    def _scan_logs(self):
        if not self.log_run.currentText():
            return
        try:
            self.controller.load_log_history(self.log_run.currentText(), self.log_channel.currentText(),
                                             int(self.log_cursor.text().strip() or "0"))
        except ValueError as error:
            self.message.setText(str(error))

    def _artifact_selected(self, *args):
        index = self.artifact_selector.currentIndex()
        if 0 <= index < len(self._artifact_entries):
            self.artifact_detail.setPlainText(json.dumps(self._artifact_entries[index], ensure_ascii=False, indent=2))
        else:
            self.artifact_detail.setPlainText("暂无已发布 artifact；运行终态后刷新。不从可见缓存导出完整数据。")

    def _download_selected(self):
        index = self.artifact_selector.currentIndex()
        if not 0 <= index < len(self._artifact_entries):
            return
        artifact = self._artifact_entries[index]
        destination, _ = QFileDialog.getSaveFileName(self, "下载完整原始 artifact", Path(artifact["path"]).name)
        if destination:
            self.controller.download_artifact(artifact, Path(destination))
            self.message.setText("下载中；完成后校验完整文件 SHA-256 和大小，写入 .source.json 来源记录。")

    def _download_finished(self, result):
        self.message.setText(("下载并校验成功：" if result["verified"] else "下载校验失败：")
                             + json.dumps(result, ensure_ascii=False))

    def closeEvent(self, event):
        self.refresh_timer.stop()
        self.controller.stop()
        super().closeEvent(event)
