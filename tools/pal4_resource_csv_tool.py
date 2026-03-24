#!/usr/bin/env python3
"""
PAL4 resource file text extractor (PNG, DDS, DFF, BIK, etc.)

Extracts length-prefixed strings embedded in binary resource files from:
- PALWorld, PALActor, palobject, ui, scenedata, Effect, palweapon, MatFX, etc.

Usage:
    export:  python pal4_resource_csv_tool.py export --input <folders> --output <csv>
    import:  python pal4_resource_csv_tool.py import --input <csv>
"""

import argparse
import csv
import re
import shutil
import struct
import sys
import time
from pathlib import Path


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
HAS_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISIBLE_HINT_RE = re.compile(r"[：，。！？、""''（）《》…～]|<colour|</colour>|<dc0>|</dc0>")

# Folders to scan in Decompressed
DEFAULT_FOLDERS = {
    "PALWorld", "PALActor", "palobject", "ui", "scenedata",
    "Effect", "palweapon", "MatFX", "2d", "videob", "VideoA"
}


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def write_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def build_backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}.bak.{stamp}")


def is_visible_text(text: str, min_chars: int, strict: bool = True) -> bool:
    """
    Check if text is visible and translatable.
    
    strict=True: Apply strict filtering (CJK count >= 2 or special punctuation)
    strict=False: Extract all strings containing at least 1 CJK character
    """
    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    if "\x00" in stripped:
        return False
    if not HAS_CJK_RE.search(stripped):
        return False
    
    if not strict:
        # Non-strict mode: accept any string with CJK characters
        return True
    
    # Strict mode: original filtering
    if IDENTIFIER_RE.fullmatch(stripped):
        return False
    cjk_count = sum(1 for char in stripped if HAS_CJK_RE.match(char))
    if cjk_count < 2 and not VISIBLE_HINT_RE.search(stripped):
        return False
    if stripped[:1].isascii() and stripped[:1].isalpha() and cjk_count < 2:
        return False
    return True


def iter_length_prefixed_strings(data: bytes, encoding: str, min_chars: int, max_bytes: int, strict: bool = True):
    """Extract length-prefixed strings from binary data"""
    seen_ranges: set[tuple[int, int]] = set()
    for offset in range(0, len(data) - 4):
        payload_len = read_u32(data, offset)
        if payload_len <= 0 or payload_len > max_bytes:
            continue

        start = offset + 4
        end = start + payload_len
        if end > len(data):
            continue

        raw = data[start:end]
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue

        if not is_visible_text(text, min_chars, strict=strict):
            continue

        item_range = (start, end)
        if item_range in seen_ranges:
            continue
        seen_ranges.add(item_range)

        yield {
            "length_offset": offset,
            "text_offset": start,
            "byte_length": payload_len,
            "original_text": text,
        }


def collect_resource_files(
    inputs: list[str],
    include_folders: set[str] | None = None,
    exclude_folders: set[str] | None = None,
) -> list[Path]:
    """Collect all resource files from input folders"""
    if include_folders is None:
        include_folders = DEFAULT_FOLDERS
    if exclude_folders is None:
        exclude_folders = set()

    files: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            # If input is Decompressed, scan specific subfolders
            for subfolder_name in include_folders:
                if subfolder_name in exclude_folders:
                    continue
                subfolder = path / subfolder_name
                if subfolder.is_dir():
                    files.extend(sorted(subfolder.rglob("*")))
        elif path.is_file():
            files.append(path)

    # Deduplicate
    seen: set[str] = set()
    result: list[Path] = []
    for fpath in files:
        if not fpath.is_file():
            continue
        resolved = str(fpath.resolve())
        if resolved not in seen:
            seen.add(resolved)
            result.append(fpath)
    return result


def export_csv(args: argparse.Namespace) -> int:
    """Export visible text from resource files to CSV"""
    exclude_set = set(args.exclude_folders) if args.exclude_folders else set()
    files = collect_resource_files(
        args.input,
        args.folders if args.folders else None,
        exclude_set,
    )
    if not files:
        raise SystemExit("No resource files found from --input")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str | int]] = []
    file_count = 0
    skipped_files = 0
    
    # Determine strict mode: default True, set False if mode is "all"
    strict_filtering = (args.mode != "all") if hasattr(args, "mode") else True

    for file_path in files:
        try:
            data = file_path.read_bytes()
        except (OSError, PermissionError):
            skipped_files += 1
            continue

        found_in_file = False
        for entry in iter_length_prefixed_strings(
            data,
            args.encoding,
            args.min_chars,
            args.max_bytes,
            strict=strict_filtering,
        ):
            rows.append(
                {
                    "file": str(file_path),
                    "length_offset": entry["length_offset"],
                    "text_offset": entry["text_offset"],
                    "byte_length": entry["byte_length"],
                    "original_text": entry["original_text"],
                    "translation": "",
                }
            )
            found_in_file = True

        if found_in_file:
            file_count += 1

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["file", "length_offset", "text_offset", "byte_length", "original_text", "translation"],
        )
        writer.writeheader()
        writer.writerows(rows)

    mode_label = "all CJK" if not strict_filtering else "strict"
    print(f"Exported {len(rows)} text rows from {file_count} file(s) to {output_path} (mode: {mode_label})")
    if skipped_files > 0:
        print(f"Skipped {skipped_files} unreadable files")
    return 0


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    """Load rows from CSV"""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def align_line_endings(text: str, reference: str) -> str:
    """Align line endings to match reference"""
    if "\r\n" in reference:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if "\r" in reference and "\r\n" not in reference:
        return text.replace("\r\n", "\n").replace("\n", "\r")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_line_endings(text: str) -> str:
    """Normalize line endings for comparison"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def import_csv(args: argparse.Namespace) -> int:
    """Import translated CSV back into resource files"""
    csv_path = Path(args.input)
    rows = load_rows(csv_path)
    if not rows:
        raise SystemExit("CSV is empty")

    # Group by file
    grouped: dict[Path, list[dict[str, str]]] = {}
    for row in rows:
        translation = (row.get("translation") or "").strip()
        original_text = row.get("original_text") or ""
        if not translation or translation == original_text:
            continue
        file_path = Path(row["file"])
        grouped.setdefault(file_path, []).append(row)

    if not grouped:
        print("No translated rows to import")
        return 0

    updated_files = 0
    updated_rows = 0
    skipped_already_applied = 0

    for file_path, file_rows in grouped.items():
        if not file_path.exists():
            print(f"Warning: File not found: {file_path}")
            continue

        data = bytearray(file_path.read_bytes())
        changed = False
        cumulative_delta = 0

        for row in sorted(file_rows, key=lambda item: int(item["text_offset"])):
            base_length_offset = int(row["length_offset"])
            base_text_offset = int(row["text_offset"])
            old_len = int(row["byte_length"])
            original_text = row["original_text"]
            translation = row["translation"]
            new_raw_pre = translation.encode(args.encoding)
            new_len_pre = len(new_raw_pre)

            length_offset = base_length_offset + cumulative_delta
            text_offset = base_text_offset + cumulative_delta
            current_len = read_u32(data, length_offset)

            # Idempotent: skip if already translated
            if current_len == new_len_pre:
                current_new_raw = bytes(data[text_offset : text_offset + current_len])
                if current_new_raw == new_raw_pre:
                    skipped_already_applied += 1
                    cumulative_delta += new_len_pre - old_len
                    continue

            if current_len != old_len:
                raise SystemExit(
                    f"Length mismatch in {file_path} at 0x{length_offset:X}: csv={old_len}, file={current_len}. "
                    "Please re-export from current baseline."
                )

            current_raw = bytes(data[text_offset : text_offset + old_len])
            try:
                current_text = current_raw.decode(args.encoding)
            except UnicodeDecodeError:
                current_text = ""

            # Tolerate line-ending normalization
            if current_raw != original_text.encode(args.encoding, errors="ignore") and (
                not current_text or normalize_line_endings(current_text) != normalize_line_endings(original_text)
            ):
                raise SystemExit(
                    f"Original text mismatch in {file_path} at 0x{text_offset:X}. "
                    "File no longer matches CSV baseline."
                )

            translation_aligned = align_line_endings(translation, current_text or original_text)
            new_raw = translation_aligned.encode(args.encoding)
            new_len = len(new_raw)

            if args.overflow == "exact" and new_len != old_len:
                raise SystemExit(
                    f"Byte length changed in {file_path} at 0x{text_offset:X}: old={old_len}, new={new_len}. "
                    "Use --overflow expand to allow variable-length replacements."
                )

            if new_len == old_len:
                data[text_offset : text_offset + old_len] = new_raw
            else:
                data[text_offset : text_offset + old_len] = new_raw
                cumulative_delta += new_len - old_len

            write_u32(data, length_offset, new_len)
            changed = True
            updated_rows += 1

        if changed:
            if args.backup:
                backup_path = build_backup_path(file_path)
                shutil.copy2(file_path, backup_path)
            file_path.write_bytes(data)
            updated_files += 1

    print(f"Updated {updated_rows} rows in {updated_files} file(s)")
    print(f"Skipped {skipped_already_applied} already-applied rows")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PAL4 resource file text CSV export/import tool (PNG, DDS, DFF, etc.)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export visible strings from resource files to CSV")
    export_parser.add_argument(
        "--input", nargs="+", required=True,
        help="Input folder(s) like D:\\PAL4_unpack\\Decompressed or D:\\PAL4_unpack\\Decompressed\\PALWorld"
    )
    export_parser.add_argument("--output", required=True, help="Output CSV path")
    export_parser.add_argument("--encoding", default="gbk", help="String encoding used by the resource files")
    export_parser.add_argument("--min-chars", type=int, default=2, help="Minimum visible text length to export")
    export_parser.add_argument("--max-bytes", type=int, default=4096, help="Maximum length-prefixed string size to scan")
    export_parser.add_argument(
        "--mode",
        choices=["strict", "all"],
        default="strict",
        help="strict: apply strong CJK filtering (default); all: extract all strings with at least 1 CJK character"
    )
    export_parser.add_argument(
        "--folders",
        nargs="+",
        help="Specific resource folders to scan (default: PALWorld, PALActor, palobject, ui, scenedata, Effect, palweapon, MatFX, 2d, videob, VideoA)"
    )
    export_parser.add_argument(
        "--exclude-folders",
        nargs="+",
        help="Folders to exclude from scanning (e.g., script database)"
    )
    export_parser.set_defaults(func=export_csv)

    import_parser = subparsers.add_parser("import", help="Import translated CSV back into resource files")
    import_parser.add_argument("--input", required=True, help="Translated CSV path")
    import_parser.add_argument("--encoding", default="gbk", help="String encoding used by the resource files")
    import_parser.add_argument(
        "--overflow",
        choices=["exact", "expand"],
        default="exact",
        help="exact: require same byte length; expand: allow variable length replacements",
    )
    import_parser.add_argument("--backup", action="store_true", help="Create timestamped .bak copy before writing")
    import_parser.set_defaults(func=import_csv)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
