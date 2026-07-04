from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
VISIBLE_CSV = ASSETS / "pal4_visible_export.translated.csv"
METRICS_JSON = ASSETS / "localization-metrics.json"
SHOWCASE_SVG = ASSETS / "localization-showcase.svg"

SPREADSHEET_NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def load_visible_text_metrics() -> dict:
    rows: list[dict[str, str]] = []
    with VISIBLE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(row)

    source_files = sorted({row.get("file", "") for row in rows if row.get("file")})
    source_file_counts = Counter(row.get("file", "") for row in rows if row.get("file"))
    original_chars = sum(len(row.get("original_text") or "") for row in rows)
    translation_chars = sum(len(row.get("translation") or "") for row in rows)
    english_words = sum(
        len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", row.get("translation") or ""))
        for row in rows
    )

    return {
        "translated_entries": len(rows),
        "script_source_files": len(source_files),
        "original_chinese_characters": original_chars,
        "english_translation_characters": translation_chars,
        "approx_english_words": english_words,
        "top_source_files": [
            {"file": file, "entries": count}
            for file, count in source_file_counts.most_common(8)
        ],
    }


def _cell_ref_col(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    value = 0
    for char in letters:
        value = value * 26 + (ord(char.upper()) - ord("A") + 1)
    return value


def _read_shared_strings(zip_file: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []
    root = ET.fromstring(zip_file.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall("a:si", SPREADSHEET_NS):
        values.append("".join((text.text or "") for text in item.findall(".//a:t", SPREADSHEET_NS)))
    return values


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    value_node = cell.find("a:v", SPREADSHEET_NS)
    if value_node is None:
        return ""
    raw = value_node.text or ""
    if cell.attrib.get("t") == "s" and raw.isdigit():
        index = int(raw)
        if index < len(shared_strings):
            return shared_strings[index]
    return raw


def load_workbook_metrics() -> dict:
    xlsx_files = sorted(ASSETS.glob("*.xlsx"))
    if not xlsx_files:
        raise FileNotFoundError("No .xlsx terminology workbook found in assets/.")
    workbook_path = xlsx_files[0]

    with ZipFile(workbook_path) as zip_file:
        shared_strings = _read_shared_strings(zip_file)
        workbook_root = ET.fromstring(zip_file.read("xl/workbook.xml"))
        rels_root = ET.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_root}

        sheets: list[dict] = []
        for sheet in workbook_root.findall("a:sheets/a:sheet", SPREADSHEET_NS):
            name = sheet.attrib["name"]
            rel_id = sheet.attrib[
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ]
            target = rel_targets[rel_id]
            path = f"xl/{target}" if not target.startswith("/") else target.lstrip("/")
            root = ET.fromstring(zip_file.read(path))

            non_empty_rows = 0
            non_empty_cells = 0
            english_cells = 0
            chinese_cells = 0
            max_column = 0

            for row in root.findall(".//a:sheetData/a:row", SPREADSHEET_NS):
                values = []
                for cell in row.findall("a:c", SPREADSHEET_NS):
                    max_column = max(max_column, _cell_ref_col(cell.attrib.get("r", "A1")))
                    value = _cell_value(cell, shared_strings)
                    values.append(value)
                    if value:
                        non_empty_cells += 1
                        if re.search(r"[A-Za-z]", value):
                            english_cells += 1
                        if re.search(r"[\u4e00-\u9fff]", value):
                            chinese_cells += 1
                if any(values):
                    non_empty_rows += 1

            sheets.append(
                {
                    "name": name,
                    "rows": non_empty_rows,
                    "max_columns": max_column,
                    "non_empty_cells": non_empty_cells,
                    "english_cells": english_cells,
                    "chinese_cells": chinese_cells,
                }
            )

    return {
        "workbook_file": "terminology workbook (.xlsx)",
        "terminology_workbook_sheets": len(sheets),
        "sheets": sheets,
    }


def build_category_metrics(visible_metrics: dict, workbook_metrics: dict) -> list[dict]:
    sheet_rows = {sheet["name"]: sheet["rows"] for sheet in workbook_metrics["sheets"]}
    return [
        {"label": "Dialogue / script", "count": visible_metrics["translated_entries"]},
        {"label": "Items / equipment", "count": sheet_rows.get("Items", 0) + sheet_rows.get("Equips", 0) - 2},
        {"label": "Skills / spells", "count": sheet_rows.get("Skill", 0) + sheet_rows.get("Spell", 0) - 2},
        {"label": "Quests / tasks", "count": sheet_rows.get("Task", 0) - 1},
        {"label": "UI / game strings", "count": sheet_rows.get("Gamestring", 0) - 2},
        {"label": "Scenes / locations", "count": sheet_rows.get("Sence", 0) - 1},
        {"label": "Script terminology", "count": sheet_rows.get("Terms in Script", 0) - 1},
    ]


def short_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        rounded = value / 1_000
        return f"{rounded:.0f}k" if rounded >= 100 else f"{rounded:.1f}k"
    return str(value)


def format_int(value: int) -> str:
    return f"{value:,}"


def svg_text(text: str) -> str:
    return html.escape(text, quote=True)


def build_svg(metrics: dict) -> str:
    categories = metrics["content_categories"]
    max_count = max(category["count"] for category in categories)

    width = 1120
    height = 760
    card_y = 128
    card_w = 238
    card_h = 106
    card_gap = 22
    left = 46

    metric_cards = [
        ("Translated entries", format_int(metrics["translated_entries"])),
        ("English words", f"{short_number(metrics['approx_english_words'])}+"),
        ("Script/source files", format_int(metrics["script_source_files"])),
        ("Workbook sheets", format_int(metrics["terminology_workbook_sheets"])),
    ]

    card_parts = []
    for index, (label, value) in enumerate(metric_cards):
        x = left + index * (card_w + card_gap)
        card_parts.append(
            f"""
  <rect x="{x}" y="{card_y}" width="{card_w}" height="{card_h}" rx="18" fill="#F8FAFC" stroke="#CBD5E1"/>
  <text x="{x + 22}" y="{card_y + 42}" class="metric">{svg_text(value)}</text>
  <text x="{x + 22}" y="{card_y + 76}" class="label">{svg_text(label)}</text>"""
        )

    bar_parts = []
    bar_x = 250
    bar_y = 306
    bar_max_w = 610
    bar_h = 22
    row_gap = 43
    for index, category in enumerate(categories):
        y = bar_y + index * row_gap
        bar_w = max(8, int(category["count"] / max_count * bar_max_w))
        label = category["label"]
        count = format_int(category["count"])
        bar_parts.append(
            f"""
  <text x="58" y="{y + 17}" class="bar-label">{svg_text(label)}</text>
  <rect x="{bar_x}" y="{y}" width="{bar_max_w}" height="{bar_h}" rx="11" fill="#E2E8F0"/>
  <rect x="{bar_x}" y="{y}" width="{bar_w}" height="{bar_h}" rx="11" fill="#2563EB"/>
  <text x="{bar_x + bar_max_w + 24}" y="{y + 17}" class="bar-count">{svg_text(count)}</text>"""
        )

    flow_parts = []
    flow_y = 650
    flow_items = [
        ("Chinese source", "#EFF6FF"),
        ("English localized text", "#ECFDF5"),
        ("Playable release", "#FFF7ED"),
    ]
    flow_x = 72
    flow_w = 278
    for index, (label, fill) in enumerate(flow_items):
        x = flow_x + index * 346
        flow_parts.append(
            f"""
  <rect x="{x}" y="{flow_y}" width="{flow_w}" height="54" rx="18" fill="{fill}" stroke="#CBD5E1"/>
  <text x="{x + flow_w / 2:.0f}" y="{flow_y + 34}" text-anchor="middle" class="flow">{svg_text(label)}</text>"""
        )
        if index < len(flow_items) - 1:
            ax = x + flow_w + 28
            flow_parts.append(
                f"""
  <path d="M {ax} {flow_y + 27} L {ax + 38} {flow_y + 27}" stroke="#64748B" stroke-width="3" stroke-linecap="round"/>
  <path d="M {ax + 38} {flow_y + 27} L {ax + 28} {flow_y + 18} M {ax + 38} {flow_y + 27} L {ax + 28} {flow_y + 36}" stroke="#64748B" stroke-width="3" stroke-linecap="round" fill="none"/>"""
            )

    generated = metrics["generated_from"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">PAL4 English Mod localization showcase</title>
  <desc id="desc">Summary metrics, content distribution, and workflow for the PAL4 English Mod translation project.</desc>
  <style>
    .title {{ font: 700 34px Arial, sans-serif; fill: #0F172A; }}
    .subtitle {{ font: 400 17px Arial, sans-serif; fill: #475569; }}
    .section {{ font: 700 22px Arial, sans-serif; fill: #0F172A; }}
    .metric {{ font: 700 33px Arial, sans-serif; fill: #1D4ED8; }}
    .label {{ font: 400 15px Arial, sans-serif; fill: #475569; }}
    .bar-label {{ font: 600 15px Arial, sans-serif; fill: #334155; }}
    .bar-count {{ font: 700 15px Arial, sans-serif; fill: #0F172A; }}
    .flow {{ font: 700 16px Arial, sans-serif; fill: #0F172A; }}
    .note {{ font: 400 12px Arial, sans-serif; fill: #64748B; }}
  </style>
  <rect width="{width}" height="{height}" fill="#FFFFFF"/>
  <text x="46" y="58" class="title">PAL4 English Mod: Localization Showcase</text>
  <text x="46" y="92" class="subtitle">A structured Chinese-to-English game localization project with glossary management, QA passes, and release packaging.</text>
  {''.join(card_parts)}
  <text x="46" y="282" class="section">Content coverage</text>
  {''.join(bar_parts)}
  <text x="46" y="620" class="section">Workflow</text>
  {''.join(flow_parts)}
  <text x="46" y="736" class="note">Generated from {svg_text(generated)}. English word count is approximate.</text>
</svg>
"""


def main() -> None:
    visible_metrics = load_visible_text_metrics()
    workbook_metrics = load_workbook_metrics()
    metrics = {
        **visible_metrics,
        **workbook_metrics,
    }
    metrics["content_categories"] = build_category_metrics(visible_metrics, workbook_metrics)
    metrics["generated_from"] = (
        "assets/pal4_visible_export.translated.csv and the terminology workbook in assets/"
    )

    METRICS_JSON.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    SHOWCASE_SVG.write_text(build_svg(metrics), encoding="utf-8")

    print(f"Wrote {METRICS_JSON.relative_to(ROOT)}")
    print(f"Wrote {SHOWCASE_SVG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
