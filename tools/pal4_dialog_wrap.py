#!/usr/bin/env python3
import argparse
import csv
import re
from pathlib import Path

TAG_RE = re.compile(r"<[^>]+>")
LATIN_RE = re.compile(r"[A-Za-z]")


def char_width(ch: str) -> int:
    if ord(ch) < 128:
        return 1
    return 2


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def tokenize_with_tags(text: str):
    pos = 0
    for match in TAG_RE.finditer(text):
        if match.start() > pos:
            yield ("TEXT", text[pos:match.start()])
        yield ("TAG", match.group(0))
        pos = match.end()
    if pos < len(text):
        yield ("TEXT", text[pos:])


def wrap_plain_text(text: str, max_cols: int) -> str:
    # Keep existing line breaks as hard paragraph boundaries.
    paragraphs = text.split("\n")
    wrapped_paragraphs = []
    for para in paragraphs:
        words = re.split(r"(\s+)", para)
        line = ""
        line_w = 0
        out = []
        for part in words:
            if not part:
                continue
            if part.isspace():
                # Collapse multi-space runs outside tags to one space for stable wrapping.
                if line and not line.endswith(" "):
                    line += " "
                    line_w += 1
                continue

            part_w = text_width(part)
            if line_w + part_w <= max_cols:
                line += part
                line_w += part_w
                continue

            if line:
                out.append(line.rstrip())
                line = ""
                line_w = 0

            if part_w <= max_cols:
                line = part
                line_w = part_w
            else:
                # Extremely long token: hard-wrap by character.
                chunk = ""
                chunk_w = 0
                for ch in part:
                    w = char_width(ch)
                    if chunk and chunk_w + w > max_cols:
                        out.append(chunk)
                        chunk = ch
                        chunk_w = w
                    else:
                        chunk += ch
                        chunk_w += w
                line = chunk
                line_w = chunk_w

        if line:
            out.append(line.rstrip())
        wrapped_paragraphs.append("\n".join(out))

    return "\n".join(wrapped_paragraphs)


def wrap_text_preserving_tags(text: str, max_cols: int) -> str:
    # Wrap only text spans, tags are zero-width and passed through untouched.
    chunks = []
    for kind, value in tokenize_with_tags(text):
        if kind == "TAG":
            chunks.append((kind, value))
        else:
            chunks.append((kind, wrap_plain_text(value, max_cols)))

    return "".join(value for _, value in chunks)


def process_csv(input_path: Path, output_path: Path, column: str, max_cols: int, latin_only: bool) -> tuple[int, int]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
        fieldnames = fh.seek(0) or None

    # Re-open to get fieldnames from DictReader reliably.
    with input_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        rows = list(reader)

    if column not in fields:
        raise SystemExit(f"Column '{column}' not found in CSV")

    changed_rows = 0
    for row in rows:
        raw = (row.get(column) or "")
        if not raw.strip():
            continue
        if latin_only and not LATIN_RE.search(raw):
            continue

        wrapped = wrap_text_preserving_tags(raw, max_cols)
        if wrapped != raw:
            row[column] = wrapped
            changed_rows += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    return len(rows), changed_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wrap PAL4 translated dialog lines by word while preserving inline tags like <colour> and <dc0>."
    )
    parser.add_argument("--input", required=True, help="Input translated CSV")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--column", default="translation", help="Target CSV column to wrap (default: translation)")
    parser.add_argument("--max-cols", type=int, default=42, help="Approx visual width before newline (default: 42)")
    parser.add_argument(
        "--all-text",
        action="store_true",
        help="Process all rows in the target column, not only rows containing Latin letters",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    total, changed = process_csv(
        input_path=Path(args.input),
        output_path=Path(args.output),
        column=args.column,
        max_cols=args.max_cols,
        latin_only=not args.all_text,
    )
    print(f"Processed {total} rows, updated {changed} rows -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
