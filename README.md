# ZCode 用量监控桌面组件

一个轻量的桌面悬浮小组件，用于实时监控 [ZCode](https://zcode.app) 智能体的运行状态与 token 用量。基于 `pywebview` 实现无边框、置顶的桌面悬浮窗，每 2 秒刷新一次本地数据。

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## 功能

组件在一个紧凑的悬浮窗内展示三类信息：

- **模型用量** — 今日 / 本周 / 累计的输入、输出、缓存读取、推理 token 数与请求次数，按模型分解。数据来自 ZCode 本地持久化的 `model_usage` 表（与 ZCode 设置面板中"今日 / 累计"一致）。
- **云端套餐额度**（可选）— 读取火山方舟 OpenAPI 的 AgentPlan 套餐额度（5 小时 / 每周 / 每月）的额度、已用、剩余与重置时间。未配置凭证时该区块自动隐藏。
- **任务列表** — 最近执行的 ZCode 任务，显示标题、状态、模型、时间，点击可一键在 ZCode 中打开对应工作区。

此外还支持：窗口拖拽、置顶悬浮、点击右上角或 Z 图标折叠 / 展开面板、实时活动日志（当前正在执行的工具调用与最近事件流）。

## 数据来源

组件只读取本地数据，不做任何写入：

| 数据 | 路径 | 说明 |
| --- | --- | --- |
| 任务索引 | `~/.zcode/v2/tasks-index.sqlite` | 当前 / 运行中任务与最近任务列表 |
| token 用量 | `~/.zcode/cli/db/db.sqlite`（`model_usage` 表） | 本地持久累计的 token 计数，权威来源 |
| 实时活动 | `~/.zcode/cli/log/zcode-<date>.jsonl` | 工具调用 / 模型活动事件流 |
| 云端套餐 | 火山方舟 OpenAPI | 只读查询，需自行配置凭证 |

## 环境要求

- Python 3.10 及以上
- Windows / macOS / Linux 桌面环境（窗口由系统 WebView 渲染）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2.（可选）配置火山方舟凭证，启用云端套餐用量展示
cp .volc.env.example .volc.env
# 然后编辑 .volc.env 填入 VOLC_AK_ID / VOLC_AK_SECRET

# 3. 启动
python widget.py
```

Windows 用户也可直接双击 `启动组件.bat` 启动（会优先使用无控制台窗口的 `pythonw`）。

> 不配置 `.volc.env` 也能正常运行——本地 token 用量照常展示，仅「云端套餐额度」区块会自动隐藏。

## 项目结构

```
widget.py              # 入口：创建悬浮窗 + 绑定 JS 桥
widget.html            # 前端界面（单文件，含样式与逻辑）
data.py                # 数据后端：读取本地 SQLite / 日志 + 火山 OpenAPI 签名调用
test_volc_usage.py     # 火山方舟 OpenAPI 用量查询的独立测试脚本
启动组件.bat           # Windows 启动脚本
.volc.env.example      # 火山凭证模板（复制为 .volc.env 后填入）
requirements.txt
```

## 关于火山方舟凭证

云端套餐用量通过火山方舟 OpenAPI 查询，使用 SigV4（HMAC-SHA256）签名鉴权。凭证通过环境变量或项目根目录下的 `.volc.env` 文件读取，**不在代码中硬编码**。获取方式：

1. 登录 [火山引擎控制台](https://console.volcengine.com/) → 方舟（OpenAPI）→ 创建 AccessKey；
2. 将 `AccessKey ID` 与 `Secret Access Key` 填入 `.volc.env`。

`.volc.env` 已在 `.gitignore` 中忽略，请勿提交到版本库。

## 许可证

[MIT](./LICENSE)
