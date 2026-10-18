"""工作台功能组装与导航；研究解析、提交和运行各归其功能域。"""
from copy import deepcopy
import json
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout
from qfluentwidgets import MessageBox, PushButton, PrimaryPushButton

from muse_gui.backend.local_backend import LocalBackendLauncher
from muse_gui.catalog.resources_view import ResourcesView
from muse_gui.experiments.submission_controller import SubmissionController
from muse_gui.runs.history_view import HistoryView
from muse_gui.runs.results_view import ResultsView
from .settings_view import SettingsView


class WorkbenchController(QObject):
    run_requested = Signal(str, bool)

    def __init__(self, window, draft, connection, configuration):
        super().__init__(window)
        self.window, self.draft, self.connection = window, draft, connection
        self.configuration = configuration
        self._view_context = None
        self._connected_generation = None
        self._new_run_ids = set()
        self.settings = SettingsView(window.preferences, window.settings_host)
        self.launcher = LocalBackendLauncher(connection, self)
        self.resources = ResourcesView(connection, window.preferences.settings, window.resources_host)
        self.history = HistoryView(connection, window.history_host)
        for host, widget in ((window.settings_host, self.settings), (window.resources_host, self.resources),
                             (window.history_host, self.history)):
            # 保留导航host，替换骨架说明；外观主题控件仍在Settings页。
            layout = host.layout()
            layout.insertWidget(layout.count() - 1, window.workbench._scroll(widget), 1)
        self.results = None
        self._retired_results = []
        self._closing = False
        self.submission = SubmissionController(draft, connection, self.resources, self, flush=self.flush)
        self.settings.connect_requested.connect(self.connect_profile)
        self.settings.start_requested.connect(self.connect_profile)
        self.settings.status_requested.connect(lambda profile: self._local('status', profile))
        self.settings.stop_requested.connect(self.stop_backend)
        connection.state_changed.connect(self._connection_changed)
        self.launcher.state_changed.connect(self.settings.set_backend_state)
        self.resources.example_ready.connect(self.accept_example)
        self.resources.resource_selected.connect(self.select_resource)
        self.resources.resource_registered.connect(lambda _: configuration.refresh_catalog())
        self.history.open_requested.connect(lambda identity: self.run_requested.emit(identity, False))
        self.history.compare_requested.connect(self.show_results)
        self.history.copy_requested.connect(self.copy_source)
        self.submission.completed.connect(self._submitted)
        self.submission.failed.connect(self._submission_failed)
        self.submission.stage_changed.connect(self._stage_changed)
        self.submission.runs_found.connect(self._show_found_runs)
        row = QHBoxLayout()
        self.start_button = PrimaryPushButton('开始 · 捕获当前草稿', window.workbench)
        self.start_button.clicked.connect(lambda: self.start(None))
        self.cancel_submission = PushButton('取消后续提交', window.workbench)
        self.cancel_submission.clicked.connect(self.submission.cancel)
        self.cancel_submission.setEnabled(False)
        self.find_run = PushButton('查找未知提交结果', window.workbench)
        self.find_run.clicked.connect(self.submission.find_submitted_runs)
        self.find_run.setEnabled(False)
        for widget in (self.start_button, self.cancel_submission, self.find_run):
            row.addWidget(widget)
        row.addStretch()
        window.workbench.layout().insertLayout(1, row)
        window.prepare_resources_close = self.prepare_close

    def mount_views(self):
        """正式入口装入场景与Run消费者；离线时不创建WebEngine页面或后端会话。"""
        from muse_gui.scene.scene_controller import SceneController
        from muse_gui.scene.scene_editor_view import SceneEditorView
        from muse_gui.runs.run_controller import RunController
        from muse_gui.runs.run_view import RunView
        from muse_gui.widgets.viewer_host import ViewerHost
        workbench = self.window.workbench
        self.draft_viewer = ViewerHost(workbench.draft_scene_host)
        self.run_viewer = ViewerHost(workbench.run_scene_host)
        self.scene_view = SceneEditorView(workbench.inspector_host)
        self.scene = SceneController(self.draft, self.connection, self.scene_view, self.draft_viewer, self)
        workbench.flush_requested.connect(self.scene_view.flush_inputs)
        self.run = RunController(self.connection, self.run_viewer, self)
        self.run_view = RunView(self.run, workbench.run_config_host)
        for host, widget in ((workbench.draft_scene_host, self.draft_viewer),
                             (workbench.run_scene_host, self.run_viewer),
                             (workbench.run_config_host, self.run_view)):
            layout = host.layout()
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(widget)
        self.scene_view.layout().removeWidget(self.scene_view.tree)
        self.scene_view.tree.setParent(workbench.tree_panel)
        workbench.tree_panel.layout().insertWidget(2, self.scene_view.tree, 1)
        workbench.inspector_host.layout().addWidget(self.scene_view)
        self.run_view.layout().removeWidget(self.run_view.object_details)
        self.run_view.object_details.setParent(workbench.inspector_host)
        self.run_view.object_details.setMaximumHeight(16777215)
        workbench.inspector_host.layout().addWidget(self.run_view.object_details)
        self.scene.error.connect(lambda error: workbench.show_message(str(error)))
        self.run_view.results_requested.connect(lambda identity: self.show_results([identity]))
        self.run_view.details_requested.connect(lambda _: workbench.set_page('config'))
        self.run_view.continue_requested.connect(lambda context: self.copy_run(context, 'TRAIN'))
        self.run_view.evaluate_requested.connect(lambda context: self.copy_run(context, 'EVALUATE'))
        self.run_requested.connect(self.open_run)
        workbench.context_changed.connect(self._context_changed)
        workbench.page_changed.connect(self._page_changed)
        self.close_tab_button = PushButton('关闭当前 Run 标签', workbench)
        self.close_tab_button.clicked.connect(self.close_current_run)
        workbench.document_actions.addWidget(self.close_tab_button)
        self._context_changed(workbench.context_kind, workbench.context_identity)

    def open_run(self, identity, new_run=False):
        if new_run:
            self._new_run_ids.add(identity)
        self.window.workbench.set_context('run', identity)
        self.window.workbench.set_page('scene')
        self.window.switchTo(self.window.workbench)

    def _context_changed(self, kind, identity):
        if self._view_context == (kind, identity):
            return
        if self._view_context is not None:
            previous_kind, previous_id = self._view_context
            if previous_kind == 'run':
                self.run.close_tab(previous_id)
            else:
                self.scene.close_preview()
        self._view_context = (kind, identity)
        if kind == 'run':
            self.run.open(identity, new_run=identity in self._new_run_ids)
            self._new_run_ids.discard(identity)
        elif self.connection.state['state'] == 'connected':
            self.scene.refresh_preview()
        if self.results is not None:
            if kind == 'draft':
                self.results.controller.stop()
                self.results.refresh_timer.stop()
            elif identity not in self.results.controller.series:
                self.results.show_runs([identity])
            elif not self.results.controller.active:
                self.results.show_runs(list(self.results.controller.series))
        self.start_button.setVisible(kind == 'draft')
        self.cancel_submission.setVisible(kind == 'draft')
        self.find_run.setVisible(kind == 'draft')
        self.close_tab_button.setVisible(kind == 'run')
        self._page_changed(self.window.workbench._page)

    def _page_changed(self, page):
        draft = self.window.workbench.context_kind == 'draft'
        self.scene_view.setVisible(draft and page == 'scene')
        self.scene_view.tree.setVisible(draft and page == 'scene')
        self.run_view.object_details.setVisible(not draft)

    def close_current_run(self):
        workbench = self.window.workbench
        if workbench.context_kind == 'run':
            workbench.close_run_context(workbench.context_identity)

    def copy_run(self, context, mode):
        def apply():
            options = {'mode': mode, 'max_decision_steps': context['record']['max_decision_steps']}
            if context['checkpoint_artifact_id'] is not None:
                options['checkpoint_artifact_id'] = context['checkpoint_artifact_id']
            self._replace_source(context['source'], options, 'Run 派生实验')
            self.window.workbench.set_page('config')
        self.window._confirm_replace(apply)

    def flush(self):
        self.window.workbench.flush_inputs()

    def _local(self, action, profile, *args):
        try:
            getattr(self.launcher, action)(profile, *args)
        except ValueError as exc:
            self.settings.set_backend_state({'state': 'failed', 'error': str(exc)})

    def connect_profile(self, profile):
        if profile['mode'] == 'local':
            self._local('start', profile)
        else:
            try:
                self.connection.connect(profile)
            except ValueError as exc:
                self.settings.set_connection_state({'state': 'failed', 'error': str(exc)})

    def stop_backend(self, profile, instance_id):
        dialog = MessageBox('停止本机后端？', '活动 Run 将被取消，排队项保留。关闭 GUI 本身不会停止后端。', self.window)
        if dialog.exec():
            self._local('stop', profile, instance_id)

    def _connection_changed(self, state):
        self.settings.set_connection_state(state)
        if state['state'] == 'connected' and state['generation'] != self._connected_generation:
            self._connected_generation = state['generation']
            profile = self.settings.profile()
            self.window.preferences.settings.setValue('backend/profile', profile)
            self.resources.refresh()
            self.history.refresh()
            if not self.draft.document['request'] and self.draft.document['catalog_id'] is None:
                self.draft.set_value(('catalog_id',), state['catalog_id'])

    def _replace_source(self, request, run_options, name):
        self.draft.replace_document(dict(format_version=1, draft_id=str(uuid4()), name=name,
            catalog_id=self.connection.state['catalog_id'], request=deepcopy(request),
            run_options=deepcopy(run_options), environment_edit={}, pending_inputs={}))
        self.window.workbench.set_context('draft', self.draft.document['draft_id'])
        self.window.switchTo(self.window.workbench)

    def accept_example(self, example):
        def apply():
            self._replace_source(example['request'], example['run_options'], '参考实验')
            self.window.workbench.show_message('模板研究状态：' + json.dumps(example['research_status'], ensure_ascii=False))
            self.configuration.refresh_catalog()
        self.window._confirm_replace(apply)

    def copy_source(self, source):
        self.window._confirm_replace(lambda: self._replace_source(source, {}, '实验副本'))

    def select_resource(self, record):
        if record['kind'] == 'training_package':
            if not self.configuration.select_package(record):
                return
            self.draft.set_value(('catalog_id',), self.connection.state['catalog_id'])
            self.window.switchTo(self.window.workbench)
            return
        dialog = MessageBox('替换当前资源？', '草稿保留场景输入；研究包切换会清除组件覆盖并重新检查绑定。环境切换会清除尚未派生的环境编辑。', self.window)
        if not dialog.exec():
            return
        self.flush()
        document = self.draft.document
        request = document['request']
        kind = record['kind']
        request[kind + '_id'], request[kind + '_revision'] = record['resource_id'], record['revision']
        self.draft.undo_stack.beginMacro('选择研究资源')
        try:
            self.draft.set_value(('catalog_id',), self.connection.state['catalog_id'])
            self.draft.set_value(('request',), request)
            if kind == 'environment':
                self.draft.set_value(('environment_edit',), {})
        finally:
            self.draft.undo_stack.endMacro()
        self.configuration.refresh_catalog()
        self.window.switchTo(self.window.workbench)

    def start(self, source_choice):
        self.flush()
        self.submission.start(self.draft.document['run_options'], source_choice)

    def _stage_changed(self, stage):
        self.start_button.setEnabled(not self.submission.busy)
        self.cancel_submission.setEnabled(self.submission.busy)
        self.window.workbench.show_message('提交阶段：' + stage)

    def _submission_failed(self, stage, error):
        self.start_button.setEnabled(True)
        self.cancel_submission.setEnabled(False)
        self.find_run.setEnabled(stage == 'submission_unknown')
        self.window.workbench.show_message(stage + '：' + json.dumps(error, ensure_ascii=False))
        self.window.workbench.set_output_collapsed(False)
        if stage == 'source_choice_required':
            dialog = MessageBox('选择研究包版本', error['message'], self.window)
            dialog.yesButton.setText('更新后运行')
            dialog.cancelButton.setText('返回编辑')
            registered = PushButton('使用已登记版本', dialog.buttonGroup)
            dialog.buttonLayout.insertWidget(1, registered)
            choice = []
            registered.clicked.connect(lambda: (choice.append('registered'), dialog.reject()))
            accepted = dialog.exec()
            if accepted or choice:
                self.start('update' if accepted else choice[0])

    def _submitted(self, run, revision):
        self.start_button.setEnabled(True)
        self.cancel_submission.setEnabled(False)
        self.window.workbench.show_message(f"revision {revision} 已提交 Run {run['run_id']}；状态 {run['state']}")
        self.run_requested.emit(run['run_id'], True)

    def _show_found_runs(self, page):
        self.history.set_page(page)
        self.window.switchTo(self.window.history_host)

    def show_results(self, run_ids):
        url = self.settings.profile()['base_url']
        if self.results is not None:
            self.results.close()
            self.results.hide()
            self.window.workbench.metrics_host.layout().removeWidget(self._results_scroll)
            self._results_scroll.takeWidget()
            self._results_scroll.deleteLater()
            self._retired_results.append(self.results)
        else:
            layout = self.window.workbench.metrics_host.layout()
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            layout.setContentsMargins(0, 0, 0, 0)
        self.results = ResultsView(self.connection, self.window.workbench.metrics_host, source_url=url)
        self._results_scroll = self.window.workbench._scroll(self.results)
        self.window.workbench.metrics_host.layout().addWidget(self._results_scroll)
        self.results.show_runs(run_ids)
        if run_ids:
            self.window.workbench.set_context('run', run_ids[0])
        self.window.workbench.set_output_collapsed(False)
        self.window.switchTo(self.window.workbench)
        self._retire_results()

    def _retire_results(self):
        for view in self._retired_results[:]:
            if not view.controller.has_pending_downloads:
                view.deleteLater()
                self._retired_results.remove(view)

    def prepare_close(self):
        if not self._closing:
            self._closing = True
            if self._view_context is not None:
                self.scene.close_preview()
                if self.run.state is not None:
                    self.run.close_tab(self.run.state.run_id)
        self.submission.cancel()
        settled = self.submission.close()
        settled = self.resources.close_operations() and settled
        if self.results:
            self.results.close()
            settled = not self.results.controller.has_pending_downloads and settled
        self._retire_results()
        settled = not self._retired_results and settled
        settled = self.launcher.close() and settled
        if self._view_context is not None:
            settled = not self.draft_viewer.releasing and not self.run_viewer.releasing and settled
        if not settled:
            return False
        return self.connection.close(wait_ms=0)
