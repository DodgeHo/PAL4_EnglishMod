#!/usr/bin/env python3
import argparse
import csv
import re
import shutil
import struct
import sys
import time
from pathlib import Path


DB_MAGIC = b"GAME_DB_FLAG"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
HAS_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISIBLE_HINT_RE = re.compile(r"[：，。！？、“”‘’（）《》…～]|[A-Za-z]+\s+[A-Za-z]+")


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def write_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def align_line_endings(text: str, reference: str) -> str:
    if "\r\n" in reference:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if "\r" in reference and "\r\n" not in reference:
        return text.replace("\r\n", "\n").replace("\n", "\r")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def build_backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.name}.bak.{stamp}")


def is_pal4_db_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".db":
        return False
    try:
        header = path.read_bytes()[: 64]
    except OSError:
        return False
    return DB_MAGIC in header


def collect_db_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_file() and is_pal4_db_file(path):
            files.append(path)
        elif path.is_dir():
            for db_path in sorted(path.rglob("*.db")):
                if is_pal4_db_file(db_path):
                    files.append(db_path)

    seen: set[str] = set()
    result: list[Path] = []
    for path in files:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def is_visible_text(text: str, min_chars: int, include_ascii: bool) -> bool:
    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    if "\x00" in stripped:
        return False
    if IDENTIFIER_RE.fullmatch(stripped):
        return False

    has_cjk = bool(HAS_CJK_RE.search(stripped))
    if not include_ascii and not has_cjk:
        return False

    if has_cjk:
        cjk_count = sum(1 for char in stripped if HAS_CJK_RE.match(char))
        if cjk_count < 1 and not VISIBLE_HINT_RE.search(stripped):
            return False
        return True

    return bool(VISIBLE_HINT_RE.search(stripped))


def iter_length_prefixed_strings(data: bytes, encoding: str, min_chars: int, max_bytes: int, include_ascii: bool):
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

        if not is_visible_text(text, min_chars, include_ascii):
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
    files = collect_db_files(args.input)
    if not files:
        raise SystemExit("No PAL4 DB files found from --input")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str | int]] = []
    for file_path in files:
        data = file_path.read_bytes()
        for entry in iter_length_prefixed_strings(
            data,
            args.encoding,
            args.min_chars,
            args.max_bytes,
            args.include_ascii,
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

    print(f"Exported {len(rows)} text rows from {len(files)} file(s) to {output_path}")
    return 0


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def resolve_input_path(raw: str) -> Path:
    p = Path(raw)
    if p.exists():
        return p
    p2 = Path("scripts") / raw
    if p2.exists():
        return p2
    raise FileNotFoundError(f"Input CSV not found: {raw}")


def load_indexed_rows(path: Path) -> list[dict[str, str]]:
    rows = load_rows(path)
    # 兼容 index 或 slice_index
    if rows:
        if "slice_index" not in rows[0] and "index" in rows[0]:
            # 动态转为 slice_index
            for r in rows:
                r["slice_index"] = r["index"]
        elif "slice_index" not in rows[0]:
            raise ValueError("Input CSV must contain slice_index or index column.")
    return rows


def apply_indexed_slice(
    target_db: Path,
    rows: list[dict[str, str]],
    start: int,
    end: int,
    skip_indices: set[int],
    encoding: str,
    overflow: str,
    backup: bool,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    candidates = []
    selected_count = 0
    for row in rows:
        idx = int(row.get("slice_index") or 0)
        if idx <= end:
            candidates.append(row)
            if start <= idx <= end and idx not in skip_indices:
                selected_count += 1

    if selected_count == 0:
        return (0, 0, 0, 0)

    data = bytearray(target_db.read_bytes())
    updated = 0
    skipped = 0
    skipped_by_blacklist = 0
    cumulative_delta = 0

    candidates.sort(key=lambda r: int(r.get("slice_index") or 0))

    # Walk rows in index order up to `end` and keep cumulative_delta in sync
    # with actual file state. This supports both sequential runs and isolated
    # range runs, and allows skipping blacklisted indices safely.
    for row in candidates:
        idx = int(row.get("slice_index") or 0)
        old_len = int(row["byte_length"])
        base_length_offset = int(row["length_offset"])
        base_text_offset = int(row["text_offset"])
        original_text = row.get("original_text") or ""
        translation = row.get("translation") or ""

        if not translation.strip() or translation.strip() == original_text.strip():
            continue

        length_offset = base_length_offset + cumulative_delta
        text_offset = base_text_offset + cumulative_delta
        current_len = read_u32(data, length_offset)

        try:
            old_raw = original_text.encode(encoding)
            new_raw = translation.encode(encoding)
        except UnicodeEncodeError:
            # If encoding fails here, this row cannot be reliably used for bootstrap.
            continue

        is_translated = (
            current_len == len(new_raw)
            and bytes(data[text_offset : text_offset + current_len]) == new_raw
        )
        is_original = (
            current_len == old_len
            and bytes(data[text_offset : text_offset + old_len]) == old_raw
        )

        if is_translated:
            cumulative_delta += len(new_raw) - old_len
            if idx >= start and idx in skip_indices:
                skipped_by_blacklist += 1
            elif idx >= start:
                skipped += 1
            continue
        if is_original:
            # Apply only when row is in selected range and not blacklisted.
            should_apply = (start <= idx <= end and idx not in skip_indices)
            if not should_apply:
                if start <= idx <= end and idx in skip_indices:
                    skipped_by_blacklist += 1
                continue

            translation_aligned = align_line_endings(translation, original_text)
            new_raw_aligned = translation_aligned.encode(encoding)
            new_len = len(new_raw_aligned)
            if overflow == "exact" and new_len != old_len:
                raise SystemExit(
                    f"Byte length changed at 0x{text_offset:X}: old={old_len}, new={new_len}. "
                    "Use -overflow expand or --overflow expand."
                )

            data[text_offset : text_offset + old_len] = new_raw_aligned
            write_u32(data, length_offset, new_len)
            if new_len != old_len:
                cumulative_delta += new_len - old_len
            updated += 1
            continue

        # Tolerate line-ending normalization when comparing original content.
        current_raw = bytes(data[text_offset : text_offset + min(current_len, max(old_len, 0))])
        try:
            current_text = current_raw.decode(encoding)
        except UnicodeDecodeError:
            current_text = ""
        if current_len == old_len and current_text and normalize_line_endings(current_text) == normalize_line_endings(original_text):
            continue

        raise SystemExit(
            f"Baseline state ambiguous before slice start at 0x{text_offset:X}. "
            "Reset DB to baseline or continue from a file modified by the same indexed CSV."
        )

    if updated and not dry_run:
        if backup:
            backup_path = build_backup_path(target_db)
            shutil.copy2(target_db, backup_path)
        target_db.write_bytes(data)

    return (selected_count, updated, skipped, skipped_by_blacklist)


def parse_skip_ranges(raw: str) -> set[int]:
    result: set[int] = set()
    if not raw.strip():
        return result

    tokens = [token for token in re.split(r"[\s,;]+", raw.strip()) if token]
    for token in tokens:
        if "-" in token:
            left, right = token.split("-", 1)
            start = int(left)
            end = int(right)
            if end < start:
                raise ValueError(f"Invalid skip range: {token}")
            for value in range(start, end + 1):
                result.add(value)
        else:
            result.add(int(token))
    return result


def run_compat_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Apply Pal4db translation slice by index range (compat mode)."
    )
    parser.add_argument("--input", required=True, help="Indexed CSV path, e.g. pal4db_only_translations.indexed.csv")
    parser.add_argument("--file", required=True, help="Target Pal4db.db path")
    parser.add_argument("--start", type=int, default=1, help="Start index (1-based, inclusive)")
    parser.add_argument("--end", type=int, required=True, help="End index (1-based, inclusive)")
    parser.add_argument("--encoding", default="gbk", help="String encoding")
    parser.add_argument("--overflow", "-overflow", choices=["exact", "expand"], default="exact")
    parser.add_argument(
        "--skip-ranges",
        default="",
        help="Blacklist indices/ranges to skip, e.g. '2156-2169 2186-2235'",
    )
    parser.add_argument("--backup", action="store_true", help="Create backup before writing")
    parser.add_argument("--dry-run", action="store_true", help="Validate and simulate without writing")
    args = parser.parse_args(argv)

    if args.start < 1:
        raise SystemExit("--start must be >= 1")
    if args.end < args.start:
        raise SystemExit("--end must be >= --start")

    skip_indices = parse_skip_ranges(args.skip_ranges)

    input_path = resolve_input_path(args.input)
    target_db = Path(args.file)
    if not target_db.exists():
        raise FileNotFoundError(f"Target DB not found: {target_db}")

    rows = load_indexed_rows(input_path)
    selected_count, updated_count, skipped_count, skipped_blacklist_count = apply_indexed_slice(
        target_db=target_db,
        rows=rows,
        start=args.start,
        end=args.end,
        skip_indices=skip_indices,
        encoding=args.encoding,
        overflow=args.overflow,
        backup=args.backup,
        dry_run=args.dry_run,
    )

    mode = "dry-run" if args.dry_run else "write"
    print(f"[{mode}] input={input_path}")
    print(f"[{mode}] file={target_db}")
    print(
        f"[{mode}] selected={selected_count}, updated={updated_count}, "
        f"skipped={skipped_count}, skipped_blacklist={skipped_blacklist_count}"
    )
    return 0


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
    skipped_already_applied = 0

    for file_path, file_rows in grouped.items():
        if not file_path.exists():
            raise SystemExit(f"File not found: {file_path}")
        if not is_pal4_db_file(file_path):
            raise SystemExit(f"Not a PAL4 DB file (missing GAME_DB_FLAG): {file_path}")

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

            # Idempotent import: if this row already equals translated content,
            # skip safely and keep cumulative delta in sync for subsequent rows.
            if current_len == new_len_pre:
                current_new_raw = bytes(data[text_offset : text_offset + current_len])
                if current_new_raw == new_raw_pre:
                    skipped_already_applied += 1
                    cumulative_delta += new_len_pre - old_len
                    continue

            if current_len != old_len:
                raise SystemExit(
                    f"Length mismatch in {file_path} at 0x{length_offset:X}: csv={old_len}, file={current_len}. "
                    "Please re-export from the current DB baseline."
                )

            current_raw = bytes(data[text_offset : text_offset + old_len])
            try:
                current_text = current_raw.decode(args.encoding)
            except UnicodeDecodeError:
                current_text = ""

            # CSV readers may normalize line endings. Accept textual equality
            # after line-ending normalization to avoid false baseline mismatch.
            if current_raw != original_text.encode(args.encoding, errors="ignore") and (
                not current_text
                or normalize_line_endings(current_text) != normalize_line_endings(original_text)
            ):
                raise SystemExit(
                    f"Original text mismatch in {file_path} at 0x{text_offset:X}. "
                    "The DB no longer matches the CSV baseline."
                )

            translation_aligned = align_line_endings(translation, current_text or original_text)
            new_raw = translation_aligned.encode(args.encoding)
            new_len = len(new_raw)

            if args.overflow == "exact" and len(new_raw) != old_len:
                raise SystemExit(
                    f"Byte length changed in {file_path} at 0x{text_offset:X}: old={old_len}, new={len(new_raw)}. "
                    "Use --overflow expand to allow variable-length replacements."
                )

            data[text_offset : text_offset + old_len] = new_raw
            write_u32(data, length_offset, new_len)
            if new_len != old_len:
                cumulative_delta += new_len - old_len

            changed = True
            updated_rows += 1

        if changed and not args.dry_run:
            if args.backup:
                backup_path = build_backup_path(file_path)
                shutil.copy2(file_path, backup_path)
            file_path.write_bytes(data)
            updated_files += 1
        elif changed:
            updated_files += 1

    mode = "dry-run" if args.dry_run else "write"
    print(
        f"[{mode}] Updated {updated_rows} rows in {updated_files} file(s), "
        f"skipped already-applied rows: {skipped_already_applied}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PAL4 .db text CSV export/import tool (length-prefixed string workflow)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export localizable strings from PAL4 .db files to CSV")
    export_parser.add_argument("--input", nargs="+", required=True, help="Input .db file(s) or folder(s)")
    export_parser.add_argument("--output", required=True, help="Output CSV path")
    export_parser.add_argument("--encoding", default="gbk", help="String encoding used by PAL4 DB files")
    export_parser.add_argument("--min-chars", type=int, default=2, help="Minimum visible text length to export")
    export_parser.add_argument("--max-bytes", type=int, default=2048, help="Maximum length-prefixed string size to scan")
    export_parser.add_argument(
        "--include-ascii",
        action="store_true",
        help="Also export pure-ASCII visible strings (default exports CJK-containing strings only)",
    )
    export_parser.set_defaults(func=export_csv)

    import_parser = subparsers.add_parser("import", help="Import translated CSV back into PAL4 .db files")
    import_parser.add_argument("--input", required=True, help="Translated CSV path")
    import_parser.add_argument("--encoding", default="gbk", help="String encoding used by PAL4 DB files")
    import_parser.add_argument(
        "--overflow",
        choices=["exact", "expand"],
        default="exact",
        help="exact: require same byte length; expand: allow variable length replacements",
    )
    import_parser.add_argument("--backup", action="store_true", help="Create timestamped .bak copy before writing")
    import_parser.add_argument("--dry-run", action="store_true", help="Validate and simulate import without writing files")
    import_parser.set_defaults(func=import_csv)

    return parser


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] not in {"export", "import"}:
        # Compatibility mode for direct slice apply CLI:
        # py tools/pal4_db_csv_tool.py --input ... --file ... --start ... --end ... -overflow expand
        return run_compat_cli(argv)

    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
