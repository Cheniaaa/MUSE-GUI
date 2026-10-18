# Muse 研究工作台

通过 HTTP/WebSocket 连接 Muse Server，编辑实验与场景、提交 Run、观察 Viser 视口及读取持久结果。

## 启动

在本工程目录使用已准备的 Python 3.13 环境：

```powershell
& '.\.venv\Scripts\Activate.ps1'
python -m muse_gui
```

首次准备环境时，安装固定依赖及本地包：

```powershell
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
```

`requirements.in` 是直接依赖，`requirements.txt` 是完整版本锁。应用图标随包安装，启动不依赖当前工作目录。

## 使用流程

1. **设置**：选择仅连接已有地址，或填写 WSL distribution、Linux user、项目/venv/数据目录及端口。点击连接或启动本机后端。连接不会安装依赖；工作台使用 API revision 2 和新 Catalog 数据目录。
2. **资源**：导入基础飞行、ISAC、history 模板或空白地面；也可从代码目录/ZIP 登记研究包，再选“用于当前草稿”。模板导入不启动 Run。已有资源更新产生新的 revision，旧 Run 内容保持冻结。
3. **编辑草稿**：配置缺项和非法输入仍可保存。检查结果中的有效值、来源与错误来自 Server；删除覆盖表示恢复下层值，不把默认数值写入请求。研究包/组合/模型切换需确认。
4. **场景**：编辑可支持的静态 box、摆放实体、查看只读对象原因。尺寸单位为实际米制长宽高；数值和三维拖动共用草稿 undo。CPU 预览是编译初态，不运行 Task 或训练；重编译时旧图明确标过期。
5. **开始**：明确选择 mode、正整数 decision 预算，以及必要 checkpoint。程序捕获当前草稿，按需更新资源/派生环境，保存 Experiment，再提交 Run；期间新编辑属于下一版。响应丢失时可按已保存 Experiment 查找结果，不自动重复提交。
6. **Run / 历史**：共享 world/rate/启用状态由 Server 管理，相机与选择按客户端隔离。INTERACTIVE 支持暂停、继续、单步、重置 world；命令以实际返回结果为准。历史可复制原始 source、查看冻结详情、从 checkpoint 新建训练或评价。
7. **结果**：曲线只读持久 metrics；数组需明确索引或统计，未知单位不推断可比性。显示缓存有界，完整结果通过 artifact 下载并核对 hash/大小。日志编码问题和分页缺口会明确显示。

关闭窗口会询问草稿保存并释放本客户端/草稿预览；**后台 Run 继续**。取消某个 Run 和停止本机后端是独立操作；停止后端取消活动 Run、保留排队项，未确认退出不会显示已停止。

## 范围与验证

当前联合验收：唯一整体审查的4项 Important 已修复，此前最终 GUI **254 passed / 6 skipped**。三参考六个 TRAIN/EVALUATE 及功能 INTERACTIVE（48步、77.51秒）已完成。最新真实CPU草稿预览确认模型显示、相机操作和持续绘制正常；此前性能脚本误把0×0二维选择框层当成三维canvas，已修正取证对象。Run集合仍为10个＝7成功＋3取消；headless有完整30秒样本，display性能尚未采样，完整B9仍部分验收。

正式源码位于 `src/muse_gui/`，按功能分为 `app/backend/experiments/catalog/scene/runs/widgets`。研究代码只由后端验证/运行进程加载，GUI 不 import 研究包，也不拥有物理循环。

ISAC 学习效果仍未达既有门槛，history 学习收益对照尚未完成；模板的工程可用性不代表研究结论成立。GUI 与后端的交付/验收状态以本地联合报告及 `.superpowers/integration/2026-10-18/` 原始证据为准。

开发检查：激活环境后运行 `python -m pytest tests -q`。普通 Qt 测试可 offscreen；真实 WebEngine、DPR 和 Windows/WSL 生命周期证据使用桌面运行。
