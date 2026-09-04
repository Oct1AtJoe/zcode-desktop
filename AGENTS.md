# AGENTS.md

## 项目记忆（跨 session 持久，新 session 自动加载；有新事实请让我追加到本节）

> ZCode 用量监控桌面组件（**Tauri 2 版为主，v3.0.0**；Electron v2.0.x legacy 保留在 `src/`，勿主动改）：无边框置顶悬浮窗 322x840，每 ~1.5s 轮询 `window.zapi.status()`（Tauri 经 `withGlobalTauri` + 前端 shim，Electron legacy 经 preload contextBridge），读取 `~/.zcode` 本地数据（tasks-index.sqlite / db.sqlite 的 model_usage / JSONL 日志）+ `~/.dsh/sessions`（zstd 压缩会话日志）+ 火山方舟 / opencode / DeepSeek / 无痕中转 OpenAPI（后台 scheduler 15s 刷新缓存），展示模型 token 用量 / 云端套餐额度 / 任务列表 / DSH 会话。

### 目录结构概览（Tauri 版为主）
- `src-tauri/src/main.rs` -- 入口：单实例锁插件 + setup（scheduler 启动 + 建窗 + 防最小化守护）+ invoke_handler 注册 9 个命令
- `src-tauri/src/window.rs` -- 窗口层：322x840@(1260,80)、frameless/transparent/alwaysOnTop/resizable(false)/shadow(false)；tauri 内置 Acrylic（`WindowEffect::Acrylic` + ACCENT_TINT）+ 手写 DWM 圆角/去边框（windows-sys）；`scale_factor()` CSS 像素换算
- `src-tauri/src/commands.rs` -- IPC：`status`（spawn_blocking）/ `win_quit` / `win_get_pos` / `win_move` / `win_drag_start` / `win_resize` / `win_set_opacity` / `win_pin_top`（折叠态强化置顶）/ `open_task`
- `src-tauri/src/data/` -- Rust 数据层：`mod.rs`（`status()` 聚合 + 15s TTL 缓存 + dump_status 冒烟测试）、`sqlite.rs`（rusqlite bundled 只读 + JSONL tail）、`volc.rs`（SigV4 + 套餐 + .volc.env 加载）、`deepseek.rs` / `opencode.rs` / `wuhen.rs` / `dsh.rs`（zstd 逐帧解压）、`net.rs`（共用 HTTP 客户端）、`scheduler.rs`（4 个独立 tokio 循环，15s TTL）
- `src-tauri/tauri.conf.json` -- `withGlobalTauri`、`frontendDist: ../src/renderer`、NSIS currentUser 打包
- `src-tauri/capabilities/default.json` -- IPC 权限（core:default + start-dragging）
- `src/renderer/index.html` -- 前端单文件（HTML+内联 CSS+内联 JS，~2500 行），**Tauri / Electron 双端共用**
- `src/main.js` / `preload.js` / `windowsBackdrop.js` / `windowsChrome.js` / `src/data/*.js` -- [Electron legacy] v2.0.x 保留，与 Tauri 版共用 `index.html`
- `package.json` -- Electron legacy 入口（main: src/main.js）+ tauri scripts；`启动组件.bat` -- Tauri 启动（优先 release exe，无产物走 `npx tauri dev`）；`.volc.env` -- 凭证（已 gitignore）；`docs/widget-preview-v3.jpg` -- 预览图

### 关键文件入口
- 主进程入口: `src-tauri/src/main.rs` -- `npm run tauri:dev` / `启动组件.bat` / 打包 exe 均走这里
- 数据聚合: `src-tauri/src/data/mod.rs` 的 `status()` -- 所有前端数据源，**零网络只读缓存**（网络调用全在 scheduler 后台）
- 窗口/装饰: `src-tauri/src/window.rs` 的 `create_main()` / `apply_chrome()` / `start_anti_min_guard()`（防最小化守护）
- 前端轮询: `src/renderer/index.html` 的 `startPolling()`（每 1500ms 调 `zapi.status()`）
- 版本号: `src-tauri/Cargo.toml` + `tauri.conf.json` **两处同步**

### 功能模块
- **窗口/桥接层** (`main.rs`+`window.rs`+`commands.rs`): frameless + `alwaysOnTop` + `transparent`；tauri 内置 Acrylic（WCA_ACCENT_POLICY，与 Electron koffi recipe 同源），页面透明像素显示窗口背景模糊；DWM 圆角（DWMWCP_ROUND）+ 去 1px 边框（DWMWA_BORDER_COLOR=NONE）；前端自实现 8 方向 resize（`win_resize` 后重设 `apply_chrome`）；失焦折叠由前端 `onFocusChanged` 监听实现；折叠态强化置顶 `win_pin_top`（SetWindowPos HWND_TOPMOST 压过其它置顶窗口 + 前端 2s 定时刷新，展开态停止）；防最小化由 native 守护线程 `start_anti_min_guard`（每 2s 查 IsIconic → SW_SHOWNOACTIVATE 恢复，Win+D 后 ≤2s 弹回）
- **拖拽**（重要，分两态）: 展开态用**原生线程高频拖拽**（`win_drag_start`：GetCursorPos + SetWindowPos ~500Hz，零 IPC，`timeBeginPeriod(1)` 提 sleep 精度；**不用系统 startDragging**——透明窗口在 SC_MOVE 期间 WebView2 暂停合成、内容消失）；折叠态用 JS `pointerdown + setPointerCapture + win_move()`（窗口 48x48 太小，指针捕获保证移出窗口仍收事件）。logo 点击展开由 JS pointerup 的移动阈值区分点击/拖动
- **数据采集** (`data/`): `sqlite.rs` 的 `read_tasks`/`read_task_tokens`/`read_usage`/`read_live_activity`（读 `~/.zcode/v2/tasks-index.sqlite` + `~/.zcode/cli/db/db.sqlite` 的 model_usage 表 + tail JSONL 日志）；`volc.rs` SigV4（open.volcengineapi.com / SERVICE=ark / REGION=cn-beijing / VERSION=2024-01-01）；`wuhen.rs`（api.wuhen-ai.com/v1/usage）；`dsh.rs`（`~/.dsh/sessions` zstd 逐帧解压，精确 usage billed 口径，15s 缓存）
- **前端渲染** (`index.html`): ZCode / DSH 双视图胶囊切换；供应商单按钮+下拉面板（火山/opencode/DeepSeek/无痕）；DSH 会话卡列表（超高内部滚动）；SVG 渐变数字（`getComputedTextLength()` 测宽，display:none 容器估算、切 tab 后 remeasure）；玻璃通透度滑块只改 CSS 变量 `--glass-alpha`；任务卡点击 `openTask` 走 `zcode://open-project?directory=` 协议（cmd start）
- **性能设计**: `status()` 零网络（15s 聚合缓存）；SQLite 聚合走 `spawn_blocking`（tokio 阻塞池，~180ms/轮不占 UI 线程）；渲染端 `refreshSig` 稳定签名，数据未变跳过整块 DOM 重建；时钟独立 1s tick

### 技术栈与工程配置
- 后端: Rust + Tauri 2（edition 2021）+ tauri-plugin-single-instance；rusqlite（bundled，只读）、reqwest、tokio、hmac+sha2（SigV4）、chrono、zstd、regex、urlencoding、windows-sys（DWM/Win32）
- 前端: 原生 HTML/CSS/JS 单文件，无框架无构建；`withGlobalTauri` 全局桥（前端检测 `window.__TAURI__` 注入 `window.zapi` shim，与 Electron preload 接口对齐）
- 打包: tauri-bundler NSIS 单文件安装器（~3.5MB，currentUser 免 UAC）；**便携版**为裸 exe `src-tauri/target/release/zcode-usage-widget.exe`（~13MB，依赖系统 WebView2，Win10/11 自带，拷走即用）
- 平台: 仅 Windows（DWM/Win32 特有）
- 凭证 (`.volc.env`): `VOLC_AK_ID`/`VOLC_AK_SECRET`（火山）/`VOLC_PLAN_TYPE`（coding 默认/agent）/`VOLC_PLAN_TIER`/`VOLC_PLAN_START`/`OPENCODE_GO_WORKSPACE_ID`/`OPENCODE_GO_AUTH_COOKIE`；查找顺序 **exe 同目录 > 当前目录 > 家目录**；DeepSeek / 无痕中转 key 自动读 `~/.zcode/v2/config.json` 的 ZCode 模型配置，按 baseURL 含 api.deepseek.com / api.wuhen-ai.com 匹配
- 网络注意: `github.com` 主站间歇不可达（DNS 抖动/被墙），已把 `20.205.243.166` 钉入 `C:\Windows\System32\drivers\etc\hosts`（备份在 `/tmp/hosts.bak`）；`api.github.com` 一直通；SSH 密钥 `id_ed25519` 未注册到 GitHub 账号（SSH 推不了）

### 运行/构建/发布命令
- 开发运行: `npm run tauri:dev` 或双击 `启动组件.bat`（需 Rust toolchain + Node 18+；bat 优先跑已构建的 release exe）
- 构建: `npm run tauri:build`（tauri build）→ `src-tauri/target/release/bundle/nsis/` 安装器；便携 exe 在 `src-tauri/target/release/zcode-usage-widget.exe`
- Electron legacy: `npm start`（仍可跑，仅应急参考）
- 冒烟对拍: `src-tauri/` 下 `cargo test -- --nocapture dump_status` 打印 status() JSON，与 Electron 版 node 脚本输出对比
- 发布链路（v3.0.0 起，参照 README）:
  ```
  bump src-tauri/Cargo.toml + tauri.conf.json version（两处）
  npm run tauri:build              # NSIS 安装器 或 便携 exe 二选一
  git add ... && git commit -m "feat/fix: ..."
  git tag v<v> && git push origin main && git push origin v<v>
  gh release create v<v> --title "ZCode 用量组件 v<v>（...）" --notes "..." <exe>
  ```
  注：`dist/` 与根目录的 3.0.0 exe 为便携产物副本；`.gitignore` 忽略 `/*.exe`，`dist/*` 保留 `!dist/*.exe` 例外

### 历史决策与已知坑（Tauri 版）
- **透明窗口系统拖拽内容消失**（v3.0.0 最重要的坑）: WebView2 透明窗口在系统移动循环（SC_MOVE，`startDragging` / `data-tauri-drag-region`）期间暂停合成渲染 → 拖动时内容消失。修复：弃用系统拖拽，Rust 原生线程 `GetCursorPos + SetWindowPos` 高频移动（~500Hz，零 IPC）；`timeBeginPeriod(1)` 提升 sleep 精度（默认 15.6ms 粒度只有 ~60Hz，不跟手）；HWND 裸指针不 Send，isize 传进线程再转回；线程自检测左键松开退出 [来源: commands.rs 注释]
- **失焦折叠竞态**（v3.0.0）: tauri `onFocusChanged` 事件异步派发，失焦后快速点击回窗口会先收到 false 再收到 true，立即折叠会把刚聚焦的窗口缩掉（实测"按住 header 拖动时窗口折叠"）。修复：失焦后延迟 300ms 确认，期间重新聚焦则取消；拖动期间（`__dragGuard`）忽略折叠 [来源: README 已知坑]
- **最小化后 JS 定时器停摆无法自救**（置顶强化时发现）: Win+D/显示桌面最小化所有窗口（TOPMOST 也躲不过），窗口最小化后 WebView2 暂停渲染 → 前端 setInterval（含 pinTop 定时器）全部冻结，靠前端恢复自己不可能（实测卡死 iconic）。修复：防最小化放 Rust native 守护线程 `window.rs::start_anti_min_guard`，每 2s 查 IsIconic → SW_SHOWNOACTIVATE 恢复（不抢焦点）+ 重插 topmost 链顶。另：验证命令是否编译进 exe 别用二进制明文搜索——LTO 把命令名比较内联成 x86 立即数，完整命令名不连续出现（假阴性）；可靠手段是临时 `.additional_browser_args("--remote-debugging-port=9333")` 起 WebView2 CDP，实调 `invoke()` 验证 [来源: commands.rs/window.rs 注释]
- **tauri 无内置 set_opacity**: 手写 Win32 `SetLayeredWindowAttributes`（WS_EX_LAYERED 与 DWM 圆角可能不兼容，已知坑）；前端当前未调用，仅为接口完整性保留（对齐 preload window.zapi.setOpacity）
- **tauri-bundler 无 portable target**（Tauri 2.9.3 确认）: 只有 msi/nsis；便携方案用裸 exe（依赖系统 WebView2，实测拷走即用）
- **后端数据读取走 spawn_blocking**: SQLite 聚合 ~180ms/轮在 tokio 阻塞池执行，不占 UI 线程（Electron 版拖动卡顿根因在 Rust 侧不存在）
- **setup 阶段不在 tokio runtime 上下文**: scheduler 的 spawn 必须走 `tauri::async_runtime`，直接 tokio::spawn 会 panic
- **resize 后重设 DWM 装饰**: 显示流程 / resize 可能重置圆角/边框（Electron 版实测折叠后出现 1px 白边），`win_resize` 末尾重设 `apply_chrome`
- **前端 CSS 像素 vs 物理像素**: 前端全链路用 CSS 像素（对齐 Electron DIP 语义），commands 层统一 `scale_factor` 换算
- **SVG 渐变数字测宽坑**（沿用 Electron 版规范）: display:none 容器里 `getComputedTextLength()` 返回 0，用估算宽度占位，切视图后 `remeasureView` 实测修正；SVG 无文本基线，flex 对齐用 center 不用 baseline
- **opacity-btn 用 div 不用 button**（沿用）: button 内容模型不允许 div，pop 会被踢出按钮导致点击错位
- **单实例锁**（main.rs）: 重复启动不叠窗，聚焦已有窗口
- **滑块填充归一化**（沿用 2.0.2）: range min=30 时 `--op-fill` 按 `(v-min)/(max-min)` 计算
- **Electron legacy 坑**（仅 legacy 相关，v2.0.x）: koffi 偶发崩溃（0xc0000409，删 DWM 探针 + koffi 3.1.5 修复）；dsh zstd 解压 OOM（Electron 35 的 `zstdDecompressSync` 输出进老生代后 major GC 不触发，每 200 帧强制 GC 修复）；日志整文件读阻塞主进程（尾部 chunk 读）；聚合 15s ttlMemo 缓存
- **遗留临时文件**: `.tmp_decompress.cjs`（~/.dsh/sessions zstd 日志解压调试脚本）、`.tmp_shot.png`（截图），用完可删，勿提交
- **测试体系薄弱**: 无自动化测试/CI，仅有 cargo test dump_status 冒烟 + 临时 node 脚本 [待补充]

---

## 角色定义
你是我的开发协作助手，主要帮助我开发这个 Tauri 桌面组件项目，后端为 `src-tauri/`（Rust），前端为 `src/renderer/index.html` 单文件（原生 HTML/CSS/JS，无框架，双端共用）。Electron legacy（`src/` 下 v2.0.x）仅作参考，不主动改动。

## 默认技术栈与偏好
- 后端用 Rust/Tauri 2，遵循现有 `src-tauri/src/` 各文件的模块风格与封装方式（window / commands / data 分层明确，注释对齐 Electron 版说明）。
- 前端 `index.html` 为单文件内联 CSS/JS，不引入框架；改动时保持单文件结构。
- 不要随意引入新依赖（Rust crate 或 npm 包）；确实需要时先说明原因（SQLite 用 bundled rusqlite，原生调用用 windows-sys / tauri 内置，HTTP 用 reqwest）。
- 修改代码前先阅读相关文件，避免凭空假设。
- 回答问题时尽量给出可直接落地的实现方案，而不是只讲概念。
- 涉及 DWM/Acrylic/圆角/拖拽/打包等坑点时，优先参考「历史决策与已知坑」小节，不要重蹈覆辙。
- 性能问题优先定位根因并给验证方式：Rust 阻塞用 `cargo test` 冒烟计时验证，前端帧率用 DevTools Performance 录制。
- 保持良好的代码注释习惯，关键决策与坑点注释要写清原因（沿用现有"对齐 Electron 版 XXX"风格）。
- 涉及 `index.html` 界面 UI 设计的，按前端 skill 决策树调用 frontend-design / impeccable；纯逻辑改动（Rust data 层、JS legacy）按 ponytail 最小化。
- 收到用户需求后，先列出明确的执行计划，包括：需要修改的文件清单、每个文件的修改内容概述、将要采用的技术方案。执行计划列出后，必须等待用户确认后才能开始执行。

## 回答习惯
- 用中文回答。
- 结论先行，必要时再解释原因。
- 代码示例尽量完整但不要冗长。
- 如果需要修改文件，直接执行修改，并在最后简要说明改了什么和如何验证。
- 如果需求不明确，必须先向我确认，得到确认后再执行，不要自行假设关键需求。

## 代码改动收尾约束（必须遵守）
- **每次改动完代码并验证通过后，必须主动向用户确认：是否需要提交（git commit）？** 不要自行提交。
- **提交推送前先检查 README**：每次提交前，先看本次改动是否影响 README.md 描述的功能特性 / 配置 / 数据源 / 界面说明 / 目录结构 / 版本历史 / 技术栈，需要更新则**先改 README 再提交推送**（并入同一 commit 或前置一个 docs commit），不得跳过。
- 本项目改动需要重新构建才能生效，所以：
  - 用户确认要提交时，进一步确认是否需要先构建（`npm run tauri:build`）。
  - 提交内容通常包含构建产物说明（NSIS 安装器 `src-tauri/target/release/bundle/nsis/` 或便携 exe `src-tauri/target/release/zcode-usage-widget.exe`；`dist/` 与根目录 exe 副本按需更新，`/*.exe` 已 gitignore）。
- 本项目还涉及发布 release：用户确认提交后，进一步确认是否需要发布（打 tag `v<版本>` + `git push origin main` + `git push origin v<版本>` + `gh release create` 带 exe 附件，版本号需同步 `Cargo.toml` + `tauri.conf.json` 两处）。发布动作必须由用户明确授权，不要自行执行。
- 整条链路：改动 -> 验证 -> [确认] 构建 -> [确认] 提交 -> [确认] 发布 release。每一步都要等用户确认再往下走，不要合并确认。

## Skills 使用要求
- 当任务明显适合某个已安装 skill 时，主动调用对应 skill。
- 如果我明确说"使用某个 skill"，必须优先尝试调用该 skill。
- 每次回答结束时，都必须附上一行："本次对话调用的 skills：xxx"
- 如果没有调用任何 skill，也必须写："本次对话调用的 skills：无"
- 如果某个 skill 被请求但不可用，要明确说明："请求的 skill 不可用：xxx"

## 可用 Skills 列表
- `/frontend-design` - `index.html` 界面设计与美化
- `/impeccable` - UI 审查 / 优化 / 性能诊断（critique / audit / optimize）
- `/ponytail` - 代码最小化（Rust 数据层、Electron legacy 纯逻辑文件强制适用）
- `/browser-use` - 浏览器调试（前端渲染验证）
- `/caveman` - 简洁回复风格
