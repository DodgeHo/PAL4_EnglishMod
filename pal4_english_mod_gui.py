#!/usr/bin/env python3
"""GUI installer for PAL4 English Mod."""

from __future__ import annotations

import sys
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from pal4_english_mod_installer import (
    DISCLAIMER_TEXT,
    PATCH_VERSION,
    auto_find_game_paths,
    get_runtime_base_dir,
    install_patch,
    uninstall_patch,
    upgrade_patch_placeholder,
    set_progress_callback,
)


class _GuiWriter:
    """Real-time stdout redirect to tkinter widget."""

    def __init__(self, app: ModGuiApp) -> None:
        self.app = app
        self._buffer = ""

    def write(self, s: str) -> int:
        self._buffer += s
        if "\n" in self._buffer:
            lines = self._buffer.split("\n")
            self._buffer = lines.pop()
            for line in lines:
                if line.strip():
                    self.app.root.after(0, self.app._append_log, line.strip())
        return len(s)

    def flush(self) -> None:
        if self._buffer.strip():
            self.app.root.after(0, self.app._append_log, self._buffer.strip())
            self._buffer = ""


class ModGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"PAL4 English Mod Installer v{PATCH_VERSION}")
        self.root.geometry("980x720")
        self.root.minsize(860, 620)

        self.runtime_base_dir = get_runtime_base_dir()
        self.is_busy = False

        self.path_var = tk.StringVar()
        self.local_zip_path: str | None = None

        self._build_ui()
        self._autofill_game_path()

        # Wire global progress callback to UI.
        set_progress_callback(self._on_progress)

    def _build_ui(self) -> None:
        top_frame = tk.Frame(self.root, padx=12, pady=12)
        top_frame.pack(fill=tk.X)

        title_label = tk.Label(
            top_frame,
            text=f"Sword and Fairy 4 English Mod Installer v{PATCH_VERSION}",
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        )
        title_label.pack(fill=tk.X, pady=(0, 10))

        # --- Game folder row ---
        path_row = tk.Frame(top_frame)
        path_row.pack(fill=tk.X)

        tk.Label(path_row, text="Game Folder:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Entry(path_row, textvariable=self.path_var, font=("Consolas", 10)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )

        tk.Button(path_row, text="Auto Detect", width=12, command=self._autofill_game_path).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(path_row, text="Browse", width=10, command=self._browse_path).pack(side=tk.LEFT)

        # --- Patch zip row ---
        local_row = tk.Frame(top_frame)
        local_row.pack(fill=tk.X, pady=(8, 0))

        tk.Label(local_row, text="Local Zip (optional):", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.local_zip_var = tk.StringVar()
        tk.Entry(local_row, textvariable=self.local_zip_var, font=("Consolas", 9), state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )
        tk.Button(local_row, text="Select Zip", width=10, command=self._browse_zip).pack(side=tk.LEFT)
        tk.Button(local_row, text="Clear", width=8, command=self._clear_zip).pack(side=tk.LEFT, padx=(4, 0))

        # --- Buttons ---
        button_row = tk.Frame(top_frame)
        button_row.pack(fill=tk.X, pady=(10, 0))

        self.install_btn = tk.Button(button_row, text="Install Mod", width=16, command=lambda: self._run_action("install"))
        self.uninstall_btn = tk.Button(button_row, text="Uninstall Mod", width=16, command=lambda: self._run_action("uninstall"))
        self.upgrade_btn = tk.Button(button_row, text="Upgrade Mod", width=18, command=lambda: self._run_action("upgrade"))

        self.install_btn.pack(side=tk.LEFT)
        self.uninstall_btn.pack(side=tk.LEFT, padx=8)
        self.upgrade_btn.pack(side=tk.LEFT)

        # --- Progress bar ---
        progress_frame = tk.Frame(top_frame)
        progress_frame.pack(fill=tk.X, pady=(8, 0))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.progress_label = tk.Label(progress_frame, text="", font=("Segoe UI", 9), anchor="w", width=40)
        self.progress_label.pack(side=tk.LEFT, padx=(8, 0))

        # --- Status ---
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(top_frame, textvariable=self.status_var, anchor="w", fg="#0A4").pack(fill=tk.X, pady=(6, 0))

        # --- Middle panes ---
        middle = tk.PanedWindow(self.root, orient=tk.VERTICAL, sashrelief=tk.RAISED)
        middle.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        log_frame = tk.LabelFrame(middle, text="Operation Log", padx=8, pady=8)
        self.log_box = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 10), height=14)
        self.log_box.pack(fill=tk.BOTH, expand=True)
        middle.add(log_frame, stretch="always")

        disclaimer_frame = tk.LabelFrame(middle, text="Disclaimer", padx=8, pady=8)
        self.disclaimer_box = scrolledtext.ScrolledText(disclaimer_frame, wrap=tk.WORD, font=("Segoe UI", 10), height=8)
        self.disclaimer_box.insert(tk.END, DISCLAIMER_TEXT)
        self.disclaimer_box.configure(state=tk.DISABLED)
        self.disclaimer_box.pack(fill=tk.BOTH, expand=True)
        middle.add(disclaimer_frame, stretch="always")

    # ------------------------------------------------------------------
    #  Log / progress helpers
    # ------------------------------------------------------------------

    def _append_log(self, text: str) -> None:
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)

    def _on_progress(self, seen: int, total: int, message: str) -> None:
        """Called from worker threads via set_progress_callback."""
        if total > 0 and seen >= 0:
            pct = min(seen * 100 // total, 100)
        else:
            pct = 0  # indeterminate
        self.root.after(0, self._update_progress_ui, pct, message)

    def _update_progress_ui(self, pct: int, message: str) -> None:
        self.progress_var.set(pct)
        self.progress_label.configure(text=message)

    # ------------------------------------------------------------------
    #  Busy state
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.install_btn.configure(state=state)
        self.uninstall_btn.configure(state=state)
        self.upgrade_btn.configure(state=state)

    # ------------------------------------------------------------------
    #  Path selection
    # ------------------------------------------------------------------

    def _browse_path(self) -> None:
        folder = filedialog.askdirectory(title="Select Chinese Paladin 4 game folder")
        if folder:
            self.path_var.set(folder)

    def _autofill_game_path(self) -> None:
        candidates = auto_find_game_paths()
        if not candidates:
            self._append_log("Auto-detect did not find the game path. Please browse manually.")
            return
        self.path_var.set(str(candidates[0]))
        self._append_log(f"Detected game path: {candidates[0]}")

    def _browse_zip(self) -> None:
        path = filedialog.askopenfilename(
            title="Select patch zip file",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.local_zip_path = path
            self.local_zip_var.set(path)

    def _clear_zip(self) -> None:
        self.local_zip_path = None
        self.local_zip_var.set("")

    # ------------------------------------------------------------------
    #  Actions
    # ------------------------------------------------------------------

    def _run_action(self, action: str) -> None:
        if self.is_busy:
            return

        game_dir = Path(self.path_var.get().strip().strip('"'))
        if not game_dir.exists() or not game_dir.is_dir():
            messagebox.showerror("Invalid Path", "Please select a valid game folder first.")
            return

        if self.local_zip_path and not Path(self.local_zip_path).exists():
            messagebox.showerror("Invalid Zip", "The selected local zip file no longer exists.")
            return

        self._set_busy(True)
        self.status_var.set(f"Running: {action}")
        self.progress_var.set(0)
        self.progress_label.configure(text="Starting...")
        self._append_log(f"=== {action.upper()} START ===")
        self._append_log(f"Game path: {game_dir}")
        if self.local_zip_path:
            self._append_log(f"Local zip: {self.local_zip_path}")

        worker = threading.Thread(
            target=self._worker_action, args=(action, game_dir),
            daemon=True,
        )
        worker.start()

    def _worker_action(self, action: str, game_dir: Path) -> None:
        try:
            gui_writer = _GuiWriter(self)
            old_stdout = sys.stdout
            sys.stdout = gui_writer  # type: ignore[assignment]

            try:
                if action == "install":
                    code = install_patch(
                        game_dir, self.runtime_base_dir,
                        local_zip_path=self.local_zip_path,
                    )
                elif action == "uninstall":
                    code = uninstall_patch(game_dir)
                else:
                    code = upgrade_patch_placeholder(game_dir, self.runtime_base_dir)
            finally:
                sys.stdout = old_stdout
                gui_writer.flush()

            self.root.after(0, self._on_action_finished, action, code, None)
        except Exception:
            self.root.after(0, self._on_action_finished, action, 1, traceback.format_exc())

    def _on_action_finished(self, action: str, code: int, error_trace: str | None) -> None:
        self.progress_var.set(100 if code == 0 else 0)

        if error_trace:
            self._append_log("Unexpected error:")
            self._append_log(error_trace)
            messagebox.showerror("Error", "Installer encountered an unexpected error. See log for details.")
            self.status_var.set("Failed")
            self.progress_label.configure(text="Error")
        elif code == 0:
            self._append_log(f"=== {action.upper()} DONE ===")
            self.status_var.set("Completed")
            self.progress_label.configure(text="Done!")
            messagebox.showinfo("Success", f"{action.capitalize()} completed successfully.")
        else:
            self._append_log(f"=== {action.upper()} FAILED ===")
            self.status_var.set("Failed")
            self.progress_label.configure(text="Failed")
            messagebox.showwarning("Operation Failed", f"{action.capitalize()} failed. See log for details.")

        self._set_busy(False)


def main() -> int:
    try:
        root = tk.Tk()
        app = ModGuiApp(root)
        _ = app
        root.mainloop()
        return 0
    except Exception:
        trace = traceback.format_exc()
        log_path = Path(tempfile.gettempdir()) / "PAL4_EnglishMod_Installer_error.log"
        log_path.write_text(
            f"[{datetime.now().isoformat()}]\n{trace}\n",
            encoding="utf-8",
            errors="ignore",
        )
        try:
            messagebox.showerror(
                "Installer Error",
                "The installer crashed unexpectedly.\n"
                f"Error log: {log_path}",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
