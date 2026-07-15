"""Launch the ZCode usage desktop widget.

Creates a frameless, always-on-top pywebview window that floats on the
desktop and live-updates every 2s from ~/.zcode data.

Run:  python widget.py
"""

import ctypes
import os
import sys

import webview

from data import Api

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "icon.ico")


def _set_windows_taskbar_icon() -> None:
    """Pin a custom AppUserModelID + icon so the taskbar groups us under our
    own Z icon instead of the default python/py icon.

    On Windows, `pythonw.exe` hosts the process, so by default the taskbar
    shows Python's icon and groups the window under Python. Setting a unique
    AppUserModelID before the window is created makes the taskbar treat us as
    a standalone application. We also set the icon on the real HWND once the
    window is loaded (looked up by title via FindWindowW, since pywebview does
    not expose the hwnd directly).
    """
    if sys.platform != "win32":
        return
    app_id = "Oct1AtJoe.ZCodeUsageWidget"
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass

    def _apply_hwnd_icon(window) -> None:
        """Set WM_ICON on the real window handle so the taskbar and Alt-Tab
        thumbnail use icon.ico."""
        try:
            import win32gui
            import win32api
            import win32con
        except Exception:
            # pywin32 is optional; the AppUserModelID alone already fixes the
            # taskbar grouping in most cases.
            return
        try:
            if not os.path.exists(ICON_PATH):
                return
            hicon = win32gui.LoadImage(
                win32api.GetModuleHandle(None), ICON_PATH,
                win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
            hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
            if hwnd:
                win32api.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_BIG, hicon)
                win32api.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, hicon)
        except Exception:
            pass

    _set_windows_taskbar_icon._apply = _apply_hwnd_icon


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
        # Apply the Z icon to the real window handle (taskbar + Alt-Tab).
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
