# AGENTS.md


## 项目概述

MUSE GUI 是 Muse 的研究工作台：同一研究者编写研究代码、编辑草稿、配置场景、提交 Run 并查看结果。客户端通过 HTTP/WebSocket 连接 Muse Server，使用 Qt WebEngine 内嵌后端 Viser。

新工作台正在按 `docs/superpowers/plans/2026-10-18-muse-research-workbench.md` 的 G1–G8 实施；不能把界面骨架当成完整产品验收。交互和模块边界见 `docs/superpowers/specs/2026-10-18-muse-research-workbench-design.md`。

后端及联合进度的唯一记录在 `\\wsl.localhost\Ubuntu-24.04\project\mjc_ws\.planning\mjlab-spike\gui_execution.md`；共享契约和 B0–B9 计划在该后端工程的 `docs/superpowers/`。子代理只维护分配的报告，不创建另一套主计划。

**技术栈：** Python 3.13、PySide6/Qt 6、qfluentwidgets、PyQtGraph、numpy/scipy、httpx2、websockets。直接依赖见 `requirements.in`，完整版本锁见 `requirements.txt`，不擅自升级。

## 环境与命令

在本工程根目录运行任何 Python 命令前，先激活 `.venv`：

```powershell
& '.\.venv\Scripts\Activate.ps1'
# 准备开发环境时执行；日常运行不重复安装。
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m pytest tests -q
# 新工作台开发入口，功能完成范围以实施记录为准。
python -m muse_gui
```

测试直接导入 `muse_gui`，不手改 `sys.path`。普通控件测试可用 `QT_QPA_PLATFORM=offscreen`；WebEngine、显示缩放与 Windows/WSL 生命周期验收必须使用真实桌面。QtTest 等待实际事件条件，不用任意 sleep 判断完成。

## 架构设计

采用已确认的 `src/muse_gui/`：外层按功能组织，模块内部区分界面、状态和控制流程。下表是文件归属，目录仅随实际消费者创建。

### 组件层次

| 包 | 职责 |
|---|---|
| `app` | 入口组装、主窗口、工作台容器、设置与跨功能导航 |
| `backend` | HTTP/WS、连接代际、目录显示缓存、本机后端启动入口 |
| `experiments` | 唯一草稿、保存/undo、schema 配置、检查与捕获提交 |
| `catalog` | 资源浏览、研究目录快照归档与登记操作 |
| `scene` | 场景编辑、对象选择、CPU 草稿预览控制 |
| `runs` | Run 只读状态/命令、历史、指标/日志与结果 |
| `widgets` | 已有跨功能消费者的公共控件，例如 ViewerHost |
| `assets` | 当前应用实际使用的包内图标等资源 |

### 依赖和状态所有权

- `app` 用构造参数和 Qt 实例信号组装功能；功能模块不反向导入窗口，不扩展旧全局 signal bus。
- `backend` 和 `widgets` 不依赖业务状态/控制器。View 展示数据与用户意图，Controller 协调有界操作，状态模型不承担网络流程。
- 表单、场景和提交共享同一个 `ExperimentDraft`；预览只持有镜像，Server 拥有解析、冻结及运行真值。GUI 不执行研究包或复制 resolver。
- GUI 主线程只操作控件；网络、归档/hash、WSL 等待在后台。返回值携带 generation/revision/run_id，过期结果不覆盖当前对象。
- 关闭 GUI 默认让后端 Run 继续；仅释放本客户端和草稿预览。停止本机后端是独立操作，由 Server 清理 Worker；未结束的 QThread 保持生命周期所有权，不能随窗口直接销毁。

## UI 框架

使用 qfluentwidgets 的 FluentWindow 与 Qt QSplitter，保持统一风格，覆盖浅/深主题、1280×800 逻辑工作区和实际 DPR 1.0/1.5。场景协议为 Z-up/m、wxyz；box 的 `size_m` 是实际尺寸，GUI 不转半尺寸。

## 实施与旧文件清理

- 新增类/字段须有当期消费者；输入校验有明确边界和唯一所有者。复用成熟库及已验证内部契约，不增加不必要的兼容、兜底、硬编码、重复校验或通用框架。
- 旧 `src/main.py`、`views/`、`controllers/`、`models/`、`config/` 属于待替换的 FlySim/Gateway 业务，已保存本地基线 `3cc31f8`。新包不得导入它们作为兼容层。
- 按 G8 在新流程贯通后做引用检查，只删除已被完全替代且无消费者的旧业务/入口；保留无关 demo 和资源，不提前批量删除旧目录。
- 同一文件只有一个写入者；主代理负责共享接口、集成、Git 暂存和本地提交，用户手动 push。保持现有忽略规则；文档、环境和 `.superpowers` 证据本地保留。
- 行为测试采用 RED→最小实现→GREEN；不为目录或文案制造测试。GPU 验收由主代理串行执行，最终仅一次跨项目独立整体审查。
