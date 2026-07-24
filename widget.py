"""Launch the ZCode usage desktop widget.

Creates a frameless, always-on-top pywebview window that floats on the
desktop and live-updates every 2s from ~/.zcode data.

Run:  python widget.py
"""

import ctypes
import os
import sys
from ctypes import wintypes

import webview

from data import Api

# Resolve the resource directory. When frozen with PyInstaller (--onefile), the
# bundled files are extracted to sys._MEIPASS at runtime; otherwise they sit
# next to this script.
if getattr(sys, "frozen", False):
    HERE = sys._MEIPASS  # type: ignore[attr-defined]
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "icon.ico")

# Declare the Shell32 function signatures so ctypes marshals strings correctly.
# Without argtypes, LPCWSTR is not marshalled properly and the AppUserModelID
# ends up as garbled bytes -- which makes the taskbar fall back to grouping the
# window under pythonw.exe (showing the Python icon instead of ours).
_shell32 = ctypes.windll.shell32
_shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
_shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long  # HRESULT

# ---- Windows Acrylic 毛玻璃 ----
# 两条路线,优先现代 DWM API(Win11 22000+),失败回退 Accent recipe:
#
# 路线A(现代):DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE=DWMSBT_TRANSIENTWINDOW)
#   Win11 原生 Acrylic,不依赖色键透明,鼠标命中正常。要求窗口客户区透明才能透出模糊。
#
# 路线B(Accent,移植自 token-monitor windowsBackdrop.js):
#   SetWindowCompositionAttribute(ACCENT_ENABLE_ACRYLICBLURBEHIND) + DwmEnableBlurBehindWindow。
#   兼容老 Win10,但需配合窗口透明。
#
# 关键约束:pywebview transparent=True 会设 TransparencyKey 色键透明,把等于该色的像素
# 打穿成鼠标穿透区 -- 与 Acrylic 模糊后的半透深灰冲突,导致整个窗口点不上。故本实现
# 不用 pywebview transparent,而是加载后手动把 WebView2 控件 + .NET Form 背景设透明
# (不设 TransparencyKey),让 DWM Acrylic 透出且保留鼠标命中。
ACRYLIC_ACCENT_ARGB = 0x3A232323
WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
DWM_BB_ENABLE = 0x1
DWM_BB_BLURREGION = 0x2
DWM_BB_TRANSITIONONMAXIMIZED = 0x4
# DWMWA 现代属性(Win11 22000+)
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_WINDOW_CORNER_PREFERENCE = 33  # Win11 22H2+ 窗口圆角偏好
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2  # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic( transient/window)
DWMSBT_TABBEDWINDOW = 4


class _ACCENT_POLICY(ctypes.Structure):
    """user32!SetWindowCompositionAttribute 的 Accent 参数结构。"""

    _fields_ = [
        ("AccentState", ctypes.c_int32),
        ("AccentFlags", ctypes.c_int32),
        ("GradientColor", ctypes.c_uint32),
        ("AnimationId", ctypes.c_int32),
    ]


class _WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attrib", ctypes.c_uint32),
        ("pvData", ctypes.c_void_p),
        ("cbData", ctypes.c_size_t),
    ]


class _DWM_BLURBEHIND(ctypes.Structure):
    """dwmapi!DwmEnableBlurBehindWindow 的参数结构。"""

    _fields_ = [
        ("dwFlags", ctypes.c_uint32),
        ("fEnable", ctypes.c_int32),
        ("hRgnBlur", ctypes.c_void_p),
        ("fTransitionOnMaximized", ctypes.c_int32),
    ]


class _MARGINS(ctypes.Structure):
    """dwmapi!DwmExtendFrameIntoClientArea 的参数结构。全 -1 = 扩展到整个客户区。"""

    _fields_ = [
        ("cxLeftWidth", ctypes.c_int32),
        ("cxRightWidth", ctypes.c_int32),
        ("cyTopHeight", ctypes.c_int32),
        ("cyBottomHeight", ctypes.c_int32),
    ]


def _dwm_set_attr(hwnd: int, attr: int, value: int) -> bool:
    """dwmapi!DwmSetWindowAttribute 包装,设一个 int 属性。best-effort。"""
    try:
        dwmapi = ctypes.windll.dwmapi
        v = ctypes.c_int(value)
        return dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(attr), ctypes.byref(v), ctypes.c_uint(ctypes.sizeof(v))
        ) == 0
    except Exception:
        return False


def _apply_acrylic_modern(hwnd: int) -> bool:
    """路线A:Win11 现代 DWM Acrylic。

    1. DWMWA_USE_IMMERSIVE_DARK_MODE=1(深色窗口,匹配深色玻璃)
    2. DWMWA_SYSTEMBACKDROP_TYPE=DWMSBT_TRANSIENTWINDOW(Acrylic 背景)
    返回是否调用成功(不代表视觉一定生效,受系统版本/主题影响)。
    """
    if sys.platform != "win32" or not hwnd:
        return False
    ok_dark = _dwm_set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    ok_backdrop = _dwm_set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)
    return ok_dark and ok_backdrop


def _apply_acrylic_accent(hwnd: int) -> bool:
    """路线B(fallback):Accent recipe 模糊。

    顺序复刻 token-monitor windowsBackdrop.js:
    1. CreateRectRgn(0,0,-1,-1) 建全屏区域
    2. DwmEnableBlurBehindWindow 开启 DWM 模糊并绑定区域
    3. DwmExtendFrameIntoClientArea(-1×4) 扩展玻璃框到整个客户区
    4. SetWindowCompositionAttribute(ACCENT_ACRYLICBLURBEHIND, ARGB) 设混合色
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        gdi32 = ctypes.windll.gdi32

        region = gdi32.CreateRectRgn(0, 0, -1, -1)
        if not region:
            return False
        try:
            blur = _DWM_BLURBEHIND(
                dwFlags=DWM_BB_ENABLE | DWM_BB_BLURREGION | DWM_BB_TRANSITIONONMAXIMIZED,
                fEnable=1,
                hRgnBlur=region,
                fTransitionOnMaximized=1,
            )
            if dwmapi.DwmEnableBlurBehindWindow(ctypes.c_void_p(hwnd), ctypes.byref(blur)) < 0:
                return False

            margins = _MARGINS(cxLeftWidth=-1, cxRightWidth=-1, cyTopHeight=-1, cyBottomHeight=-1)
            if dwmapi.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd), ctypes.byref(margins)) < 0:
                return False

            accent = _ACCENT_POLICY(
                AccentState=ACCENT_ENABLE_ACRYLICBLURBEHIND,
                AccentFlags=0,
                GradientColor=ACRYLIC_ACCENT_ARGB,
                AnimationId=0,
            )
            data = _WINDOWCOMPOSITIONATTRIBDATA(
                Attrib=WCA_ACCENT_POLICY,
                pvData=ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                cbData=ctypes.sizeof(accent),
            )
            return bool(user32.SetWindowCompositionAttribute(ctypes.c_void_p(hwnd), ctypes.byref(data)))
        finally:
            gdi32.DeleteObject(region)
    except Exception:
        return False


def _set_windows_taskbar_icon() -> None:
    """Pin a custom AppUserModelID + icon so the taskbar groups us under our
    own Z icon instead of the default python/py icon.

    Two things must happen:

    1. AppUserModelID - set before the window is created so the taskbar treats
       us as a standalone application (not pythonw.exe). The ctypes argtypes
       must be declared, or LPCWSTR is not marshalled and the id ends up
       garbled.

    2. Form.Icon - pywebview's WinForms backend extracts the icon from
       `sys.executable` (python.exe) in BrowserForm.__init__ and assigns it to
       Form.Icon at the .NET level, which overrides any Win32 WM_SETICON we
       send. So we must overwrite Form.Icon ourselves via `window.native`
       (the underlying .NET Form exposed by pywebview).

    Timing matters: `webview.start(func=...)` runs func on a background thread
    *immediately*, before the window is created, so `window.native` is still
    None at that point. We wait for the `loaded` event first - after that the
    BrowserForm exists and `window.native` is populated.

    WinForms controls are thread-affined, so the Icon assignment is marshaled
    onto the UI thread via Form.Invoke(Action).
    """
    if sys.platform != "win32":
        return
    app_id = "Oct1AtJoe.ZCodeUsageWidget"
    try:
        _shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass

    def _apply_form_icon(window) -> None:
        try:
            if not os.path.exists(ICON_PATH):
                return
            import clr  # noqa: F401  (pythonnet, already loaded by pywebview)
            from System.Drawing import Icon
            from System import Action

            native = getattr(window, "native", None)
            if native is None:
                return

            def _set_icon():
                native.Icon = Icon(ICON_PATH)

            native.Invoke(Action(_set_icon))
        except Exception:
            pass

    _set_windows_taskbar_icon._apply = _apply_form_icon


def _set_rounded_region(hwnd: int, w: int, h: int, radius: int = 14) -> bool:
    """用 Win32 SetWindowRgn 给窗口设圆角矩形区域,系统级裁切窗口形状。

    透明窗口下 CSS border-radius 裁不掉 WebView2 客户区的方形角,四角会露出
    黑边。SetWindowRgn 在系统层把窗口(含子控件)裁成圆角矩形,彻底消除方角。
    每次窗口尺寸变化(resize)后必须重设,否则区域与窗口不匹配。

    NOTE: SetWindowRgn 不支持抗锯齿,圆角边缘有 1px 锯齿。Win11 优先用
    _dwm_set_attr(DWMWA_WINDOW_CORNER_PREFERENCE=DWMWCP_ROUND) 走系统原生圆角
    (带 AA),本函数仅作老系统 fallback。
    """
    if sys.platform != "win32" or not hwnd or w <= 0 or h <= 0:
        return False
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
        if not rgn:
            return False
        # SetWindowRgn 接管区域所有权,系统负责释放,无需 DeleteObject
        return user32.SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(rgn), True) != 0
    except Exception:
        return False


def _enable_acrylic_on_form(window) -> None:
    """在已加载的窗口上启用系统 Acrylic 毛玻璃。

    步骤(全在 UI 线程,因 .NET 控件线程亲和):
    1. 手动把 WebView2 控件背景设透明(DefaultBackgroundColor=Color.Transparent)
       + Form 开 SupportsTransparentBackColor,让窗口客户区透明以透出 DWM 模糊。
       关键:不设 Form.TransparencyKey(那是色键透明,会打穿鼠标命中区)。
    2. 优先路线A:DwmSetWindowAttribute(SYSTEMBACKDROP_TYPE=Acrylic)现代 API
    3. 回退路线B:Accent recipe(SetWindowCompositionAttribute)
    任一步失败静默降级,不影响窗口正常显示与交互。
    """
    if sys.platform != "win32":
        return
    try:
        import clr  # noqa: F401
        from System import Action
        from System.Drawing import Color
        import System.Windows.Forms as WinForms

        native = getattr(window, "native", None)
        if native is None:
            return

        def _do_acrylic():
            hwnd = int(native.Handle.ToInt32())
            # 1. WebView2 控件 + Form 背景透明(不设 TransparencyKey,保留鼠标命中)
            try:
                native.SetStyle(WinForms.ControlStyles.SupportsTransparentBackColor, True)
                native.BackColor = Color.Transparent
                browser = getattr(native, "browser", None)
                if browser is not None:
                    browser.DefaultBackgroundColor = Color.Transparent
            except Exception:
                pass
            # 2. 现代路线A;失败回退路线B
            if not _apply_acrylic_modern(hwnd):
                _apply_acrylic_accent(hwnd)
            # 3. 系统级圆角:透明窗口下 CSS border-radius 裁不掉 WebView2 方形角。
            #    优先 DWMWA_WINDOW_CORNER_PREFERENCE=ROUND(Win11 原生圆角,带抗锯齿,
            #    pywebview frameless 下可能被设成 DONOTROUND=1,这里强制 ROUND=2);
            #    失败回退 SetWindowRgn(老系统,有锯齿但能裁方角)。
            if not _dwm_set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND):
                def _apply_region(*_):
                    try:
                        _set_rounded_region(hwnd, native.Width, native.Height, 14)
                    except Exception:
                        pass
                try:
                    _apply_region()
                    native.SizeChanged += WinForms.EventHandler(_apply_region)
                except Exception:
                    pass

        native.Invoke(Action(_do_acrylic))
    except Exception:
        pass


_set_windows_taskbar_icon()



def main() -> None:
    api = Api()
    # 启动火山用量后台刷新线程：周期性调用火山 OpenAPI 刷新 _VOLC_CACHE，
    # 让 status() 调用路径上不再有任何网络请求。网络栈休眠时 requests.post
    # 阻塞只会卡后台 daemon 线程，不会拖死 status()，从而根治长时间空闲后
    # 组件卡在"重连中"的问题。线程幂等启动，daemon=True 随进程退出。
    from data import start_volc_refresher
    start_volc_refresher()

    html_path = os.path.join(HERE, "widget.html")

    # NOTE: We deliberately do NOT pass js_api=api here. Passing the whole Api
    # object triggers pythonnet to marshal it to a System.Drawing.Rectangle
    # (a known pywebview 5.4 / pythonnet bug on WinForms), which throws
    # "No method matches given arguments for Rectangle.op_Equality" and breaks
    # the JS bridge. Instead we expose() individual functions below, which
    # avoids the object marshalling entirely.
    #
    # 不用 pywebview transparent=True:它走 TransparencyKey 色键透明,会把等于
    # 该色的像素打穿成鼠标穿透区,与 Acrylic 模糊后半透深灰冲突导致整个窗口
    # 点不上/拖不动。改在 _enable_acrylic_on_form 里手动设 WebView2 控件透明
    # (不设 TransparencyKey),保留鼠标命中。background_color 仅作加载前底色。
    window = webview.create_window(
        title="ZCode 用量监控",
        url=html_path,
        width=322,
        height=840,
        x=1260,
        y=80,
        resizable=False,
        frameless=True,
        easy_drag=False,
        on_top=True,
        background_color="#0f1014",
        text_select=False,
        min_size=(1, 1),
    )

    # Bind the window so api.quit()/getPos()/moveWindow() can control it.
    api.window = window

    # Expose bridge functions individually (avoids the Rectangle marshalling
    # bug). These become callable as window.pywebview.api.<name>() in JS.
    window.expose(api.status, api.quit, api.getPos, api.moveWindow, api.resizeWindow, api.openTask)

    def _on_loaded():
        # webview.start(func=...) runs this on a background thread immediately,
        # before the window is created -- so window.native is still None. We
        # must wait for the `loaded` event first: after it the BrowserForm
        # exists and window.native (the .NET Form) is populated.
        window.events.loaded.wait(15)
        # Apply the Z icon to the .NET Form (taskbar + Alt-Tab).
        apply = getattr(_set_windows_taskbar_icon, "_apply", None)
        if apply:
            apply(window)
        # 启用系统 Acrylic 毛玻璃模糊桌面背景。.NET Form.Handle 访问受线程
        # 亲和约束,须在 UI 线程取 HWND 后调用。失败静默降级到 CSS backdrop-filter。
        _enable_acrylic_on_form(window)
        # Inject custom dragging logic (easy_drag has a bug in pywebview 5.4).
        # Uses getPos()/moveWindow() bound above.
        window.evaluate_js(
            """
            (function(){
              if(window.__dragBound) return; window.__dragBound=true;
              var dragging=false, moved=false, ox=0, oy=0, wx=0, wy=0;
              var hdr=document.getElementById('dragHandle');
              if(!hdr) return;
              hdr.addEventListener('mousedown', function(e){
                if(e.target.classList.contains('close-btn')) return;
                if(e.target.closest('.collapse-btn')) return;
                dragging=true; moved=false;
                ox=e.screenX; oy=e.screenY;
                window.pywebview.api.getPos().then(function(p){ wx=p.x; wy=p.y; });
                e.preventDefault();
              });
              document.addEventListener('mousemove', function(e){
                if(!dragging) return;
                var dx=e.screenX-ox, dy=e.screenY-oy;
                if(Math.abs(dx)>3 || Math.abs(dy)>3) moved=true;
                window.pywebview.api.moveWindow(wx+dx, wy+dy);
              });
              document.addEventListener('mouseup', function(e){
                dragging=false;
                // If the logo was pressed without moving (a click, not a drag),
                // toggle the widget: collapsed -> expand, expanded -> collapse.
                if(!moved){
                  var w=document.getElementById('widget');
                  if(w && e.target && e.target.classList && e.target.classList.contains('logo')){
                    if(window.__toggleExpand) window.__toggleExpand(!w.classList.contains('collapsed'));
                  }
                }
              });
            })();
            """
        )

    webview.start(func=_on_loaded, debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
