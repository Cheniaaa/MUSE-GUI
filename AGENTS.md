# AGENTS.md


## 项目概述

MUSE 是一个基于 Python 和 PySide6 (Qt6) 构建的**无人机仿真 GUI 应用程序**。它为配置、控制和监控基于 PX4 的无人机仿真提供了桌面界面，并支持强化学习训练功能。

**技术栈：** Python 3.13、PySide6、PySide6-Fluent-Widgets、PyQtGraph、numpy

## 开发规则
- 运行前需要使用.venv虚拟环境，使用"E:\Projects\Python\03_Work\Lab\MUSE\.venv\Scripts\activate"来激活运行环境

## 运行应用

```bash
# 主 FlySim 应用
python -m src.main
```

## 架构设计

应用采用 **Model-View-Controller (MVC)** 模式，结合 Qt 的信号/槽机制。

### 组件层次

| 层级 | 位置 | 用途 |
|------|------|------|
| **Views（视图层）** | `src/views/` | UI 组件（FlySimWindow、HomePage、DroneConfigPage） |
| **Controllers（控制层）** | `src/controllers/` |  |
| **Models（模型层）** | `src/models/` |  |
| **Config（配置层）** | `src/config/` | 应用配置、全局信号总线、QSS 样式表 |

### 核心文件

- `src/main.py` - 应用入口点，创建 `FlySimWindow`
- `src/views/main_window.py` - `FlySimWindow` 窗口类
- `src/views/home_view.py` - 主界面，包含仿真控制、遥测数据、训练曲线图
- `src/views/drone_view.py` - 无人机配置界面（机架、动力系统）

## UI 框架

使用 `qfluentwidgets`（Fluent 设计）：
- `FluentWindow` 导航界面
- 自动主题检测（浅色/深色）
- 高 DPI 支持已启用
- 卡片组件：`BasicSettingCard`、`NetworkSettingCard`、`ControlPanelCard`、`DroneSettingCard`

## 主要功能

- MavLink 网络配置（UDP/TCP）
- 多机 PX4 SITL 仿真
- RL 算法集成（PPO、DQN、DDPG）
- 实时遥测数据显示（位置、速度、姿态、GPS）
- PyQtGraph 训练曲线可视化
