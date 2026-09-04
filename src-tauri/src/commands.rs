// IPC 命令 — 对齐 Electron preload window.zapi 接口（status/quit/getPos/
// moveWindow/resizeWindow/openTask/setOpacity；失焦折叠由前端 onFocusChanged
// 监听实现，无需主进程事件）。

use tauri::{AppHandle, PhysicalPosition, PhysicalSize, WebviewWindow};

use crate::window::{self, ICON_H, ICON_W};

#[tauri::command]
pub fn win_quit(app: AppHandle) {
    app.exit(0);
}

#[tauri::command]
pub fn win_get_pos(window: WebviewWindow) -> serde_json::Value {
    let sf = window::scale_factor(&window);
    let p = window.outer_position().unwrap_or(PhysicalPosition::new(0, 0));
    let s = window.outer_size().unwrap_or(PhysicalSize::new(0, 0));
    serde_json::json!({
        "x": (p.x as f64 / sf).round() as i64,
        "y": (p.y as f64 / sf).round() as i64,
        "w": (s.width as f64 / sf).round() as i64,
        "h": (s.height as f64 / sf).round() as i64,
    })
}

#[tauri::command]
pub fn win_move(window: WebviewWindow, x: f64, y: f64) {
    let sf = window::scale_factor(&window);
    let _ = window.set_position(PhysicalPosition::new((x * sf).round() as i32, (y * sf).round() as i32));
}

#[tauri::command]
pub fn win_drag_start(window: WebviewWindow) {
    // 原生高频拖拽线程：GetCursorPos + SetWindowPos 直接移动窗口（~500Hz）。
    // 为什么不用系统拖拽（startDragging）：transparent 窗口在 SC_MOVE 移动循环
    // 期间 WebView2 暂停合成 → 内容消失（实测）；SetWindowPos 逐帧移动不触发。
    // 为什么不用前端 JS 拖拽：pointermove 频率受 WebView2 渲染帧率限制（~60Hz）
    // + IPC 往返 → 明显不跟手（实测）。原生线程零 IPC、500Hz，跟手度接近系统拖拽。
    // 线程自检测左键松开退出；SetWindowPos 与 tao 状态经 WM_WINDOWPOSCHANGED 同步。
    use windows_sys::Win32::Foundation::{HWND, POINT, RECT};
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetCursorPos, GetWindowRect, SetWindowPos, SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOZORDER,
    };
    let hwnd = window.hwnd().unwrap_or_default().0 as HWND;
    let (base_x, base_y, start_x, start_y) = unsafe {
        let mut cur = POINT { x: 0, y: 0 };
        if GetCursorPos(&mut cur) == 0 {
            return;
        }
        let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
        if GetWindowRect(hwnd, &mut rect) == 0 {
            return;
        }
        (rect.left, rect.top, cur.x, cur.y)
    };
    // HWND 是裸指针不 Send，isize 传进线程再转回
    let hwnd = hwnd as isize;
    std::thread::spawn(move || {
        use windows_sys::Win32::Media::{timeBeginPeriod, timeEndPeriod};
        // 关键：Windows 默认 sleep 粒度 15.6ms（循环只有 ~60Hz，拖拽明显不跟手）。
        // timeBeginPeriod(1) 把计时精度提到 1ms → 循环 ~500Hz，跟手度接近系统拖拽。
        unsafe { timeBeginPeriod(1); }
        let hwnd = hwnd as HWND;
        let mut cur = POINT { x: 0, y: 0 };
        unsafe {
            loop {
                // 左键已松开 → 拖拽结束（i16 高位 = 按下，转 u16 避免符号位问题）
                if ((GetAsyncKeyState(VK_LBUTTON as i32) as u16) & 0x8000) == 0 {
                    break;
                }
                if GetCursorPos(&mut cur) == 0 {
                    break;
                }
                let nx = base_x + (cur.x - start_x);
                let ny = base_y + (cur.y - start_y);
                if SetWindowPos(hwnd, std::ptr::null_mut(), nx, ny, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE) == 0 {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
        }
        unsafe { timeEndPeriod(1); }
    });
}

#[tauri::command]
pub fn win_resize(window: WebviewWindow, w: f64, h: f64) {
    let sf = window::scale_factor(&window);
    let w = (w.max(ICON_W) * sf).round() as u32;
    let h = (h.max(ICON_H) * sf).round() as u32;
    let _ = window.set_size(PhysicalSize::new(w, h));
    // resize 后 DWM 圆角/边框可能恢复（Electron 版实测），重设
    window::apply_chrome(&window);
}

#[tauri::command]
pub fn win_pin_top(window: WebviewWindow) {
    // 折叠态强化置顶：SetWindowPos(HWND_TOPMOST) 把窗口重新插到 topmost 链顶，
    // 压过同链其它置顶窗口（tauri always_on_top 只保证在普通窗口之上，其它
    // 置顶窗口激活后仍会盖住本窗）。前端折叠态每 2s 调一次 → 始终最顶层。
    // 全屏独占窗口（游戏/全屏视频）绕过 DWM 合成，压不过——物理限制。
    // 防最小化不在此处：窗口最小化后 WebView2 暂停渲染、本命令无人再调，
    // 由 window.rs 的 native 守护线程负责恢复（Rust 侧不冻结）。
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        SetWindowPos, HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE,
    };
    unsafe {
        let hwnd = window.hwnd().unwrap_or_default().0;
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    }
}

#[tauri::command]
pub fn win_set_opacity(window: WebviewWindow, v: f64) {
    // tauri 2 无内置 set_opacity，手写 Win32 layered window alpha（对齐 Electron
    // win.setOpacity）。注意 WS_EX_LAYERED 与 DWM 圆角可能不兼容（已知坑）；
    // 当前前端未调用此命令，仅为接口完整性保留（对齐 preload window.zapi.setOpacity）。
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetWindowLongW, SetLayeredWindowAttributes, SetWindowLongW, GWL_EXSTYLE, LWA_ALPHA, WS_EX_LAYERED,
    };
    unsafe {
        let hwnd = window.hwnd().unwrap_or_default().0;
        let style = GetWindowLongW(hwnd, GWL_EXSTYLE);
        SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED as i32);
        SetLayeredWindowAttributes(hwnd, 0, (v.clamp(0.3, 1.0) * 255.0) as u8, LWA_ALPHA);
    }
}

#[tauri::command]
pub fn open_task(path: String) {
    // zcode:// 协议打开项目（对齐 Electron shell.openExternal）
    let url = format!("zcode://open-project?directory={}", urlencoding::encode(&path));
    let _ = std::process::Command::new("cmd").args(["/c", "start", "", &url]).spawn();
}

#[tauri::command]
pub async fn status() -> serde_json::Value {
    // 同步数据读取（SQLite 聚合等）放阻塞池，不占 tokio worker
    tauri::async_runtime::spawn_blocking(crate::data::status)
        .await
        .unwrap_or_else(|_| serde_json::json!({ "error": "status failed" }))
}
