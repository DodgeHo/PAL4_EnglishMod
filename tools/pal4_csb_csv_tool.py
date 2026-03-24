#!/usr/bin/env python3
import argparse
import csv
import re
import shutil
import struct
import time
from pathlib import Path


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
HAS_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISIBLE_HINT_RE = re.compile(r"[：，。！？、“”‘’（）《》…～]|<colour|</colour>|<dc0>|</dc0>")


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def write_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def collect_csb_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() == ".csb":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.csb")))

    seen: set[str] = set()
    result: list[Path] = []
    for path in files:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def is_visible_text(text: str, min_chars: int, strict: bool = True) -> bool:
    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    if "\x00" in stripped:
        return False
    if not HAS_CJK_RE.search(stripped):
        return False

    if not strict:
        return True

    if IDENTIFIER_RE.fullmatch(stripped):
        return False
    cjk_count = sum(1 for char in stripped if HAS_CJK_RE.match(char))
    if cjk_count < 2 and not VISIBLE_HINT_RE.search(stripped):
        return False
    if stripped[:1].isascii() and stripped[:1].isalpha() and cjk_count < 2:
        return False
    return True


def iter_length_prefixed_strings(data: bytes, encoding: str, min_chars: int, max_bytes: int, strict: bool = True):
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


def export_csv(args: argparse.Namespace) -> int:
    files = collect_csb_files(args.input)
    if not files:
        raise SystemExit("No .csb files found from --input")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str | int]] = []
    strict_filtering = args.mode != "all"
    for file_path in files:
        data = file_path.read_bytes()
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

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["file", "length_offset", "text_offset", "byte_length", "original_text", "translation"],
        )
        writer.writeheader()
        writer.writerows(rows)

    mode_label = "all CJK" if not strict_filtering else "strict"
    print(f"Exported {len(rows)} text rows from {len(files)} file(s) to {output_path} (mode: {mode_label})")
    return 0


def build_backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}.bak.{stamp}")


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def import_csv(args: argparse.Namespace) -> int:
    csv_path = Path(args.input)
    rows = load_rows(csv_path)
    if not rows:
        raise SystemExit("CSV is empty")

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

    for file_path, file_rows in grouped.items():
        if not file_path.exists():
            raise SystemExit(f"File not found: {file_path}")

        data = bytearray(file_path.read_bytes())
        changed = False
        cumulative_delta = 0

        for row in sorted(file_rows, key=lambda item: int(item["text_offset"])):
            base_length_offset = int(row["length_offset"])
            base_text_offset = int(row["text_offset"])
            old_len = int(row["byte_length"])
            original_text = row["original_text"]
            translation = row["translation"]

            length_offset = base_length_offset + cumulative_delta
            text_offset = base_text_offset + cumulative_delta

            current_len = read_u32(data, length_offset)
            if current_len != old_len:
                raise SystemExit(
                    f"Length mismatch in {file_path} at 0x{length_offset:X}: csv={old_len}, file={current_len}. "
                    "Please re-export from the current .csb baseline."
                )

            current_raw = bytes(data[text_offset : text_offset + old_len])
            expected_raw = original_text.encode(args.encoding)
            if current_raw != expected_raw:
                raise SystemExit(
                    f"Original text mismatch in {file_path} at 0x{text_offset:X}. "
                    "The file no longer matches the CSV baseline."
                )

            new_raw = translation.encode(args.encoding)
            if args.overflow == "exact" and len(new_raw) != old_len:
                raise SystemExit(
                    f"Byte length changed in {file_path} at 0x{text_offset:X}: "
                    f"old={old_len}, new={len(new_raw)}. "
                    "Use --overflow expand to allow variable-length replacements."
                )

            new_len = len(new_raw)
            if new_len == old_len:
                data[text_offset : text_offset + old_len] = new_raw
            else:
                data[text_offset : text_offset + old_len] = new_raw
                cumulative_delta += new_len - old_len

            write_u32(data, length_offset, new_len)
            changed = True
            updated_rows += 1

        if changed:
            # Observed on PAL4 CSB samples: offset 0 stores (file_size - 4).
            write_u32(data, 0, len(data) - 4)
            if args.backup:
                backup_path = build_backup_path(file_path)
                shutil.copy2(file_path, backup_path)
            file_path.write_bytes(data)
            updated_files += 1

    print(f"Updated {updated_rows} rows in {updated_files} file(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PAL4 .csb visible-text CSV export/import tool (safe same-byte-length importer)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export visible strings from .csb files to CSV")
    export_parser.add_argument("--input", nargs="+", required=True, help="Input .csb file(s) or folder(s)")
    export_parser.add_argument("--output", required=True, help="Output CSV path")
    export_parser.add_argument("--encoding", default="gbk", help="String encoding used by the script files")
    export_parser.add_argument("--min-chars", type=int, default=2, help="Minimum visible text length to export")
    export_parser.add_argument("--max-bytes", type=int, default=4096, help="Maximum length-prefixed string size to scan")
    export_parser.add_argument(
        "--mode",
        choices=["strict", "all"],
        default="strict",
        help="strict: apply strong CJK filtering (default); all: extract all strings with at least 1 CJK character",
    )
    export_parser.set_defaults(func=export_csv)

    import_parser = subparsers.add_parser("import", help="Import translated CSV back into .csb files")
    import_parser.add_argument("--input", required=True, help="Translated CSV path")
    import_parser.add_argument("--encoding", default="gbk", help="String encoding used by the script files")
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