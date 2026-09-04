#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// ZCode 用量监控组件 — Tauri 2 主进程。
// 真磨砂方案对齐 Electron 版：窗口不透明 + tauri 内置 Acrylic 窗口效果
// （Windows 上走 WCA_ACCENT_POLICY，同 koffi recipe），页面透明像素显示
// 窗口背景的模糊。数据层在 data/，经 IPC 命令暴露给前端（window.zapi shim）。

mod commands;
mod data;
mod window;

use tauri::Manager;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 单实例锁：重复启动不叠窗，聚焦已有窗口（对齐 Electron requestSingleInstanceLock）
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
        }))
        .setup(|app| {
            // 后台刷新调度（火山/DeepSeek/opencode/wuhen/scnet，60s 周期，首次立即执行）
            data::scheduler::start_all(app.handle().clone());
            window::create_main(app.handle())?;
            window::start_anti_min_guard(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::status,
            commands::win_move,
            commands::win_drag_start,
            commands::win_resize,
            commands::win_get_pos,
            commands::win_set_opacity,
            commands::win_pin_top,
            commands::win_quit,
            commands::open_task,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
