"""Launch the ZCode usage desktop widget.

Creates a frameless, always-on-top pywebview window that floats on the
desktop and live-updates every 2s from ~/.zcode data.

Run:  python widget.py
"""

import os
import sys

import webview

from data import Api

HERE = os.path.dirname(os.path.abspath(__file__))


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
