# ZCode 用量监控组件（Tauri 版）

基于 Tauri 2 的 ZCode AI 用量监控桌面悬浮组件：无边框置顶悬浮窗，实时展示模型 token 用量、云端套餐额度（火山方舟 / opencode / DeepSeek / 无痕中转 / SCNet scent）、任务列表与实时活动。真磨砂 Acrylic 玻璃（DWM WCA_ACCENT_POLICY，与 Electron 版 koffi recipe 同源），液态玻璃视觉风格。exe ~8MB（Electron 版 78MB）。

![组件预览](docs/widget-preview-v3.jpg)

## 功能特性

- **模型用量**：今日 / 近 7 日 / 累计 token 统计（本地 `model_usage` 持久表，跨 session 不剪枝），今日输入 / 输出分卡片展示，大数字 SVG 渐变金属质感
- **DSH 用量**：DeepSeek Harness 会话统计（读 `~/.dsh/sessions` zstd 压缩日志，**精确 token**，口径对齐 DSH GUI：输入 = 未缓存输入 + 缓存读取 + 缓存写入），顶部 ZCode / DSH 胶囊切换；今日输入大字 + 累计小字（悬停看完整明细）+ 今日分模型悬浮明细（hover ▦ icon）+ 今日输入 / 输出 / 推理 / 缓存四格 + 最近会话卡（项目 / 模型 / 相对时间 / token，超高内部滚动，15s 缓存）
- **云端额度**：火山方舟套餐（`GetCodingPlanUsage` / `GetAFPUsage`）、opencode Go 套餐（dashboard 解析）、DeepSeek 官方余额（`/user/balance`）、无痕中转余额+用量（`/v1/usage`）、SCNet scent 套餐额度（Token Plan 控制台 cookie 抓取），供应商单按钮+下拉面板切换；5 小时 / 每周 / 每月三窗口进度条 + 重置倒计时
- **任务列表**：当前运行任务 + 最近任务（含 token 消耗），点击卡片经 `zcode://open-project` 协议打开对应工作区
- **实时活动**：tail 本地 JSONL 日志，解析工具调用事件流
- **液态玻璃 UI**：深 / 浅双主题令牌，毛玻璃模糊（`blur(32px) saturate(115%)`）+ 天光描边，失焦自动折叠为小图标
- **置顶悬浮**：`alwaysOnTop` 无边框窗口，8 方向自定义 resize，最小 48x48；折叠态强化置顶（`SetWindowPos(HWND_TOPMOST)` 压过其它置顶窗口 + 2s 定时刷新）；防最小化 native 守护线程（Win+D 显示桌面后 ≤2s 自动恢复，`SW_SHOWNOACTIVATE` 不抢焦点）

## 界面说明

窄条竖屏悬浮窗（322x840，默认位于屏幕右缘），顶部胶囊可切换 ZCode / DSH 两个视图：

| 视图 | 区块 | 内容 |
|------|------|------|
| ZCode | 顶栏 | Z 渐变 logo、连接状态（绿点"实时连接"）、ZCode / DSH 视图切换胶囊、设置入口 |
| ZCode | 模型用量 | 今日累计大数字（如 `1.76M tokens`）、今日 / 近 7 日 / 累计统计行、今日输入 / 输出双子卡、请求次数胶囊 |
| ZCode | 云端额度 | 火山 / opencode / DeepSeek / 无痕中转 / scent 单按钮+下拉面板切换（供应商增多不撑爆标题栏）；火山显示套餐进度条（已用 % + 剩余 % + 重置倒计时），其余显示余额大字 + 2x2 用量网格（今日输入 / 输出、近 7 天 / 近 30 天）；无痕中转另含今日 Tokens / 请求 / 花费（actual_cost）、累计 Tokens 与按模型悬浮明细；scent 显示 Token Plan 额度进度条（已用 / 总额 Credits + 剩余天数）；底部数据来源与更新时间标注 |
| ZCode | 任务列表 | 运行中任务置顶，含状态标签（已完成 / 运行中）、模型标签（如 `deepseek-v4-flash$max`）、相对时间、token 消耗 |
| DSH | 用量总览 | 今日输入大字（billed 口径，本地自然日窗口）、累计小字（悬停看完整明细）、今日分模型悬浮（hover ▦ 看明细与占比）、今日输入 / 输出 / 推理 / 缓存四格 |
| DSH | 最近会话 | 会话卡列表（项目最后一层文件夹名 / 模型 / 相对时间 / 标题 / turn / 工具数 / billed 输入 token，hover 看未缓存与缓存明细），超高内部滚动 |

## 技术栈

- **框架**：Tauri 2（Rust 后端 + WebView2 前端），`withGlobalTauri` 全局桥（前端零构建，单文件 index.html 直载）
- **数据层**：Rust `rusqlite`（bundled，只读）+ `reqwest`（native-tls），火山方舟 SigV4 手写（hmac/sha2），对齐 Electron 版 data/ 直译
- **原生能力**：tauri 内置 Acrylic 窗口效果（WCA_ACCENT_POLICY）+ 手写 DWM 圆角 / 去边框（windows-sys）；拖拽用 Rust 原生线程高频 `GetCursorPos + SetWindowPos`（~500Hz，零 IPC，`timeBeginPeriod(1)` 提精度）
- **窗口**：frameless + transparent + alwaysOnTop + 失焦折叠（`onFocusChanged` 延迟 300ms 确认防竞态），前端自实现 8 方向 resize
- **打包**：tauri-bundler NSIS 单文件安装器（~8MB，currentUser 免 UAC，安装完自动启动）

## 快速开始

```bash
# 开发运行（需 Rust toolchain + Node 18+）
npm install
npm run tauri:dev    # 或双击 启动组件.bat
```

或运行构建产物 `src-tauri/target/release/bundle/nsis/` 下的安装器（安装到当前用户目录，桌面/开始菜单启动）。

> 旧版 Electron 实现（v2.0.x）保留在 `src/main.js` / `src/preload.js` / `src/windowsBackdrop.js` / `src/windowsChrome.js` / `src/data/*.js`，与 Tauri 版共用 `src/renderer/index.html`（前端经 `window.zapi` 桥双端共存：Tauri 由 shim 注入，Electron 由 preload 注入）。`npm start` 仍可跑 Electron 版。

## 配置（.volc.env）

凭证查找顺序：**exe 同目录（打包后）> 当前目录（dev 时是项目根）> 家目录**。参考：

```bash
# 火山方舟（云端额度 tab 必填，缺失时自动隐藏）
VOLC_AK_ID=xxxx
VOLC_AK_SECRET=xxxx
VOLC_PLAN_TYPE=coding        # coding(默认) | agent
VOLC_PLAN_TIER=Pro           # 可选，仅角标展示
VOLC_PLAN_START=2026-07-15T23:19:00   # 可选，套餐开通时间（倒推分模型明细）

# opencode Go 套餐（dashboard 抓取）
OPENCODE_GO_WORKSPACE_ID=xxx
OPENCODE_GO_AUTH_COOKIE=xxx

# SCNet Token Plan 额度（scent，控制台 session cookie 抓取；过期需重新从
# 浏览器 DevTools 复制整行 Cookie，含 httpOnly jsessionid）
SCNET_AUTH_COOKIE=jsessionid=xxx; userName=xxx; Token=xxx

# DeepSeek / 无痕中转：key 不在此文件，自动读 ~/.zcode/v2/config.json 的
# ZCode 模型配置，按 baseURL 含 api.deepseek.com / api.wuhen-ai.com 匹配取 apiKey
```

> `.volc.env` 已 gitignore，含火山 AK / opencode / scnet cookie 等敏感凭证，勿提交。DeepSeek / 无痕中转的 key 在 ZCode 自身配置（`~/.zcode/v2/config.json`），不在本项目管理范围。

## 数据源

| 数据 | 来源 |
|------|------|
| 任务列表 / token 用量 | `~/.zcode/v2/tasks-index.sqlite` + `~/.zcode/cli/db/db.sqlite` 的 `model_usage` 表 |
| DSH 会话用量 | `~/.dsh/sessions`（zstd 压缩日志逐帧解压，精确 usage，billed 输入含缓存，15s 缓存） |
| 实时活动 | `~/.zcode/cli/log/zcode-<date>.jsonl`（tail 400 行） |
| 火山套餐额度 | `open.volcengineapi.com` Ark OpenAPI（SigV4，15s 后台刷新） |
| opencode 额度 | dashboard 页面解析（SolidJS SSR 水合数据） |
| DeepSeek 余额 | `api.deepseek.com/user/balance` |
| 无痕中转余额+用量 | `api.wuhen-ai.com/v1/usage`（余额 / 今日累计 / 按模型，15s 后台刷新） |
| SCNet scent 额度 | `www.scnet.cn/acx/charge/account/currentuser/tokenplan/list`（控制台 cookie 抓取，已用/总额 Credits + 生效期，15s 后台刷新） |

网络调用全在后台 scheduler（15s 周期），`status()` 路径零网络、只读缓存，前端 1.5s 轮询（数据未变时按稳定签名跳过 DOM 重建）。SQLite 聚合 15s TTL 缓存（对齐 Electron 版 ttlMemo）。

## 构建打包

```bash
npm run tauri:build   # tauri build → src-tauri/target/release/bundle/nsis/ 单文件安装器
```

**便携版**：`src-tauri/target/release/zcode-usage-widget.exe`（~13MB）是裸 exe，依赖系统 WebView2（Win10/11 自带），**拷到任意目录双击即用**（实测）。`.volc.env` 放 exe 同目录即可读凭证。发布时二选一：便携 exe（免安装）或 NSIS 安装器（3.5MB，开始菜单/桌面快捷方式）。

## 目录结构

```
src-tauri/
├── Cargo.toml            # 依赖：tauri2 / rusqlite(bundled) / reqwest / hmac+sha2 / zstd / windows-sys
├── tauri.conf.json       # withGlobalTauri、frontendDist=../src/renderer、NSIS 打包
├── capabilities/         # IPC 权限（core:default + start-dragging）
└── src/
    ├── main.rs           # 入口：单实例锁 + setup（scheduler 启动 + 建窗）
    ├── window.rs         # 窗口：frameless/transparent/Acrylic 效果 + DWM 圆角去边框
    ├── commands.rs       # IPC：status/win_move/win_drag_start/win_resize/getPos/opacity/pinTop/quit/openTask
    └── data/
        ├── mod.rs        # status() 聚合 + 15s TTL 缓存（对齐 data/index.js）
        ├── sqlite.rs     # tasks / model_usage / live activity（rusqlite 只读）
        ├── logs.rs       # （并入 sqlite.rs）JSONL 尾部读 + 会话信号
        ├── volc.rs       # 火山 SigV4 + 套餐解析 + .volc.env 加载
        ├── deepseek.rs   # DeepSeek 用量聚合 + 余额
        ├── opencode.rs   # opencode 用量 + Go 套餐 dashboard 抓取
        ├── wuhen.rs      # 无痕中转余额+用量（/v1/usage）
        ├── scnet.rs      # SCNet scent 套餐额度（控制台 cookie 抓取）
        ├── dsh.rs        # DSH 会话用量（zstd 逐帧解压 + 精确 usage + 按模型聚合）
        ├── net.rs        # 共用 HTTP 客户端
        └── scheduler.rs  # 后台刷新调度（tauri::async_runtime::spawn，15s 周期）
src/
├── renderer/index.html   # 前端单文件（双端共用：Tauri shim / Electron preload 注入 window.zapi）
├── main.js               # [Electron legacy] 主进程（v2.0.x，保留）
├── preload.js            # [Electron legacy] contextBridge 桥
├── windowsBackdrop.js    # [Electron legacy] Accent 模糊
├── windowsChrome.js      # [Electron legacy] DWM 圆角去边框
└── data/*.js             # [Electron legacy] JS 数据层（Tauri 版为 Rust 直译）
docs/widget-preview-v3.jpg   # 界面预览图
```

## 已知坑

- **透明窗口系统拖拽内容消失**：WebView2 透明窗口在系统移动循环（SC_MOVE，`startDragging` / `data-tauri-drag-region`）期间暂停合成渲染 → 拖动时内容消失。解决方案：弃用系统拖拽，Rust 原生线程 `GetCursorPos + SetWindowPos` 高频移动（~500Hz，零 IPC）；`timeBeginPeriod(1)` 提升 sleep 精度（默认 15.6ms 粒度只有 ~60Hz，不跟手）
- **失焦折叠竞态**：tauri `onFocusChanged` 事件异步派发，失焦后快速点击回窗口会先收到 false 再收到 true，立即折叠会把刚聚焦的窗口缩掉（实测"按住 header 拖动时窗口折叠"）。修复：失焦后延迟 300ms 确认，期间重新聚焦则取消；拖动期间（`__dragGuard`）忽略折叠
- **最小化后 JS 定时器停摆无法自救**：Win+D / 显示桌面会最小化所有窗口（TOPMOST 也躲不过），窗口一旦最小化 WebView2 暂停渲染 → 前端 `setInterval` 全部冻结，靠前端调 pinTop 恢复自己是不可能的（实测卡死 iconic）。修复：防最小化放 Rust native 守护线程（`window.rs` `start_anti_min_guard`，每 2s 查 `IsIconic` → `SW_SHOWNOACTIVATE` 恢复 + 重插 topmost 链顶）
- **tauri 无内置 set_opacity**：手写 Win32 `SetLayeredWindowAttributes`（前端当前未调用，接口完整性保留）
- **tauri-bundler 无 portable target**：只有 msi / nsis（Tauri 2.9.3 确认），NSIS currentUser 单文件安装器为最近体验
- **后端数据读取走 spawn_blocking**：SQLite 聚合 ~180ms/轮在 tokio 阻塞池执行，不占 UI 线程（Electron 版拖动卡顿根因在 Rust 侧不存在）

## 版本历史

- `v3.0.1` DSH 用量总览改今日口径：今日输入大字（本地自然日窗口）+ 累计小字（悬停看完整明细）+ 今日分模型悬浮 + 今日输入/输出/推理/缓存四格；删会话/turn/step/工具调用摘要行与"X 会话"徽标（Rust dsh.rs 与 Electron legacy dsh.js 同步实现）
- `v3.0.0` Tauri 2 重构：exe 78MB→~8MB，内存减半；前端零改动复用（zapi shim 双端共存）；数据层 Rust 直译（对拍验证一致）；原生线程高频拖拽 + 失焦折叠竞态修复（透明窗口 SC_MOVE 渲染暂停 / onFocusChanged 异步竞态两大坑）；NSIS 单文件安装器
- `v2.0.6` DSH 视图升级：精确 token（对齐 GUI billed 口径：输入含缓存读取/写入，usage chunk + message 双源去重）+ 最近会话卡列表（真实 cwd 最后一层文件夹名）+ 分模型用量悬浮 + 会话列表内部滚动 + 顶栏去时钟/标题精简 + bat 启动改为本地 electron.exe（不依赖 npx）
- `v2.0.6` 补丁：修复两处无声退出——① koffi 偶发崩溃（`0xc0000409` 栈破坏 fail-fast）：删启动 DWM 探针 + koffi 升级 3.1.5；② dsh 会话 zstd 解压 OOM（Electron 35 的 `zstdDecompressSync` 输出 Buffer 进老生代后 major GC 不触发，海量会话帧累积数 GB 提交内存导致进程无声退出）：解压循环每 200 帧制造堆压力强制 GC（实测峰值 9.4GB→1.25GB）
- `v2.0.5` DSH（DeepSeek Harness）会话用量（zstd 日志聚合 + 视图切换框架）+ 任务栏图标修复（删 `setAppUserModelId`，避开旧身份缓存导致的空白占位图标）
- `v2.0.4` 无痕中转余额/用量查询（`/v1/usage`）+ 供应商下拉选择器（.section 堆叠上下文修复）+ 云端数据刷新 60s→15s
- `v2.0.3` 展开态拖动卡顿修复（日志尾部读 + 聚合 15s 缓存 + 渲染跳过）
- `v2.0.2` 滑块填充按轨道范围归一化 + 单实例锁
- `v2.0.1` portable 单文件 exe + 凭证读取兜底
- `v2.0.0` Electron 版重构：真磨砂 Acrylic + 全数据层移植（pywebview 版历史见 `archive-python` 分支）
