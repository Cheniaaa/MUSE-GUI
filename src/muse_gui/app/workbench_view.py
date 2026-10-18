"""稳定工作台容器；真实功能视图由 app 装入对应 host。"""

from typing import Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QScrollArea, QSplitter, QStackedWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, CardWidget, DropDownPushButton, FluentIcon as FIF,
    IconWidget, LineEdit, Pivot, PrimaryPushButton, PushButton, RoundMenu,
    StrongBodyLabel, TabBar, TransparentPushButton, TreeWidget,
)


class WorkbenchView(QWidget):
    context_changed = Signal(str, str)
    new_requested = Signal()
    open_requested = Signal()
    save_requested = Signal(bool)

    def __init__(self, draft, parent=None):
        super().__init__(parent)
        self.setObjectName("workbench")
        self.draft = draft
        self.context_kind = "draft"
        self.context_identity = draft.document["draft_id"]
        self._contexts = [("draft", self.context_identity)]
        self._page = "scene"
        self._expanded_height = 180
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(3)
        title.addWidget(CaptionLabel("MUSE  /  RESEARCH WORKBENCH", self))
        self.name_edit = LineEdit(self)
        self.name_edit.setObjectName("draftName")
        self.name_edit.setMinimumWidth(190)
        self.name_edit.setMaximumWidth(360)
        self.name_edit.setText(draft.document["name"])
        self.name_edit.setAccessibleName("实验名称")
        self.name_edit.editingFinished.connect(self.flush_inputs)
        title.addWidget(self.name_edit)
        header.addLayout(title, 1)
        self.connection_label = CaptionLabel("未连接\n本地草稿 · 可离线保存", self)
        self.connection_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.connection_label)
        for text, signal in (("新建", self.new_requested), ("打开", self.open_requested)):
            button = PushButton(text, self)
            button.setFixedWidth(72)
            button.clicked.connect(signal.emit)
            header.addWidget(button)
        self.save_button = PrimaryPushButton(FIF.SAVE, "保存", self)
        self.save_button.setFixedWidth(90)
        self.save_button.clicked.connect(lambda: self.save_requested.emit(False))
        header.addWidget(self.save_button)
        self.save_as_button = PushButton("另存为", self)
        self.save_as_button.setFixedWidth(80)
        self.save_as_button.clicked.connect(lambda: self.save_requested.emit(True))
        header.addWidget(self.save_as_button)
        root.addLayout(header)

        context_row = QHBoxLayout()
        self.context_tabs = TabBar(self)
        self.context_tabs.setMovable(False)
        self.context_tabs.setTabsClosable(False)
        self.context_tabs.setAddButtonVisible(False)
        self.context_tabs.setTabMaximumWidth(260)
        self.context_tabs.addTab("draft", "编辑草稿", FIF.EDIT)
        self.context_tabs.currentChanged.connect(self._tab_changed)
        context_row.addWidget(self.context_tabs, 1)
        self.layout_button = DropDownPushButton(FIF.LAYOUT, "布局", self)
        menu = RoundMenu(parent=self)
        for text, callback in (
            ("显示 / 隐藏实验内容", lambda: self.tree_panel.setVisible(self.tree_panel.isHidden())),
            ("显示 / 隐藏属性", lambda: self.inspector_panel.setVisible(self.inspector_panel.isHidden())),
            ("展开 / 收起输出", lambda: self.set_output_collapsed(not self.output_collapsed)),
            ("恢复默认布局", self.restore_layout),
        ):
            action = Action(text, self)
            action.triggered.connect(callback)
            menu.addAction(action)
        self.layout_button.setMenu(menu)
        context_row.addWidget(self.layout_button)
        root.addLayout(context_row)

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.vertical_splitter.setObjectName("workbenchVertical")
        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.horizontal_splitter.setObjectName("workbenchHorizontal")
        self.horizontal_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.setChildrenCollapsible(False)
        for splitter in (self.horizontal_splitter, self.vertical_splitter):
            splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        self.vertical_splitter.addWidget(self.horizontal_splitter)

        self.tree_panel = CardWidget(self)
        self.tree_panel.setClickEnabled(False)
        self.tree_panel.setMinimumWidth(165)
        tree_layout = QVBoxLayout(self.tree_panel)
        tree_layout.setContentsMargins(8, 12, 6, 8)
        self.tree_heading = StrongBodyLabel("实验内容 · 草稿", self)
        tree_layout.addWidget(self.tree_heading)
        self.tree = TreeWidget(self.tree_panel)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(12)
        scene = QTreeWidgetItem(self.tree, ["场景"])
        for text in ("环境", "实体与布局"):
            item = QTreeWidgetItem(scene, [text])
            item.setData(0, Qt.ItemDataRole.UserRole, "scene")
        config = QTreeWidgetItem(self.tree, ["研究配置"])
        for text in ("组合与研究组件", "角色 / 观测 / 奖励", "cost 与终止条件", "无线 / 感知模型", "运行参数"):
            item = QTreeWidgetItem(config, [text])
            item.setData(0, Qt.ItemDataRole.UserRole, "config")
        self.tree.expandAll()
        self.tree.itemClicked.connect(self._tree_clicked)
        tree_layout.addWidget(self.tree, 1)
        self.tree_note = CaptionLabel("一份活动草稿\n改动仅用于下一次运行", self)
        tree_layout.addWidget(self.tree_note)
        self.horizontal_splitter.addWidget(self.tree_panel)

        self.central_panel = CardWidget(self)
        self.central_panel.setClickEnabled(False)
        self.central_panel.setMinimumWidth(330)
        center = QVBoxLayout(self.central_panel)
        center.setContentsMargins(8, 8, 8, 8)
        center.setSpacing(10)
        self.page_tabs = Pivot(self)
        self.page_tabs.addItem("scene", "场景布局", lambda: self.set_page("scene"))
        self.page_tabs.addItem("config", "研究配置", lambda: self.set_page("config"))
        self.page_tabs.setItemFontSize(13)
        center.addWidget(self.page_tabs)
        self.central_stack = QStackedWidget(self)
        self.draft_scene_host = self._host("sceneEditorHost", "暂无场景预览", "草稿尚未加载场景视图", FIF.GLOBE)
        self.draft_config_host = self._host("researchConfigHost", "研究配置", "尚未加载研究包与配置 schema", FIF.EDUCATION)
        self.config_scroll = self._scroll(self.draft_config_host)
        self.run_scene_host = self._host("runSceneHost", "暂无实时数据", "运行显示由 Server 提供；此处不生成模拟快照", FIF.VIEW)
        self.run_config_host = self._host("runConfigHost", "运行配置 · 只读", "尚未加载此 Run 的冻结输入", FIF.DOCUMENT)
        self.run_config_scroll = self._scroll(self.run_config_host)
        for widget in (self.draft_scene_host, self.config_scroll, self.run_scene_host, self.run_config_scroll):
            self.central_stack.addWidget(widget)
        center.addWidget(self.central_stack, 1)
        self.horizontal_splitter.addWidget(self.central_panel)

        self.inspector_panel = CardWidget(self)
        self.inspector_panel.setClickEnabled(False)
        self.inspector_panel.setMinimumWidth(210)
        inspector = QVBoxLayout(self.inspector_panel)
        inspector.setContentsMargins(12, 12, 10, 8)
        self.inspector_heading = StrongBodyLabel("属性与上下文 · 草稿", self)
        inspector.addWidget(self.inspector_heading)
        self.inspector_host = self._host("contextInspectorHost", "尚未选择对象", "选择对象或研究组件后，在这里查看属性、来源与约束。", FIF.INFO)
        inspector.addWidget(self._scroll(self.inspector_host), 1)
        self.horizontal_splitter.addWidget(self.inspector_panel)
        self.horizontal_splitter.setStretchFactor(1, 1)

        self.output_panel = CardWidget(self)
        self.output_panel.setClickEnabled(False)
        self.output_panel.setObjectName("contextOutput")
        output = QVBoxLayout(self.output_panel)
        output.setContentsMargins(10, 0, 10, 4)
        output.setSpacing(0)
        output_header = QHBoxLayout()
        self.output_heading = StrongBodyLabel("校验 / 消息", self)
        output_header.addWidget(self.output_heading)
        self.output_context = CaptionLabel("当前草稿", self)
        output_header.addStretch()
        output_header.addWidget(self.output_context)
        self.output_toggle = TransparentPushButton("展开", self)
        self.output_toggle.setFixedHeight(32)
        self.output_toggle.clicked.connect(lambda: self.set_output_collapsed(not self.output_collapsed))
        output_header.addWidget(self.output_toggle)
        output.addLayout(output_header)
        self.output_content = QStackedWidget(self)
        self.messages_host = QWidget(self)
        self.messages_host.setObjectName("draftMessagesHost")
        messages = QVBoxLayout(self.messages_host)
        self.message_label = BodyLabel("草稿可不完整保存。尚未执行后端检查。", self)
        self.message_label.setWordWrap(True)
        messages.addWidget(self.message_label)
        messages.addStretch()
        self.output_content.addWidget(self.messages_host)
        self.run_output_host = QWidget(self)
        self.run_output_host.setObjectName("runResultsHost")
        run_output = QVBoxLayout(self.run_output_host)
        self.metrics_host = self._host("runMetricsHost", "暂无运行指标", "仅展示后端实际发布的数据", FIF.PIE_SINGLE)
        self.logs_host = QWidget(self)
        self.logs_host.setObjectName("runLogsHost")
        self.logs_host.setLayout(QVBoxLayout())
        run_output.addWidget(self.metrics_host)
        run_output.addWidget(self.logs_host)
        self.output_content.addWidget(self.run_output_host)
        output.addWidget(self.output_content, 1)
        self.vertical_splitter.addWidget(self.output_panel)
        root.addWidget(self.vertical_splitter, 1)

        footer = QHBoxLayout()
        self.context_label = CaptionLabel("编辑草稿 · 改动用于下一次运行", self)
        footer.addWidget(self.context_label, 1)
        self.revision_label = CaptionLabel(self)
        footer.addWidget(self.revision_label)
        root.addLayout(footer)
        draft.changed.connect(self._draft_changed)
        draft.state_changed.connect(self._update_state)
        self.restore_layout()
        self.set_context("draft", self.context_identity)

    @staticmethod
    def _scroll(widget):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(widget)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        widget.setAutoFillBackground(False)
        return scroll

    def _host(self, name, heading, note, icon):
        host = QWidget(self)
        host.setObjectName(name)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(18, 24, 18, 24)
        layout.setSpacing(12)
        layout.addStretch()
        image = IconWidget(icon, host)
        image.setFixedSize(36, 36)
        layout.addWidget(image, alignment=Qt.AlignmentFlag.AlignHCenter)
        title = StrongBodyLabel(heading, host)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        detail = CaptionLabel(note, host)
        detail.setWordWrap(True)
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(detail)
        layout.addStretch()
        return host

    @property
    def read_only(self):
        return self.context_kind == "run"

    def flush_inputs(self):
        if not self.read_only:
            self.draft.set_value(("name",), self.name_edit.text())
            self.name_edit.setModified(False)

    def _draft_changed(self, revision):
        if not self.read_only:
            document = self.draft.document
            replaced = self.context_identity != document["draft_id"]
            self.context_identity = document["draft_id"]
            self._contexts[0] = ("draft", self.context_identity)
            if replaced or not self.name_edit.isModified():
                self.name_edit.setText(document["name"])

    def _update_state(self):
        self.context_tabs.setTabText(0, "编辑草稿" + (" · 未保存" if self.draft.dirty else ""))
        self.save_button.setEnabled(not self.read_only and not self.draft.saving)
        self.save_as_button.setEnabled(not self.read_only and not self.draft.saving)
        self.revision_label.setText(f"revision {self.draft.revision}  ·  " + ("保存中…" if self.draft.saving else "本地草稿"))
        self.revision_label.setVisible(not self.read_only)

    def _tab_changed(self, index):
        if 0 <= index < len(self._contexts):
            self.set_context(*self._contexts[index])

    def set_context(self, kind: Literal["draft", "run"], identity: str) -> None:
        self.flush_inputs()
        if kind == "draft":
            identity = self.draft.document["draft_id"]
            self._contexts[0] = (kind, identity)
            index = 0
        else:
            if (kind, identity) not in self._contexts:
                self._contexts.append((kind, identity))
                self.context_tabs.addTab("run:" + identity, "Run · " + identity, FIF.VIEW)
            index = self._contexts.index((kind, identity))
        changed = (kind, identity) != (self.context_kind, self.context_identity)
        self.context_kind, self.context_identity = kind, identity
        self.name_edit.setReadOnly(self.read_only)
        self.name_edit.setText("Run · " + identity if self.read_only else self.draft.document["name"])
        self.tree_heading.setText("实验内容 · " + ("Run 只读" if self.read_only else "草稿"))
        self.tree_note.setText("冻结输入 · 只读观察" if self.read_only else "一份活动草稿\n改动仅用于下一次运行")
        self.inspector_heading.setText("属性与上下文 · " + ("Run 只读" if self.read_only else "草稿"))
        self.context_label.setText("Run · " + identity + " · 只读；切换不改变运行生命周期" if self.read_only else "编辑草稿 · 改动用于下一次运行")
        self.output_context.setText("Run · " + identity if self.read_only else "当前草稿")
        self.output_heading.setText("运行结果 / 日志 · 只读" if self.read_only else "校验 / 消息")
        self.output_content.setCurrentWidget(self.run_output_host if self.read_only else self.messages_host)
        self.metrics_host.setVisible(self.read_only)
        self.logs_host.setVisible(self.read_only)
        self.context_tabs.setCurrentIndex(index)
        self.set_page(self._page)
        self._update_state()
        if changed:
            self.context_changed.emit(kind, identity)

    def set_page(self, page: Literal["scene", "config"]) -> None:
        self._page = page
        self.page_tabs.setCurrentItem(page)
        self.central_stack.setCurrentIndex((2 if self.read_only else 0) + (page == "config"))

    def _tree_clicked(self, item, column):
        page = item.data(0, Qt.ItemDataRole.UserRole)
        if page:
            self.set_page(page)

    def set_output_collapsed(self, collapsed: bool) -> None:
        if collapsed and hasattr(self, "output_collapsed") and not self.output_collapsed:
            self._expanded_height = max(100, self.vertical_splitter.sizes()[1])
        self.output_collapsed = collapsed
        self.output_content.setVisible(not collapsed)
        self.output_panel.setMaximumHeight(38 if collapsed else 16777215)
        self.output_toggle.setText("展开" if collapsed else "收起")
        height = self.vertical_splitter.height()
        bottom = 38 if collapsed else self._expanded_height
        self.vertical_splitter.setSizes([max(100, height - bottom), bottom])

    def restore_layout(self) -> None:
        self.tree_panel.show()
        self.inspector_panel.show()
        self.horizontal_splitter.setSizes([205, 700, 255])
        self._expanded_height = 180
        self.set_output_collapsed(True)

    def show_message(self, text: str) -> None:
        self.message_label.setText(text)
