#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def is_translated(row: dict[str, str]) -> bool:
    original = (row.get("original_text") or "").strip()
    translation = (row.get("translation") or "").strip()
    return bool(original and translation and original != translation)


def is_pal4db_row(row: dict[str, str]) -> bool:
    file_path = (row.get("file") or "").replace("/", "\\").lower()
    return file_path.endswith("pal4db.db")


def build_index(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    pal_rows = [row for row in rows if is_pal4db_row(row) and is_translated(row)]
    indexed: list[dict[str, str]] = []
    for idx, row in enumerate(pal_rows, start=1):
        item = dict(row)
        item["slice_index"] = str(idx)
        indexed.append(item)
    return indexed


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def cmd_extract(args: argparse.Namespace) -> int:
    rows = load_rows(Path(args.input))
    indexed = build_index(rows)

    fieldnames = [
        "slice_index",
        "file",
        "length_offset",
        "text_offset",
        "byte_length",
        "original_text",
        "translation",
    ]
    write_csv(Path(args.output), indexed, fieldnames)
    print(f"Extracted {len(indexed)} translated Pal4db rows to {args.output}")
    return 0


def resolve_range(total: int, start: int | None, end: int | None, first: int | None) -> tuple[int, int]:
    if first is not None:
        if first < 0:
            raise ValueError("--first must be >= 0")
        return (1, min(total, first))

    s = 1 if start is None else start
    e = total if end is None else end
    if s < 1:
        raise ValueError("--start must be >= 1")
    if e < s:
        raise ValueError("--end must be >= --start")
    if s > total:
        return (total + 1, total)
    return (s, min(total, e))


def cmd_slice(args: argparse.Namespace) -> int:
    input_rows = load_rows(Path(args.input))
    indexed = build_index(input_rows)
    total = len(indexed)
    if total == 0:
        raise SystemExit("No translated Pal4db rows found in input CSV")

    start, end = resolve_range(total, args.start, args.end, args.first)
    selected_indices = {i for i in range(start, end + 1)} if start <= end else set()

    # Build lookup by stable identity tuple.
    selected_keys = set()
    for row in indexed:
        idx = int(row["slice_index"])
        if idx in selected_indices:
            key = (row.get("file", ""), row.get("length_offset", ""), row.get("text_offset", ""))
            selected_keys.add(key)

    output_rows: list[dict[str, str]] = []
    for row in input_rows:
        new_row = dict(row)
        key = (new_row.get("file", ""), new_row.get("length_offset", ""), new_row.get("text_offset", ""))

        # Default behavior: clear all translations, only keep selected Pal4db rows.
        new_row["translation"] = ""
        if key in selected_keys:
            new_row["translation"] = row.get("translation", "")

        # Optional path remap to target DB file for import convenience.
        if args.target_db and is_pal4db_row(new_row):
            new_row["file"] = args.target_db

        output_rows.append(new_row)

    fieldnames = list(output_rows[0].keys()) if output_rows else [
        "file",
        "length_offset",
        "text_offset",
        "byte_length",
        "original_text",
        "translation",
    ]
    write_csv(Path(args.output), output_rows, fieldnames)

    print(f"Total translated Pal4db rows: {total}")
    print(f"Selected slice: {start}-{end} (count={len(selected_indices)})")
    print(f"Wrote slice CSV: {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract and slice Pal4db translated rows for incremental import debugging."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="Extract translated Pal4db rows and assign slice_index")
    p_extract.add_argument("--input", required=True, help="Input translated CSV path")
    p_extract.add_argument("--output", required=True, help="Output Pal4db-only indexed CSV path")
    p_extract.set_defaults(func=cmd_extract)

    p_slice = sub.add_parser("slice", help="Create a full import CSV with only selected Pal4db translation rows enabled")
    p_slice.add_argument("--input", required=True, help="Input translated CSV path")
    p_slice.add_argument("--output", required=True, help="Output sliced CSV path")
    p_slice.add_argument("--first", type=int, help="Keep first N translated Pal4db rows")
    p_slice.add_argument("--start", type=int, help="Start index (1-based, inclusive)")
    p_slice.add_argument("--end", type=int, help="End index (1-based, inclusive)")
    p_slice.add_argument("--target-db", help="Rewrite Pal4db row file path to this DB file path")
    p_slice.set_defaults(func=cmd_slice)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
