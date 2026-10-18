"""检查与有界切换流程；编辑唯一草稿，不解析科学配置。"""

from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from .schema_editor import at, path_text


class ConfigurationController(QObject):
    flush_requested = Signal()
    report_received = Signal(dict)

    CLEAR_ON_COMPOSITION = ("task_config", "algorithm_config", "term_config", "cost_config", "model_config", "model_choices")
    CATALOG_PATHS = {"schema": "/catalog/schema", "training_packages": "/catalog/training-packages",
                     "networks": "/catalog/networks", "entity_models": "/catalog/entity-models",
                     "controllers": "/catalog/controllers", "sensors": "/catalog/sensors"}

    def __init__(self, draft, connection, view, *, confirm=None):
        super().__init__(view)
        self.draft, self.connection, self.view = draft, connection, view
        view.value_reader = lambda path: at(draft.document, path)
        self.confirm = confirm if confirm is not None else view.confirm_change
        self._editing = False
        self._inspection = None
        self._catalog_requests = {}
        self._catalog = {}
        self._catalog_generation = None
        self._catalog_failed = False
        draft.changed.connect(self._draft_changed)
        view.input_committed.connect(self._commit_input)
        view.override_removed.connect(self._remove_override)
        view.pending_changed.connect(lambda path, text: self._edit(draft.stage_input, path, text))
        view.inspect_requested.connect(self.inspect)
        view.composition_requested.connect(self.select_composition)
        view.model_requested.connect(self.select_model)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._state_changed)
        view.set_document(draft.document)
        if connection.state['state'] != 'connected':
            self.restore_cached_catalog(draft.document['catalog_id'])

    def _edit(self, operation, *args, **kwargs):
        self._editing = True
        try:
            operation(*args, **kwargs)
        finally:
            self._editing = False
        self.view.update_choices(self.draft.document)
        self.view.mark_stale()

    def _draft_changed(self, revision):
        if not self._editing:
            self.view.set_document(self.draft.document)
            catalog_id = self.draft.document['catalog_id']
            if (self.connection.state['state'] != 'connected' and catalog_id is not None
                    and (self.view._catalog is None or self.view._catalog['catalog_id'] != catalog_id)):
                self.restore_cached_catalog(catalog_id)

    def _commit_input(self, path, value, pending_path, pending_subtree):
        self._edit(self.draft.commit_input, path, value, pending_path=path_text(pending_path),
                   pending_subtree=pending_subtree)
        if pending_subtree:
            # 同步退役其它 typed editor 的旧控件，避免下一次 flush 复活已取消文本。
            self.view.set_document(self.draft.document)
            QTimer.singleShot(0, self, self.inspect)

    def _remove_override(self, path):
        self._edit(self.draft.remove_override, path, pending_prefix=path_text(path))
        self.view.set_document(self.draft.document)
        QTimer.singleShot(0, self, self.inspect)

    def set_catalog(self, *, schema, training_packages, networks, entity_models, controllers, sensors):
        """传入已分页聚合的真实目录；也供本地表单独立装配，不生成资源身份。"""
        self.view.set_catalog(schema=schema, training_packages=training_packages, networks=networks,
                              entity_models=entity_models, controllers=controllers, sensors=sensors,
                              catalog_id=self.connection.state["catalog_id"])
        self.view.catalog_status.setText('在线目录 · Catalog ' + str(self.connection.state['catalog_id']))

    def restore_cached_catalog(self, catalog_id):
        """只发布同Catalog的完整显示目录；不恢复inspection或validated_revision。"""
        if catalog_id is None:
            return
        catalog, records = {}, []
        self._catalog_requests.clear()
        self.view.clear_catalog()
        try:
            for key, path in self.CATALOG_PATHS.items():
                offset = 0
                while True:
                    params = None if key == 'schema' else {'offset': offset, 'limit': 50}
                    record = self.connection.cached(path, params=params, catalog_id=catalog_id)
                    if record is None:
                        self.view.catalog_status.setText(f'离线 · Catalog {catalog_id} 缓存不完整：{path} / {offset}；请连接后刷新。')
                        return
                    records.append(record)
                    if key == 'schema':
                        catalog[key] = record['data']
                        break
                    catalog.setdefault(key, []).extend(record['data']['items'])
                    offset = record['data']['next_offset']
                    if offset is None:
                        break
            self.view.set_catalog(**catalog, catalog_id=catalog_id)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            self.view.clear_catalog()
            self.view.catalog_status.setText(f'离线 · Catalog {catalog_id} 缓存读取失败：{error}')
            return
        self.view.catalog_status.setText(f'离线缓存 · Catalog {catalog_id} / API 2 · 来源 '
            + ', '.join(sorted({record['source'] for record in records}))
            + ' · 保存时间 ' + min(record['saved_at'] for record in records)
            + ' ～ ' + max(record['saved_at'] for record in records) + '；仅供显示/编辑，检查与运行需连接后端。')

    def _state_changed(self, state):
        if state["state"] == "connected" and self._catalog_generation != state["generation"]:
            self.refresh_catalog()
        elif state["state"] != "connected":
            self._inspection = None
            self.view.mark_stale()
            self.restore_cached_catalog(self.draft.document['catalog_id'] or state['catalog_id'])

    def refresh_catalog(self):
        if self.connection.state['state'] != 'connected':
            self.restore_cached_catalog(self.draft.document['catalog_id'] or self.connection.state['catalog_id'])
            return
        self._catalog_generation = self.connection.state["generation"]
        self._catalog = {}
        self._catalog_requests = {}
        self._catalog_failed = False
        for key in self.CATALOG_PATHS:
            self._request_catalog(key, 0)

    def _request_catalog(self, key, offset):
        identity = "g3:catalog:" + uuid4().hex
        self._catalog_requests[identity] = (self.connection.state["generation"], key)
        kwargs = {} if key == "schema" else {"params": {"offset": offset, "limit": 50}}
        self.connection.request(identity, "GET", self.CATALOG_PATHS[key], **kwargs)

    def inspect(self) -> None:
        self.flush_requested.emit()
        self.view.flush()
        revision, document = self.draft.capture()
        if document["pending_inputs"]:
            fields = ", ".join(document["pending_inputs"])
            self.view.show_error("请先处理未完成输入：" + fields)
            self.view.focus_error(next(iter(document["pending_inputs"])))
            return
        state = self.connection.state
        if document["catalog_id"] is not None and document["catalog_id"] != state["catalog_id"]:
            self.view.show_error("catalog_id 不同：请显式重新选择资源，不能按名称重绑")
            return
        identity = "g3:inspect:" + uuid4().hex
        self._inspection = (identity, state["generation"], revision)
        self.view.show_error("正在检查本次捕获输入…")
        self.connection.request(identity, "POST", "/experiments/inspect", json=document["request"])

    def _received(self, generation, identity, result):
        if generation != self.connection.state["generation"]:
            return
        if identity in self._catalog_requests:
            expected, key = self._catalog_requests.pop(identity)
            if generation != expected:
                return
            if not result["ok"]:
                self._catalog_failed = True
                self.view.show_error("目录读取失败：" + str(result["error"]))
                return
            data = result["data"]
            if key == "schema":
                self._catalog[key] = data
            else:
                self._catalog.setdefault(key, []).extend(data["items"])
                if data["next_offset"] is not None:
                    self._request_catalog(key, data["next_offset"])
                    return
            if not self._catalog_requests and not self._catalog_failed and set(self._catalog) == set(self.CATALOG_PATHS):
                self.set_catalog(**self._catalog)
            return
        if self._inspection is None or self._inspection[0] != identity:
            return
        _, expected, revision = self._inspection
        self._inspection = None
        if generation != expected or revision != self.draft.revision:
            self.view.mark_stale()
            return
        if not result["ok"]:
            self.view.show_error("检查失败：" + str(result["error"]))
            return
        report = result["data"]
        self.draft.mark_validated(revision)
        self.view.show_report(report)
        self.report_received.emit(deepcopy(report))

    def _switch(self, request, cleared_prefixes):
        self._editing = True
        self.draft.undo_stack.beginMacro("切换研究配置")
        try:
            self.draft.set_value(("request",), request)
            for key in self.draft.document["pending_inputs"]:
                if any(key == prefix or key.startswith(prefix + ".") for prefix in cleared_prefixes):
                    self.draft.set_pending(key, None)
        finally:
            self.draft.undo_stack.endMacro()
            self._editing = False
        self._inspection = None
        self.view.report = None
        self.view.set_document(self.draft.document)

    def select_composition(self, composition_id: str) -> None:
        request = self.draft.document["request"]
        if composition_id == request.get("composition_id"):
            return
        descriptor = self.view.descriptor
        if descriptor is None or composition_id not in descriptor["compositions"]:
            raise ValueError("组合不属于当前已登记研究包")
        if not self.confirm("切换实验组合？", "将删除 task/algorithm/term/cost/model 显式覆盖与模型选择；场景、绑定、seed、RunOptions 保留，绑定需要重新检查。"):
            self.view.set_document(self.draft.document)
            return
        self.view.flush()
        request = self.draft.document["request"]
        prefixes = self._clear_components(request)
        request["composition_id"] = composition_id
        self._switch(request, prefixes)

    def _clear_components(self, request):
        for key in self.CLEAR_ON_COMPOSITION:
            request.pop(key, None)
        return [path_text(("request", key)) for key in self.CLEAR_ON_COMPOSITION]

    def select_package(self, record: dict, *, confirmed: bool = False) -> bool:
        """选择真实package；跨Catalog确认由app合并，组件覆盖清理仍归本域。"""
        request = self.draft.document["request"]
        selected = (request.get("training_package_id"), request.get("training_package_revision"))
        if selected == (record["resource_id"], record["revision"]):
            return True
        if not confirmed and not self.confirm("切换研究包？", "将删除研究组件覆盖及其未完成文本，并选择新包声明的默认组合；场景、绑定、seed、RunOptions 保留，绑定需重新检查。"):
            return False
        self.view.flush()
        request = self.draft.document["request"]
        prefixes = self._clear_components(request)
        request["training_package_id"] = record["resource_id"]
        request["training_package_revision"] = record["revision"]
        composition = record["metadata"]["default_composition_id"]
        if composition is None:
            request.pop("composition_id", None)
        else:
            request["composition_id"] = composition
        self._switch(request, prefixes)
        if self.view.descriptor is None and self.connection.state["state"] == "connected":
            self.refresh_catalog()
        return True

    def select_model(self, kind: str, reference: str) -> None:
        binding = self.view.composition["model_bindings"][kind]
        if reference not in binding["options"]:
            raise ValueError("模型不是当前组合的合法候选")
        request = self.draft.document["request"]
        selected = request.get("model_choices", {}).get(kind, binding["default"])
        if selected == reference:
            return
        if not self.confirm("切换 " + kind + " 模型？", "仅删除该 kind 前一模型的参数和未完成文本，其他 kind 与场景保持不变；能力与绑定需重新检查。"):
            self.view.set_document(self.draft.document)
            return
        self.view.flush()
        request = self.draft.document["request"]
        request.get("model_config", {}).pop(kind, None)
        request.setdefault("model_choices", {})[kind] = reference
        self._switch(request, [path_text(("request", "model_config", kind))])
