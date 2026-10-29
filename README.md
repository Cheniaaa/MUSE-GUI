# Muse 研究工作台

**从实验草稿、场景编辑到运行观察与历史复盘的桌面研究客户端。**

Muse GUI 是 [Muse 仿真与强化学习平台](https://github.com/Cheniaaa/MUSE) 的配套工作台，使用 PySide6 / Fluent 界面，通过 HTTP / WebSocket 连接 Muse Server，并以 Qt WebEngine 嵌入 Viser 三维场景。

**本仓库仅提供客户端，不包含仿真后端。** 物理计算、研究包执行和策略学习由 Server 管理的 Worker 完成，GUI 不加载研究包或拥有训练循环。

## 主要功能

| 工作环节 | 能力 |
|---|---|
| 资源与研究配置 | 浏览和登记资源版本、导入参考模板、选择研究包 / 组合 / 模型，编辑 schema 驱动参数 |
| 实验草稿 | 保存不完整输入、检查配置、查看参数来源、撤销 / 重做，并从已有 Run 派生新草稿 |
| 场景编辑 | 实体摆放、受支持的静态 box 编辑、三维选择与拖动、CPU 初态预览 |
| 运行管理 | 提交 TRAIN / EVALUATE / INTERACTIVE，查看状态、录制进度和日志，控制实时 Viser / Native 显示 |
| 结果分析 | 持久指标曲线、双 Run 对照、原始产物下载及大小 / SHA-256 校验 |
| 历史复盘 | 从已发布轨迹打开三维回放，查看同样本诊断，定位 world / episode，并导出 MP4 |

```text
连接 Server → 导入资源 → 编辑草稿与场景 → 检查并提交 Run
                                               ↓
                          指标 / 日志 / checkpoint / 轨迹
                                               ↓
                                 历史三维回放 → MP4
```

## 安装与启动

### 环境要求

- **客户端**：当前目标为 Windows 桌面环境，Python **3.13**。
- **后端**：按 [Muse README](https://github.com/Cheniaaa/MUSE#readme) 独立准备 Linux / WSL2、Python 3.12 和 NVIDIA GPU 环境。
- **显示**：三维视口使用 Qt WebEngine / WebGL；本机后端启动入口通过 Windows 的 `wsl.exe` 工作。仅连接已有 Server 时无需由 GUI 启动 WSL。

客户端与后端使用不同的 Python 环境，不要将两仓依赖安装到同一虚拟环境。后端 GPU 要求不等于客户端也必须使用相同 GPU 配置。

### 首次安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/Cheniaaa/MUSE-GUI.git
cd MUSE-GUI
py -3.13 -m venv .venv
& '.\.venv\Scripts\Activate.ps1'
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m muse_gui
```

[requirements.in](requirements.in) 声明直接依赖，[requirements.txt](requirements.txt) 固定完整依赖版本。应用图标随包安装，无需复制额外资源目录。

后续启动只需激活已有环境：

```powershell
& '.\.venv\Scripts\Activate.ps1'
python -m muse_gui
```

## 连接后端

首次进入**设置**页，选择一种连接方式：

| 方式 | 配置与用途 |
|---|---|
| 连接已有 Server | 填写实际 HTTP 地址，例如 `http://127.0.0.1:8001`；后端由终端或其他进程启动 |
| 启动本机 WSL 后端 | 填写 WSL distribution、Linux user、后端项目目录、虚拟环境目录、数据目录及 HTTP / viewer 端口 |

WSL 项目、环境和数据目录应填写 **Linux 绝对路径**，不是 Windows 盘符或 UNC 路径。数据目录是后端 Catalog 的持久存储，不是客户端安装目录；同一研究目录重连时继续使用原目录。

连接和启动操作不会自动安装后端依赖。工作台当前使用 **API revision 2**，连接前请确保后端版本匹配，并且 GUI 可以访问 Server 返回的 viewer 地址。

## 推荐工作流

1. **导入资源**：使用基础飞行、ISAC、history 或空白地面模板，也可从源码目录 / ZIP 登记研究包。模板导入不启动仿真，更新资源产生新 revision，旧 Run 的配置保持冻结。
2. **编辑草稿**：配置任务、算法、角色、模型和参数覆盖。不完整输入仍可保存；检查结果中的有效值、来源与错误由 Server 返回。
3. **配置场景**：摆放实体，编辑受支持的静态 box。数值编辑与三维拖动共享撤销记录；未形成完整几何的输入保留，并显示不能预览的原因。CPU 预览只编译初态，不运行 Task 或训练。
4. **检查并开始**：选择运行模式、正整数 decision 预算及必要 checkpoint，可同时配置诊断录制。提交捕获当时的草稿，后续编辑属于下一版，不修改已启动的 Run。
5. **观察与调试**：查看实时场景、运行状态与日志。INTERACTIVE 支持暂停、继续、全 world 单步和指定 world reset，是否执行成功以 Server 返回结果为准。
6. **分析与复盘**：查看指标、比较 Run、下载原始产物；从 checkpoint 新建训练 / 评价，或从 trajectory 打开历史回放。

**预算按 Run 的 decision 计数，不乘 world 数。** checkpoint 恢复的是学习状态，环境从新 episode 开始，不是恢复某一历史物理帧。

### 诊断录制、回放与视频

运行前在参数表单配置 `recording`；`null` 表示关闭。启用时显式填写：

| 字段 | 含义 |
|---|---|
| `world_ids` | 需要采集的 world 列表 |
| `every_n_decisions` | 每个 episode 内的 decision 采样间隔 |
| `max_data_bytes` | 未压缩动态数组的字节预算，不是最终 ZIP 大小或视频时长 |

录制独立于实时视口；关闭显示、切换观察 world 或断开 GUI 不会改变已冻结的采样配置。容量耗尽时保存已有轨迹并停止录制，后台 Run 继续执行。

在**结果 → 原始 artifact / checkpoint**中选择已发布的 trajectory，点击**历史回放**：

- 切换 world / episode，播放、暂停、调整倍速或逐样本前后定位。
- 选择对象，查看与当前场景同一样本的位姿、速度、动作、reward、已启用 cost 和结束信息。
- 相机连接就绪后，显式填写 FPS、宽和高，将一个 world / episode 的已录部分导出为 MP4；宽高须为正偶数。
- 视频完成后，沿产物列表下载并校验；导出失败不会改变源 Run 的状态。

回放和视频消费同一份轨迹，不重新运行策略。Run 结束后仍可复盘，但 **Server 必须在线**；当前不是独立离线播放器。增加视频 FPS 不会补出未采集的轨迹细节，稀疏 reward / cost 也不表示跨间隔累计。

## 关闭与数据安全

| 操作 | 影响 |
|---|---|
| 隐藏回放面板 | 卸载本地网页，保留历史会话、游标和已冻结导出，重显后恢复 |
| 关闭或替换历史会话 | 释放会话，并取消尚未完成的导出；已发布轨迹与视频保留 |
| 关闭 GUI | 提示保存草稿，释放本客户端、预览和历史会话；**后台 Run 继续** |
| 取消 Run | 向后端显式请求在安全边界停止该 Run |
| 停止本机后端 | 独立操作，取消活动 Run、保留排队项；未确认退出不会显示已停止 |

结果绑定原 Catalog / Run / artifact。切换后端不会把旧结果重新归属于新 Catalog，跨来源操作会被拒绝。下载会核对大小与 SHA-256，并保存来源记录；曲线显示缓存不替代完整原始数据。

## 离线编辑

成功读取的目录和 schema 按 Catalog / API revision 缓存在本机，界面标明来源、保存时间和离线状态：

- 可浏览已缓存的资源分页；目录缓存完整时可离线编辑配置，未完成草稿可保存。
- 缺页、缓存损坏或来源不匹配会明确提示，不将缓存当作当前 Server 的权威状态。
- 配置检查、资源绑定、提交 Run 和历史回放仍需要真实后端连接。
- “使用已登记版本”复用选定资源引用；“更新后运行”检查并上传当前源码快照，两者不会偷偷替换旧 Run 的资源。

## 项目结构

正式源码位于 [src/muse_gui](src/muse_gui)：

| 目录 | 职责 |
|---|---|
| `app` | 应用入口、主窗口、设置和跨功能装配 |
| `backend` | HTTP / WebSocket、连接状态与 WSL 启动入口 |
| `experiments` | 草稿、schema 表单、检查与提交 |
| `catalog` | 资源目录、登记与缓存 |
| `scene` | 场景编辑与 CPU 预览 |
| `runs` | 运行控制、历史、指标、日志与回放 |
| `widgets` | 共享控件与 WebEngine 容器 |

## 当前边界

- 本项目是研究工作台，不是独立仿真器；可用研究能力取决于所连接的 Muse 后端与研究包。
- 场景编辑目前支持实体摆放和有限静态几何编辑，不是通用 CAD 或机械装配工具。
- 模板可运行不代表策略已收敛；ISAC 安全目标、完整学习对照和 history 学习收益仍需研究验证。
- 指标对照需保持定义、单位和实验设置可比；单次小场景性能不代表通用性能上限。
