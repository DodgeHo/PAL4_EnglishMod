import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RowMeta:
    row_index: int         # 1-based data row index (header excluded)
    line_start: int        # 1-based physical file line index
    line_end: int          # 1-based physical file line index


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_csv_with_line_meta(path: Path) -> tuple[list[str], list[dict[str, str]], list[RowMeta]]:
    rows: list[dict[str, str]] = []
    meta: list[RowMeta] = []

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        prev_end_line = 1  # header line
        for idx, row in enumerate(reader, start=1):
            end_line = reader.line_num
            start_line = prev_end_line + 1
            prev_end_line = end_line

            rows.append(row)
            meta.append(RowMeta(idx, start_line, end_line))

    return fieldnames, rows, meta


def row_matches_scope(row: dict[str, str], meta: RowMeta, scope: dict[str, Any]) -> bool:
    row_range = scope.get("row_range")
    if row_range:
        lo, hi = int(row_range[0]), int(row_range[1])
        if not (lo <= meta.row_index <= hi):
            return False

    line_range = scope.get("line_range")
    if line_range:
        lo, hi = int(line_range[0]), int(line_range[1])
        # overlap match: if any part of CSV row intersects the physical line range
        if meta.line_end < lo or meta.line_start > hi:
            return False

    file_equals = scope.get("file")
    if file_equals and row.get("file") != file_equals:
        return False

    file_regex = scope.get("file_regex")
    if file_regex and not re.search(file_regex, row.get("file", "")):
        return False

    original_contains = scope.get("original_contains")
    if original_contains and original_contains not in row.get("original_text", ""):
        return False

    original_not_contains = scope.get("original_not_contains")
    if original_not_contains and original_not_contains in row.get("original_text", ""):
        return False

    translation_contains = scope.get("translation_contains")
    if translation_contains and translation_contains not in row.get("translation", ""):
        return False

    return True


def apply_replace(text: str, action: dict[str, Any]) -> tuple[str, int]:
    src = action["from"]
    dst = action["to"]
    case_sensitive = bool(action.get("case_sensitive", False))
    whole_word = bool(action.get("whole_word", False))

    if whole_word:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = r"\\b" + re.escape(src) + r"\\b"
        new_text, n = re.subn(pattern, dst, text, flags=flags)
        return new_text, n

    if case_sensitive:
        n = text.count(src)
        return text.replace(src, dst), n

    pattern = re.escape(src)
    new_text, n = re.subn(pattern, dst, text, flags=re.IGNORECASE)
    return new_text, n


def apply_regex_replace(text: str, action: dict[str, Any]) -> tuple[str, int]:
    pattern = action["pattern"]
    repl = action["repl"]
    flags = 0
    if action.get("ignore_case", False):
        flags |= re.IGNORECASE
    new_text, n = re.subn(pattern, repl, text, flags=flags)
    return new_text, n


def apply_action_to_row(row: dict[str, str], action: dict[str, Any]) -> tuple[bool, int]:
    target_field = action.get("field", "translation")
    if target_field not in row:
        return False, 0

    text = row.get(target_field, "")
    original_text = text
    total_replacements = 0

    if action["type"] == "replace":
        text, n = apply_replace(text, action)
        total_replacements += n
    elif action["type"] == "regex_replace":
        text, n = apply_regex_replace(text, action)
        total_replacements += n
    else:
        raise ValueError(f"Unsupported action type: {action['type']}")

    if text != original_text:
        row[target_field] = text
        return True, total_replacements

    return False, 0


def process(rows: list[dict[str, str]], meta: list[RowMeta], rules: dict[str, Any]) -> dict[str, Any]:
    groups = rules.get("groups", [])

    changed_rows = 0
    total_replacements = 0
    group_hits: dict[str, int] = {}

    for row, m in zip(rows, meta):
        row_changed = False

        for group in groups:
            name = group.get("name", "unnamed")
            scope = group.get("scope", {})
            actions = group.get("actions", [])

            if not row_matches_scope(row, m, scope):
                continue

            local_hits = 0
            local_changed = False
            for action in actions:
                changed, reps = apply_action_to_row(row, action)
                if changed:
                    local_changed = True
                local_hits += reps

            if local_changed:
                group_hits[name] = group_hits.get(name, 0) + 1
            total_replacements += local_hits
            if local_changed:
                row_changed = True

        if row_changed:
            changed_rows += 1

    return {
        "changed_rows": changed_rows,
        "total_replacements": total_replacements,
        "group_hits": group_hits,
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply style/terminology rules to PAL4 translated CSV by line/row ranges."
    )
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--rules", required=True, help="Rules JSON path")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, do not write output")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    rules_path = Path(args.rules)

    fieldnames, rows, meta = parse_csv_with_line_meta(input_path)
    rules = load_json(rules_path)

    report = process(rows, meta, rules)

    print(f"Input rows: {len(rows)}")
    print(f"Changed rows: {report['changed_rows']}")
    print(f"Total replacements: {report['total_replacements']}")
    print("Group hits:")
    for name, cnt in sorted(report["group_hits"].items()):
        print(f"  {name}: {cnt}")

    if not args.dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(output_path, fieldnames, rows)
        print(f"Wrote: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
