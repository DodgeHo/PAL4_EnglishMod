#!/usr/bin/env python3
"""Downloader-installer for Sword and Fairy 4 English Mod.

Features:
- Downloads patch from GitHub Releases on demand (small exe, no bundled data).
- Supports HTTP Range resume for interrupted downloads.
- Install patch with mandatory backup of original files.
- Uninstall patch and restore backups.
- Upgrade entry point placeholder for future versions.
- Auto-detect Steam game path; prompt user when detection fails.
- Leave a quick uninstall batch script in game directory.
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


PATCH_VERSION = "1.1a"
GAME_FOLDER_NAME = "Chinese Paladin 4"
MOD_STATE_DIR_NAME = ".pal4_englishmod"
BACKUP_DIR_NAME = ".pal4_englishmod_backup"
MANIFEST_FILE_NAME = "manifest.json"
QUICK_UNINSTALL_BAT_NAME = "Uninstall PAL4 English Mod.bat"

DOWNLOAD_URL = (
    f"https://github.com/DodgeHo/PAL4_EnglishMod/releases/download/"
    f"v1.1/PAL4.EnglishMod.Patch.{PATCH_VERSION}.zip"
)

# Minimum required files used to validate a patch source is present and intact.
REQUIRED_PATCH_FILES = [
    Path("Config.exe"),
    Path("OIRAMLOOK.dll"),
    Path("gamedata/database.cpk"),
    Path("gamedata/script.cpk"),
    Path("gamedata/ui.cpk"),
    Path("gamedata/Music/p61.smp"),
    Path("gamedata/PALSound/HSOff.mp3"),
    Path("gamedata/PALSound/HSOn.mp3"),
]


_TEMP_PATCH_DIRS: list[Path] = []

# Global progress callback: (seen_bytes, total_bytes, message) -> None
_progress_callback: Callable[[int, int, str], None] | None = None


def set_progress_callback(cb: Callable[[int, int, str], None] | None) -> None:
    """Set a callback for progress updates during download/install."""
    global _progress_callback
    _progress_callback = cb


def _emit_progress(seen: int, total: int, message: str) -> None:
    if _progress_callback:
        _progress_callback(seen, total, message)


DISCLAIMER_TEXT = """
==================== DISCLAIMER ====================
This fan project is non-profit and made for passion.
This project is developed for the Steam version of Sword and Fairy 4
(Chinese Paladin 4) only.

Audience
- This guide is for non-technical users.
- Steam version only: Sword and Fairy 4 (Chinese Paladin 4).

What this folder is
- This folder is a manual patch package.
- Copy all files in this folder into your game folder.

How to find your game folder in Steam
1. Open Steam Library.
2. Right-click "Sword and Fairy 4" (Chinese Paladin 4).
3. Click "Properties".
4. Click "Installed Files".
5. Click "Browse".
6. A folder window opens. This is your game folder.

Install (manual copy)
1. Close the game and Steam.
2. Open this 1.1a/1.1b patch folder.
3. Select ALL files and folders here (for example: Config.exe, OIRAMLOOK.dll, launch.exe, gamedata).
4. Copy them into the game folder you opened from Steam.
5. When Windows asks, choose "Replace files in destination".
6. Start the game from Steam.

Uninstall
- Steam Library -> Right-click game -> Properties -> Installed Files -> Verify integrity of game files.
- Steam will restore original files automatically.

Support / Updates
I am DodgeHo, a long-time fan of the series.
- GitHub: https://github.com/DodgeHo/PAL4_EnglishMod
- Email: asdsay@foxmail.com
- Discord: daoanlandodge
- QQ Group: 1064586214

====================================================
""".strip()


def print_disclaimer() -> None:
    print(DISCLAIMER_TEXT)
    print()


# ---------------------------------------------------------------------------
#  Download with resume
# ---------------------------------------------------------------------------

def _get_remote_file_size(url: str) -> int | None:
    """HEAD request to get Content-Length in bytes, or None if unsupported."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as resp:
            length = resp.headers.get("Content-Length")
            if length is not None and length.isdigit():
                return int(length)
    except Exception:
        pass
    return None


def _download_patch_zip(
    dest_dir: Path,
    resume: bool = True,
    max_retries: int = 3,
) -> Path | None:
    """Download patch zip from GitHub Releases with resume + retry support.

    Returns the path to the downloaded zip, or None on failure.
    Prints progress to stdout and calls the global progress callback.
    """
    zip_path = dest_dir / f"PAL4.EnglishMod.Patch.{PATCH_VERSION}.zip"

    remote_size = _get_remote_file_size(DOWNLOAD_URL)
    total = remote_size

    if resume and zip_path.exists():
        existing_size = zip_path.stat().st_size
        if remote_size and existing_size >= remote_size:
            if existing_size == remote_size:
                print(f"  Patch zip already downloaded ({existing_size / (1024*1024):.1f} MB).")
                _emit_progress(existing_size, existing_size, "Already downloaded.")
                return zip_path
            print("  Existing file larger than expected, re-downloading...")
            zip_path.unlink()

    print(f"Downloading {PATCH_VERSION} patch from GitHub Releases...")
    print(f"  URL: {DOWNLOAD_URL}")
    if remote_size:
        print(f"  Size: {remote_size / (1024*1024):.1f} MB")

    attempt = 0
    while attempt < max_retries:
        attempt += 1

        existing_size = zip_path.stat().st_size if resume and zip_path.exists() else 0
        if existing_size > 0 and remote_size and existing_size >= remote_size:
            print(f"  Patch zip already downloaded ({existing_size / (1024*1024):.1f} MB).")
            _emit_progress(existing_size, existing_size, "Already downloaded.")
            return zip_path

        mode = "ab" if existing_size > 0 else "wb"
        if existing_size > 0:
            print(f"  Resuming from {existing_size / (1024*1024):.1f} MB..."
                  f"{' (retry ' + str(attempt) + ')' if attempt > 1 else ''}")

        try:
            request = urllib.request.Request(DOWNLOAD_URL)
            if existing_size > 0:
                request.add_header("Range", f"bytes={existing_size}-")

            # No hard timeout — let the TCP stack handle it via keep-alive.
            # timeout=None means the socket blocks indefinitely until data
            # arrives or the server closes the connection.
            with urllib.request.urlopen(request, timeout=600) as response:
                if response.status == 206:
                    cr = response.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)", cr)
                    if m:
                        total = int(m[1])
                elif response.status == 200 and existing_size > 0:
                    print("  Server does not support resume, starting fresh...")
                    existing_size = 0
                    mode = "wb"

                seen = existing_size
                with open(zip_path, mode) as f:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        seen += len(chunk)

                        if total:
                            mb_seen = seen / (1024 * 1024)
                            mb_total = total / (1024 * 1024)
                            pct = seen * 100 // total
                            msg = f"Downloading... {pct}%  ({mb_seen:.1f} / {mb_total:.1f} MB)"
                        else:
                            msg = f"Downloading... {seen / (1024*1024):.1f} MB"

                        if not total or seen % max(1, total // 100) < 256 * 1024:
                            print(f"  {msg}")

                        _emit_progress(seen, total or 0, msg)

                # Completed successfully.
                mb = seen / (1024 * 1024)
                print(f"  Download complete. ({mb:.1f} MB)")
                _emit_progress(seen, seen, "Download complete.")
                return zip_path

        except (urllib.error.HTTPError, OSError, ConnectionError, TimeoutError) as exc:
            saved = zip_path.stat().st_size if zip_path.exists() else 0
            if attempt < max_retries and saved > 0:
                print(f"  Connection lost at {saved / (1024*1024):.1f} MB: {exc}")
                print(f"  Will retry in 3 seconds (attempt {attempt}/{max_retries})...")
                import time as _time
                _time.sleep(3)
                continue
            print(f"  Download failed: {exc}")
            _emit_progress(saved, total or 0, f"Download failed: {exc}")
            return None

    return None


# ---------------------------------------------------------------------------
#  Registry & Steam helpers
# ---------------------------------------------------------------------------

def _load_winreg():
    try:
        import winreg  # type: ignore
        return winreg
    except Exception:
        return None


def get_steam_roots_from_registry() -> list[Path]:
    winreg = _load_winreg()
    if winreg is None:
        return []

    roots: list[Path] = []
    key_specs = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]

    for hive, subkey, value_name in key_specs:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                raw_value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue

        if not isinstance(raw_value, str):
            continue

        candidate = Path(raw_value)
        if candidate.suffix.lower() == ".exe":
            candidate = candidate.parent
        if candidate.exists():
            roots.append(candidate)

    return _dedupe_paths(roots)


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen = set()
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def parse_libraryfolders_vdf(vdf_path: Path) -> list[Path]:
    if not vdf_path.exists():
        return []

    text = vdf_path.read_text(encoding="utf-8", errors="ignore")
    matches = []
    matches.extend(re.findall(r'"\d+"\s+"([^"]+)"', text))
    matches.extend(re.findall(r'"path"\s+"([^"]+)"', text))

    paths = []
    for m in matches:
        normalized = m.replace("\\\\", "\\")
        p = Path(normalized)
        if p.exists():
            paths.append(p)
    return _dedupe_paths(paths)


def auto_find_game_paths() -> list[Path]:
    steam_roots = get_steam_roots_from_registry()
    library_roots: list[Path] = []

    for steam_root in steam_roots:
        library_roots.append(steam_root)
        vdf = steam_root / "steamapps" / "libraryfolders.vdf"
        library_roots.extend(parse_libraryfolders_vdf(vdf))

    game_candidates: list[Path] = []
    for lib in _dedupe_paths(library_roots):
        candidate = lib / "steamapps" / "common" / GAME_FOLDER_NAME
        if candidate.exists() and candidate.is_dir():
            game_candidates.append(candidate)

    return _dedupe_paths(game_candidates)


def ask_user_for_game_path(found_paths: list[Path]) -> Path | None:
    if found_paths:
        print("Detected possible game path(s):")
        for idx, path in enumerate(found_paths, start=1):
            print(f"  {idx}. {path}")
        print("  M. Enter path manually")
        choice = input("Select a path (number/M): ").strip().lower()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(found_paths):
                return found_paths[idx - 1]
        if choice not in {"m", "manual", ""}:
            print("Invalid choice.")
            return None

    manual = input("Enter your Chinese Paladin 4 game folder path: ").strip().strip('"')
    if not manual:
        return None
    p = Path(manual)
    if p.exists() and p.is_dir():
        return p
    print("The path does not exist or is not a folder.")
    return None


def find_game_path(cli_game_path: str | None) -> Path | None:
    if cli_game_path:
        p = Path(cli_game_path).expanduser()
        return p if p.exists() and p.is_dir() else None
    return ask_user_for_game_path(auto_find_game_paths())


# ---------------------------------------------------------------------------
#  Patch source discovery
# ---------------------------------------------------------------------------

def get_runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def get_patch_source_candidates(runtime_base_dir: Path) -> list[Path]:
    candidates = [runtime_base_dir / PATCH_VERSION]

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / PATCH_VERSION)

    candidates.append(Path.cwd() / PATCH_VERSION)
    candidates.append(runtime_base_dir / "_internal" / PATCH_VERSION)

    return _dedupe_paths(candidates)


def get_all_patch_files(patch_source: Path) -> list[Path]:
    """Enumerate all patchable files under *patch_source* recursively.

    Returns paths relative to *patch_source*, sorted for determinism.
    """
    result: list[Path] = []
    for p in sorted(patch_source.rglob("*")):
        if p.is_file():
            result.append(p.relative_to(patch_source))
    return result


def validate_patch_source(patch_source: Path) -> tuple[bool, list[Path]]:
    missing = [rel for rel in REQUIRED_PATCH_FILES if not (patch_source / rel).exists()]
    return len(missing) == 0, missing


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _cleanup_temp_patch_dirs() -> None:
    for path in _TEMP_PATCH_DIRS:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


atexit.register(_cleanup_temp_patch_dirs)


def _get_zip_source_candidates(runtime_base_dir: Path) -> list[Path]:
    roots = [runtime_base_dir, Path.cwd()]
    if getattr(sys, "frozen", False):
        roots.insert(1, Path(sys.executable).resolve().parent)

    zip_names = [
        f"{PATCH_VERSION}.zip",
        f"PAL4_EnglishMod_{PATCH_VERSION}.zip",
        f"PAL4.EnglishMod.Patch.{PATCH_VERSION}.zip",
        "patch.zip",
    ]

    candidates: list[Path] = []
    for root in roots:
        for name in zip_names:
            candidates.append(root / name)
    return _dedupe_paths(candidates)


def _extract_patch_zip(zip_path: Path) -> Path | None:
    if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
        return None

    _emit_progress(0, 1, f"Extracting {zip_path.name}...")
    temp_root = Path(tempfile.mkdtemp(prefix=f"pal4_patch_{PATCH_VERSION}_"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_root)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        return None

    for candidate in [temp_root / PATCH_VERSION, temp_root]:
        ok, _ = validate_patch_source(candidate)
        if ok:
            _TEMP_PATCH_DIRS.append(temp_root)
            return candidate

    shutil.rmtree(temp_root, ignore_errors=True)
    return None


def get_patch_source_dir(
    runtime_base_dir: Path,
    local_zip_path: str | None = None,
) -> Path | None:
    """Resolve patch source. Order:
    1. Explicit --patch-zip argument.
    2. Extracted folder next to exe (bundled or manual layout).
    3. Local zip files next to exe.
    4. Download from GitHub Releases.
    """
    # 1. Explicit local zip.
    if local_zip_path:
        p = Path(local_zip_path)
        if not p.exists():
            print(f"Provided patch zip does not exist: {p}")
            return None
        print(f"Using provided patch zip: {p}")
        extracted = _extract_patch_zip(p)
        if extracted is not None:
            return extracted
        print("Failed to extract provided zip — it may be corrupted or incomplete.")
        return None

    # 2. Extracted folder.
    for candidate in get_patch_source_candidates(runtime_base_dir):
        ok, _ = validate_patch_source(candidate)
        if ok:
            return candidate

    # 3. Local zip files.
    for zip_candidate in _get_zip_source_candidates(runtime_base_dir):
        extracted = _extract_patch_zip(zip_candidate)
        if extracted is not None:
            return extracted

    # 4. Download.
    temp_root = Path(tempfile.mkdtemp(prefix=f"pal4_dl_{PATCH_VERSION}_"))
    zip_path = _download_patch_zip(temp_root)
    if zip_path is not None:
        extracted = _extract_patch_zip(zip_path)
        if extracted is not None:
            _TEMP_PATCH_DIRS.append(temp_root)
            return extracted
        zip_path.unlink(missing_ok=True)
    shutil.rmtree(temp_root, ignore_errors=True)

    return None


# ---------------------------------------------------------------------------
#  Quick uninstall batch
# ---------------------------------------------------------------------------

def write_quick_uninstall_bat(game_dir: Path, patch_files: list[Path]) -> None:
    lines = [
        "@echo off",
        "setlocal",
        "set GAME_DIR=%~dp0",
        f"set BACKUP_DIR=%GAME_DIR%{BACKUP_DIR_NAME}\\",
        "echo PAL4 English Mod quick uninstall",
        "echo.",
        "if not exist \"%BACKUP_DIR%\" (",
        "  echo Backup folder was not found. Cannot restore original files.",
        "  pause",
        "  exit /b 1",
        ")",
    ]

    for rel in patch_files:
        rel_win = str(rel).replace("/", "\\")
        lines.extend(
            [
                f"if exist \"%BACKUP_DIR%{rel_win}\" (",
                f"  copy /Y \"%BACKUP_DIR%{rel_win}\" \"%GAME_DIR%{rel_win}\" >nul",
                ") else (",
                f"  echo Missing backup for: {rel_win}",
                ")",
            ]
        )

    lines.extend(
        [
            f"if exist \"%GAME_DIR%{MOD_STATE_DIR_NAME}\\{MANIFEST_FILE_NAME}\" del /f /q \"%GAME_DIR%{MOD_STATE_DIR_NAME}\\{MANIFEST_FILE_NAME}\" >nul",
            "echo.",
            "echo Restore finished. You can now launch the original game.",
            "pause",
        ]
    )

    bat_path = game_dir / QUICK_UNINSTALL_BAT_NAME
    bat_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


# ---------------------------------------------------------------------------
#  Install / Uninstall / Upgrade
# ---------------------------------------------------------------------------

def install_patch(
    game_dir: Path,
    runtime_base_dir: Path,
    local_zip_path: str | None = None,
) -> int:
    _emit_progress(0, 0, "Looking for patch source...")

    patch_source = get_patch_source_dir(runtime_base_dir, local_zip_path=local_zip_path)
    if patch_source is None:
        print("Could not obtain patch source — no local data found and download failed.")
        print("Searched locations:")
        for candidate in get_patch_source_candidates(runtime_base_dir):
            print(f"  - {candidate}")
        for zip_candidate in _get_zip_source_candidates(runtime_base_dir):
            print(f"  - {zip_candidate}")
        return 1

    ok, missing = validate_patch_source(patch_source)
    if not ok:
        print("Patch source is incomplete. Missing files:")
        for rel in missing:
            print(f"  - {rel}")
        print("Searched locations:")
        for candidate in get_patch_source_candidates(runtime_base_dir):
            print(f"  - {candidate}")
        for zip_candidate in _get_zip_source_candidates(runtime_base_dir):
            print(f"  - {zip_candidate}")
        return 1

    backup_root = game_dir / BACKUP_DIR_NAME
    state_dir = game_dir / MOD_STATE_DIR_NAME
    manifest_path = state_dir / MANIFEST_FILE_NAME

    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_version = str(existing_manifest.get("version", "")).strip()
            if existing_version and existing_version != PATCH_VERSION:
                print(f"Detected existing mod version: {existing_version}. Will update to v{PATCH_VERSION}.")
        except Exception:
            print("Warning: failed to read existing manifest. Installer will continue with backup-safe mode.")

    missing_in_game = [rel for rel in REQUIRED_PATCH_FILES if not (game_dir / rel).exists()]
    if missing_in_game:
        print("Install aborted. Some target files do not exist in your game folder:")
        for rel in missing_in_game:
            print(f"  - {rel}")
        return 1

    all_patch_files = get_all_patch_files(patch_source)

    print(f"Installing PAL4 English Mod v{PATCH_VERSION} to:")
    print(f"  {game_dir}")

    backup_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    total_files = len(all_patch_files)
    for i, rel in enumerate(all_patch_files):
        src = patch_source / rel
        dst = game_dir / rel
        backup = backup_root / rel

        if dst.exists() and not backup.exists():
            ensure_parent(backup)
            shutil.copy2(dst, backup)

        ensure_parent(dst)
        shutil.copy2(src, dst)
        print(f"  Patched: {rel}")
        _emit_progress(i + 1, total_files, f"Patching files... ({i+1}/{total_files})")

    manifest = {
        "mod_name": "PAL4 English Mod",
        "version": PATCH_VERSION,
        "installed_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": [str(p).replace("\\", "/") for p in all_patch_files],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    write_quick_uninstall_bat(game_dir, all_patch_files)
    print(f"Quick uninstall entry created: {game_dir / QUICK_UNINSTALL_BAT_NAME}")
    print("Install completed.")
    _emit_progress(total_files, total_files, "Install complete!")
    return 0


def uninstall_patch(game_dir: Path) -> int:
    backup_root = game_dir / BACKUP_DIR_NAME
    state_dir = game_dir / MOD_STATE_DIR_NAME
    manifest_path = state_dir / MANIFEST_FILE_NAME

    if not backup_root.exists():
        print("Backup folder not found. Cannot uninstall safely.")
        return 1

    # Determine which files to restore: prefer manifest, fall back to backup scan.
    files_to_restore: list[Path] = []
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files_to_restore = [Path(f) for f in existing_manifest.get("files", [])]
        except Exception:
            pass
    if not files_to_restore:
        # Fallback: enumerate all files present in the backup directory.
        for p in sorted(backup_root.rglob("*")):
            if p.is_file():
                files_to_restore.append(p.relative_to(backup_root))

    print(f"Uninstalling PAL4 English Mod from: {game_dir}")

    restored_any = False
    for rel in files_to_restore:
        backup = backup_root / rel
        dst = game_dir / rel
        if backup.exists():
            ensure_parent(dst)
            shutil.copy2(backup, dst)
            restored_any = True
            print(f"  Restored: {rel}")
        else:
            print(f"  Warning: backup missing, skipped: {rel}")

    if manifest_path.exists():
        manifest_path.unlink()

    quick_uninstall = game_dir / QUICK_UNINSTALL_BAT_NAME
    if quick_uninstall.exists():
        quick_uninstall.unlink()

    if restored_any:
        print("Uninstall completed. Original files restored from backup.")
        return 0

    print("No files were restored. Please check backup contents.")
    return 1


def upgrade_patch_placeholder(game_dir: Path, runtime_base_dir: Path) -> int:
    print("Running upgrade workflow...")
    return install_patch(game_dir, runtime_base_dir)


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PAL4 English Mod installer")
    parser.add_argument("--install", action="store_true", help="Install patch")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall patch")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade patch")
    parser.add_argument("--game-path", type=str, default=None, help="Path to game folder")
    parser.add_argument("--patch-zip", type=str, default=None,
                        help="Path to local patch zip (skips download)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation when possible",
    )
    return parser


def interactive_menu() -> str:
    print("Choose an action:")
    print("  1. Install mod")
    print("  2. Uninstall mod")
    print("  3. Upgrade mod")
    print("  4. Exit")
    choice = input("Enter 1/2/3/4: ").strip()
    return {
        "1": "install",
        "2": "uninstall",
        "3": "upgrade",
        "4": "exit",
    }.get(choice, "invalid")


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    runtime_base_dir = get_runtime_base_dir()

    print_disclaimer()

    explicit_action_count = sum([args.install, args.uninstall, args.upgrade])
    if explicit_action_count > 1:
        print("Please select only one action: --install, --uninstall, or --upgrade")
        return 2

    if explicit_action_count == 0:
        action = interactive_menu()
        if action == "exit":
            return 0
        if action == "invalid":
            print("Invalid selection.")
            return 2
    else:
        action = "install" if args.install else "uninstall" if args.uninstall else "upgrade"

    game_dir = find_game_path(args.game_path)
    if game_dir is None:
        print("Could not determine a valid game path.")
        return 1

    print(f"Selected game path: {game_dir}")
    if not args.yes:
        confirm = input("Continue? (y/N): ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Cancelled by user.")
            return 0

    if action == "install":
        return install_patch(game_dir, runtime_base_dir, local_zip_path=args.patch_zip)
    if action == "uninstall":
        return uninstall_patch(game_dir)
    if action == "upgrade":
        return upgrade_patch_placeholder(game_dir, runtime_base_dir)

    print("Unknown action.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
