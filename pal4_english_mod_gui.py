#!/usr/bin/env python3
"""GUI installer for PAL4 English Mod."""

from __future__ import annotations

import io
import threading
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from pal4_english_mod_installer import (
    DISCLAIMER_TEXT,
    PATCH_VERSION,
    auto_find_game_paths,
    get_runtime_base_dir,
    install_patch,
    uninstall_patch,
    upgrade_patch_placeholder,
)


class ModGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"PAL4 English Mod Installer v{PATCH_VERSION}")
        self.root.geometry("980x700")
        self.root.minsize(860, 620)

        self.runtime_base_dir = get_runtime_base_dir()
        self.is_busy = False

        self.path_var = tk.StringVar()

        self._build_ui()
        self._autofill_game_path()

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

        path_row = tk.Frame(top_frame)
        path_row.pack(fill=tk.X)

        tk.Label(path_row, text="Game Folder:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Entry(path_row, textvariable=self.path_var, font=("Consolas", 10)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )

        tk.Button(path_row, text="Auto Detect", width=12, command=self._autofill_game_path).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(path_row, text="Browse", width=10, command=self._browse_path).pack(side=tk.LEFT)

        button_row = tk.Frame(top_frame)
        button_row.pack(fill=tk.X, pady=(10, 0))

        self.install_btn = tk.Button(button_row, text="Install Mod", width=16, command=lambda: self._run_action("install"))
        self.uninstall_btn = tk.Button(button_row, text="Uninstall Mod", width=16, command=lambda: self._run_action("uninstall"))
        self.upgrade_btn = tk.Button(button_row, text="Upgrade Mod", width=18, command=lambda: self._run_action("upgrade"))

        self.install_btn.pack(side=tk.LEFT)
        self.uninstall_btn.pack(side=tk.LEFT, padx=8)
        self.upgrade_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(top_frame, textvariable=self.status_var, anchor="w", fg="#0A4").pack(fill=tk.X, pady=(8, 0))

        middle = tk.PanedWindow(self.root, orient=tk.VERTICAL, sashrelief=tk.RAISED)
        middle.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        log_frame = tk.LabelFrame(middle, text="Operation Log", padx=8, pady=8)
        self.log_box = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 10), height=12)
        self.log_box.pack(fill=tk.BOTH, expand=True)
        middle.add(log_frame, stretch="always")

        disclaimer_frame = tk.LabelFrame(middle, text="Disclaimer", padx=8, pady=8)
        self.disclaimer_box = scrolledtext.ScrolledText(disclaimer_frame, wrap=tk.WORD, font=("Segoe UI", 10), height=10)
        self.disclaimer_box.insert(tk.END, DISCLAIMER_TEXT)
        self.disclaimer_box.configure(state=tk.DISABLED)
        self.disclaimer_box.pack(fill=tk.BOTH, expand=True)
        middle.add(disclaimer_frame, stretch="always")

    def _append_log(self, text: str) -> None:
        self.log_box.insert(tk.END, text.rstrip() + "\n")
        self.log_box.see(tk.END)

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.install_btn.configure(state=state)
        self.uninstall_btn.configure(state=state)
        self.upgrade_btn.configure(state=state)

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

    def _run_action(self, action: str) -> None:
        if self.is_busy:
            return

        game_dir = Path(self.path_var.get().strip().strip('"'))
        if not game_dir.exists() or not game_dir.is_dir():
            messagebox.showerror("Invalid Path", "Please select a valid game folder first.")
            return

        self._set_busy(True)
        self.status_var.set(f"Running: {action}")
        self._append_log(f"=== {action.upper()} START ===")
        self._append_log(f"Game path: {game_dir}")

        worker = threading.Thread(target=self._worker_action, args=(action, game_dir), daemon=True)
        worker.start()

    def _worker_action(self, action: str, game_dir: Path) -> None:
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                if action == "install":
                    code = install_patch(game_dir, self.runtime_base_dir)
                elif action == "uninstall":
                    code = uninstall_patch(game_dir)
                else:
                    code = upgrade_patch_placeholder(game_dir, self.runtime_base_dir)

            output = buffer.getvalue().strip()
            self.root.after(0, self._on_action_finished, action, code, output, None)
        except Exception:
            self.root.after(0, self._on_action_finished, action, 1, "", traceback.format_exc())

    def _on_action_finished(self, action: str, code: int, output: str, error_trace: str | None) -> None:
        if output:
            self._append_log(output)

        if error_trace:
            self._append_log("Unexpected error:")
            self._append_log(error_trace)
            messagebox.showerror("Error", "Installer encountered an unexpected error. See log for details.")
            self.status_var.set("Failed")
        elif code == 0:
            self._append_log(f"=== {action.upper()} DONE ===")
            self.status_var.set("Completed")
            messagebox.showinfo("Success", f"{action.capitalize()} completed successfully.")
        else:
            self._append_log(f"=== {action.upper()} FAILED ===")
            self.status_var.set("Failed")
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
