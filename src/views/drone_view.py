from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFrame,
    QSizePolicy,
)
from PySide6.QtCore import Qt

from qfluentwidgets import (
    SubtitleLabel,
    BodyLabel,
    StrongBodyLabel,
    CaptionLabel,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    FluentIcon,
    IconWidget,
    ScrollArea,
)


class Separator(QFrame):
    """自定义分割线"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Sunken)
        self.setStyleSheet(
            "background-color: rgba(255, 255, 255, 0.1); border: none; max-height: 1px;"
        )


class DroneSettingCard(QFrame):
    """配置卡片"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.widgets_layout = QVBoxLayout(self)
        self.widgets_layout.setSpacing(20)
        self.widgets_layout.setContentsMargins(20, 20, 20, 20)

        self._init_widgets()
        self._init_layout()
        self._connect_signal_slot()

    def _init_widgets(self):
        """初始化组件"""

        # --- 标题区域 ---
        self.head_icon = IconWidget(FluentIcon.SETTING, parent=self)
        self.head_icon.setFixedSize(24, 24)
        self.head_title = SubtitleLabel(text="无人机配置", parent=self)
        self.head_title.setStyleSheet("font-size: 24px; font-weight: bold;")

        # 第一部分：机架参数
        self.section_title1 = self.get_section_header("机架基础参数")

        # 1. 机架类型
        self.combo_type = ComboBox()
        self.combo_type.addItems(["四旋翼 (Quad)", "六旋翼 (Hex)"])

        # 2. 质量
        self.edit_mass = LineEdit()
        self.edit_mass.setText("1.5")
        self.edit_mass.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit_mass.setPlaceholderText("kg")

        # 3. 轴距
        self.edit_wheel = LineEdit()
        self.edit_wheel.setText("450")
        self.edit_wheel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit_wheel.setPlaceholderText("mm")

        # 4. 导入按钮
        self.btn_import = PushButton("导入配置")
        self.btn_import.setIcon(FluentIcon.LIBRARY_FILL)

        # =======================
        # 第二部分：动力系统
        # =======================
        self.section_title2 = self.get_section_header("动力系统组件")

        # 表头
        self.lbl_h1 = CaptionLabel("组件名称", self)
        self.lbl_h1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_h1.setStyleSheet("font-size: 15px; font-weight: bold;")

        self.lbl_h2 = CaptionLabel("品牌 / 厂商", self)
        self.lbl_h2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_h2.setStyleSheet("font-size: 15px; font-weight: bold;")

        self.lbl_h3 = CaptionLabel("型号 / 规格", self)
        self.lbl_h3.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_h3.setStyleSheet("font-size: 15px; font-weight: bold;")

        # 动力系统组件行数据
        component_rows_data = [
            (FluentIcon.SPEED_HIGH, "无刷电机", "DJI (大疆)", "2312 KV960"),
            (FluentIcon.SEND, "螺旋桨", "APC", "10x4.5MR"),
            (FluentIcon.DEVELOPER_TOOLS, "电子调速器", "Hobbywing (好盈)", "XRotor 20A"),
            (FluentIcon.BASKETBALL, "动力电池", "ACE (格氏电池)", "3S-11.1V-5500mAh"),
        ]

        # 存储组件行的 widget 引用
        self._component_name_widgets: list[QWidget] = []
        self._component_brand_combos: list[ComboBox] = []
        self._component_model_combos: list[ComboBox] = []

        # 创建动力系统组件
        for icon, name, brand_default, model_default in component_rows_data:
            # 左侧：图标 + 名称
            name_container = QWidget()
            h_layout = QHBoxLayout(name_container)
            h_layout.setContentsMargins(0, 0, 0, 0)
            h_layout.setSpacing(10)
            h_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            iw = IconWidget(icon)
            iw.setFixedSize(18, 18)

            lbl = BodyLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            h_layout.addWidget(iw)
            h_layout.addWidget(lbl)

            # 中间：品牌
            cb_brand = ComboBox()
            cb_brand.addItem(brand_default)
            cb_brand.addItems(["T-Motor", "SunnySky", "Generic"])
            cb_brand.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

            # 右侧：型号
            cb_model = ComboBox()
            cb_model.addItem(model_default)
            cb_model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

            self._component_name_widgets.append(name_container)
            self._component_brand_combos.append(cb_brand)
            self._component_model_combos.append(cb_model)

        # 底部按钮区域
        self.btn_reset = PushButton("重置默认")
        self.btn_reset.setFixedWidth(140)

        self.btn_save = PrimaryPushButton("保存配置")
        self.btn_save.setFixedWidth(140)

    def _init_layout(self):
        """初始化布局"""

        # --- 标题区域 ---
        title_layout = QHBoxLayout()
        title_layout.addWidget(self.head_icon)
        title_layout.addWidget(self.head_title)
        self.widgets_layout.addLayout(title_layout)

        # 第一部分：机架参数
        self.widgets_layout.addWidget(self.section_title1)
        self.grid_frame = QGridLayout()
        self.grid_frame.setVerticalSpacing(10)
        self.grid_frame.setHorizontalSpacing(30)  # 增加水平间距，区分不同参数

        for i in range(4):
            self.grid_frame.setColumnStretch(i, 1)
        self.add_form_item(0, 0, "机架类型", self.combo_type)
        self.add_form_item(0, 1, "整机质量 (kg)", self.edit_mass)
        self.add_form_item(0, 2, "轴距 (mm)", self.edit_wheel)
        self.add_form_item(0, 3, "导入配置文件", self.btn_import)

        self.widgets_layout.addLayout(self.grid_frame)
        self.widgets_layout.addSpacing(15)

        # 分割线
        self.widgets_layout.addWidget(Separator())
        self.widgets_layout.addSpacing(15)

        # 第二部分：动力系统
        self.widgets_layout.addWidget(self.section_title2)
        self.grid_comp = QGridLayout()
        self.grid_comp.setVerticalSpacing(25)
        self.grid_comp.setHorizontalSpacing(20)
        self.grid_comp.setColumnStretch(0, 2)
        self.grid_comp.setColumnStretch(1, 3)
        self.grid_comp.setColumnStretch(2, 3)
        self.widgets_layout.addLayout(self.grid_comp)

        # 添加表头
        self.grid_comp.addWidget(self.lbl_h1, 0, 0)
        self.grid_comp.addWidget(self.lbl_h2, 0, 1)
        self.grid_comp.addWidget(self.lbl_h3, 0, 2)

        # 添加动力系统组件行
        for row_idx, (name_w, brand_cb, model_cb) in enumerate(zip(
                self._component_name_widgets,
                self._component_brand_combos,
                self._component_model_combos
        ), start=1):
            self.grid_comp.addWidget(name_w, row_idx, 0)
            self.grid_comp.addWidget(brand_cb, row_idx, 1)
            self.grid_comp.addWidget(model_cb, row_idx, 2)

        self.widgets_layout.addStretch(1)

        # 底部按钮区域
        self.button_layout = QHBoxLayout()
        self.button_layout.setContentsMargins(0, 30, 0, 0)
        self.button_layout.addStretch(1)
        self.button_layout.addWidget(self.btn_reset)
        self.button_layout.addWidget(self.btn_save)
        self.widgets_layout.addLayout(self.button_layout)

    def _connect_signal_slot(self):
        pass

    def get_section_header(self, text):
        lbl = StrongBodyLabel(text)
        lbl.setStyleSheet("font-size: 17px; font-weight: bold;")
        return lbl

    def add_form_item(self, row, col, label_text, widget):
        """垂直排列：文字居中优化"""

        container = QWidget()
        l = QVBoxLayout(container)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)  # 标签和输入框的间距

        lbl = BodyLabel(label_text)
        l.addWidget(lbl, 0, Qt.AlignmentFlag.AlignCenter)

        l.addWidget(widget)
        self.grid_frame.addWidget(container, row, col)


class DroneConfigPage(ScrollArea):

    def __init__(self):
        super().__init__()

        self.drone_widget = QWidget()
        self.drone_layout = QVBoxLayout(self.drone_widget)

        self.setObjectName("flysim_model_interface")
        self.setStyleSheet("QScrollArea{border: none; background-color: transparent;}")
        self.drone_widget.setStyleSheet("QWidget{background-color: transparent;}")

        self.drone_setting_card = DroneSettingCard()
        self.drone_setting_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.drone_layout.addWidget(self.drone_setting_card)

        self.setWidget(self.drone_widget)
        self.setWidgetResizable(True)
