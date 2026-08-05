"""Native OS folder-picker, used so path fields (e.g. the Unity project path) can be
selected rather than typed. Only meaningful because this app always runs locally next to
the user's own desktop — there is no browser API that exposes a real filesystem path."""
import tkinter as tk
from tkinter import filedialog


def pick_folder(initial: str | None = None, mustexist: bool = True) -> str | None:
    """Blocks until the user picks a folder or cancels. Returns None on cancel."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(initialdir=initial or None, mustexist=mustexist)
    finally:
        root.destroy()
    return path or None
