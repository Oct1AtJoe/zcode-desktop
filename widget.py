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


def _set_windows_taskbar_icon() -> None:
    """Pin a custom AppUserModelID + icon so the taskbar groups us under our
    own Z icon instead of the default python/py icon.

    Two things must happen:

    1. AppUserModelID — set before the window is created so the taskbar treats
       us as a standalone application (not pythonw.exe). The ctypes argtypes
       must be declared, or LPCWSTR is not marshalled and the id ends up
       garbled.

    2. Form.Icon — pywebview's WinForms backend extracts the icon from
       `sys.executable` (python.exe) in BrowserForm.__init__ and assigns it to
       Form.Icon at the .NET level, which overrides any Win32 WM_SETICON we
       send. So we must overwrite Form.Icon ourselves via `window.native`
       (the underlying .NET Form exposed by pywebview).

    Timing matters: `webview.start(func=...)` runs func on a background thread
    *immediately*, before the window is created, so `window.native` is still
    None at that point. We wait for the `loaded` event first — after that the
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


_set_windows_taskbar_icon()



def main() -> None:
    api = Api()
    html_path = os.path.join(HERE, "widget.html")

    # NOTE: We deliberately do NOT pass js_api=api here. Passing the whole Api
    # object triggers pythonnet to marshal it to a System.Drawing.Rectangle
    # (a known pywebview 5.4 / pythonnet bug on WinForms), which throws
    # "No method matches given arguments for Rectangle.op_Equality" and breaks
    # the JS bridge. Instead we expose() individual functions below, which
    # avoids the object marshalling entirely.
    window = webview.create_window(
        title="ZCode 用量监控",
        url=html_path,
        width=322,
        height=835,
        x=1860,
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
