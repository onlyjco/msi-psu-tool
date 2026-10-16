"""Entry point for the windowed build.

When launched with no arguments this opens the GUI. When given a CLI command
it runs that instead -- but because a windowed (no-console) build has no
stdout, results are shown in a message box and can be copied out.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must run before Tk creates a window: without it Windows bitmap-stretches the
# finished window on a scaled display, which makes every glyph look soft.
import win_dpi

win_dpi.apply()


def _show_text(title: str, body: str) -> None:
    """Display text in a scrollable Tk window (no console available)."""
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title(title)
    root.configure(bg="#1e1f26")
    root.geometry("760x560")

    txt = scrolledtext.ScrolledText(root, wrap="word", font=("Consolas", 10),
                                    bg="#282a36", fg="#e6e6e6",
                                    insertbackground="#e6e6e6", bd=0,
                                    padx=12, pady=12)
    txt.pack(fill="both", expand=True, padx=10, pady=10)
    txt.insert("1.0", body)
    txt.configure(state="disabled")

    bar = tk.Frame(root, bg="#1e1f26")
    bar.pack(fill="x", padx=10, pady=(0, 10))

    def copy():
        root.clipboard_clear()
        root.clipboard_append(body)
        btn.config(text="Copied")

    btn = tk.Button(bar, text="Copy", font=("Segoe UI", 9), bg="#4a9eff",
                    fg="white", relief="flat", padx=14, cursor="hand2",
                    command=copy)
    btn.pack(side="left")
    tk.Button(bar, text="Close", font=("Segoe UI", 9), bg="#3a3d4d",
              fg="#e6e6e6", relief="flat", padx=14, cursor="hand2",
              command=root.destroy).pack(side="left", padx=8)
    root.mainloop()


def main() -> int:
    argv = sys.argv[1:]

    if not argv or argv[0] == "gui":
        from msi_psu_gui import App
        extra = argv[1:] if argv and argv[0] == "gui" else []
        pid = None
        if "--pid" in extra:
            try:
                pid = int(extra[extra.index("--pid") + 1], 0)
            except (IndexError, ValueError):
                pid = None
        App(pid=pid).mainloop()
        return 0

    # CLI command in a windowed build: capture output and show it.
    import contextlib
    import io

    import msi_psu

    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = msi_psu.main(argv)

    body = buf.getvalue()
    if err.getvalue():
        body += ("\n--- errors ---\n" if body else "") + err.getvalue()
    if not body.strip():
        body = f"(no output; exit code {rc})"
    # strip ANSI colour escapes -- they are meaningless in a Tk text widget
    import re
    body = re.sub(r"\x1b\[[0-9;]*m", "", body)

    _show_text(f"msi-psu {argv[0]}", body)
    return rc


if __name__ == "__main__":
    sys.exit(main())
