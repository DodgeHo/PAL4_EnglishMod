#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


DEFAULT_API_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
DEFAULT_API_URL = "https://api.deepseek.com"
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass
class ApiFeedback:
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def append_log(log_path: Path, message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{ts}] {message}\n")


def load_term_whitelist(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [field.lower() for field in (reader.fieldnames or [])]
        if "source" not in fields or "target" not in fields:
            raise ValueError("Whitelist CSV must contain source,target columns")

        source_key = reader.fieldnames[fields.index("source")]
        target_key = reader.fieldnames[fields.index("target")]
        mapping: Dict[str, str] = {}
        for row in reader:
            source = (row.get(source_key) or "").strip()
            target = (row.get(target_key) or "").strip()
            if source and target:
                mapping[source] = target
        return mapping


def choose_non_overlapping_terms(source: str, known_terms: List[str]) -> List[Tuple[int, int, str]]:
    candidates: List[Tuple[int, int, str]] = []
    for term in known_terms:
        start = source.find(term)
        while start != -1:
            end = start + len(term)
            candidates.append((start, end, term))
            start = source.find(term, start + 1)

    candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
    selected: List[Tuple[int, int, str]] = []
    occupied = [False] * max(1, len(source))
    for start, end, term in candidates:
        if any(occupied[index] for index in range(start, end)):
            continue
        for index in range(start, end):
            occupied[index] = True
        selected.append((start, end, term))
    selected.sort(key=lambda item: item[0])
    return selected


def protect_tokens(text: str) -> tuple[str, Dict[str, str]]:
    replacements = {
        "\\n": "{NL}",
        "\\r": "{CR}",
        "\\t": "{TAB}",
        "<colour": "{TAG_COLOUR_OPEN}",
        "</colour>": "{TAG_COLOUR_CLOSE}",
        "<dc0>": "{TAG_DC0_OPEN}",
        "</dc0>": "{TAG_DC0_CLOSE}",
    }
    protected = text
    for original, token in replacements.items():
        protected = protected.replace(original, token)
    return protected, replacements


def restore_tokens(text: str, replacements: Dict[str, str]) -> str:
    restored = text
    for original, token in replacements.items():
        restored = restored.replace(token, original)
    return restored


def mark_text_with_terms(source: str, glossary: Dict[str, str]) -> tuple[str, Dict[str, str]]:
    terms = [term for term in glossary if term in source and len(term) >= 2]
    matches = choose_non_overlapping_terms(source, terms)
    if not matches:
        return source, {}

    parts: List[str] = []
    marker_map: Dict[str, str] = {}
    cursor = 0
    marker_index = 1
    for start, end, term in matches:
        if start > cursor:
            parts.append(source[cursor:start])
        marker = f"__TERM_{marker_index}__"
        marker_index += 1
        parts.append(marker)
        marker_map[marker] = glossary[term]
        cursor = end
    if cursor < len(source):
        parts.append(source[cursor:])

    return "".join(parts), marker_map


def restore_markers(text: str, marker_map: Dict[str, str]) -> str:
    restored = text
    for marker, value in marker_map.items():
        restored = restored.replace(marker, value)
    return restored


def call_deepseek_batch(client, source_items: List[str], target: str, mode: str) -> tuple[List[str], ApiFeedback]:
    if mode == "translate":
        system_message = (
            "You are a professional game localization translator. "
            "Translate each source string into natural, idiomatic target-language text for PAL4. "
            "Keep placeholders and tokens unchanged, including {NL} {CR} {TAB}, __TERM_N__, "
            "{TAG_COLOUR_OPEN}, {TAG_COLOUR_CLOSE}, {TAG_DC0_OPEN}, and {TAG_DC0_CLOSE}. "
            "Use Pinyin for Chinese names when possible. "
            "Do not add extra notes or explanations."
        )
        user_message = (
            f"Target language: {target}\n"
            "Return ONLY a valid JSON array of translated strings in the same order and same length as input.\n"
            "Input JSON array:\n"
            + json.dumps(source_items, ensure_ascii=False)
        )
    elif mode == "shorten":
        system_message = (
            "You are a localization post-editor. "
            "Rewrite each string to concise target-language text while preserving meaning and all placeholders/tokens exactly. "
            "Hard requirements: (1) no Chinese characters; (2) each string must be shorter than 700 characters; "
            "(3) keep {NL} {CR} {TAB}, __TERM_N__, {TAG_COLOUR_OPEN}, {TAG_COLOUR_CLOSE}, {TAG_DC0_OPEN}, {TAG_DC0_CLOSE} unchanged."
        )
        user_message = (
            f"Target language: {target}\n"
            "Return ONLY a valid JSON array with same order and same length as input.\n"
            "If an item is still too long, aggressively summarize it while preserving key gameplay information.\n"
            "Input JSON array:\n"
            + json.dumps(source_items, ensure_ascii=False)
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stream=False,
    )

    content = response.choices[0].message.content
    try:
        payload = json.loads(content)
    except Exception:
        start = content.find("[")
        end = content.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Failed to parse JSON array. Raw response: {content}")
        payload = json.loads(content[start : end + 1])

    usage = getattr(response, "usage", None)
    feedback = ApiFeedback(
        model=str(getattr(response, "model", "")),
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
    )
    return [str(item) for item in payload], feedback


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def violation_reason(text: str, max_chars: int) -> str:
    if has_cjk(text):
        return "contains_chinese"
    if len(text or "") >= max_chars:
        return f"length_ge_{max_chars}"
    return ""


def translate_unique_texts(
    unique_texts: List[str],
    target: str,
    api_url: str,
    api_key: str,
    log_path: Path,
    max_batch_chars: int,
    seed_glossary: Dict[str, str],
) -> Dict[str, str]:
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("Missing dependency: openai. Install with: pip install openai") from exc

    client = OpenAI(api_key=api_key, base_url=api_url)
    glossary = dict(seed_glossary)
    pending = sorted([text for text in unique_texts if text not in glossary], key=lambda item: (len(item), item))

    if not pending:
        return glossary

    total_pending = len(pending)
    index = 0
    batch_no = 0
    while index < len(pending):
        batch_start = index
        batch_texts: List[str] = []
        batch_meta: List[tuple[str, Dict[str, str], Dict[str, str]]] = []
        total_chars = 0

        while index < len(pending):
            source = pending[index]
            protected, replacements = protect_tokens(source)
            marked, marker_map = mark_text_with_terms(protected, glossary)
            if batch_texts and total_chars + len(marked) > max_batch_chars:
                break
            batch_texts.append(marked)
            batch_meta.append((source, replacements, marker_map))
            total_chars += len(marked)
            index += 1

        batch_no += 1
        start_n = batch_start + 1
        end_n = index
        print(f"[Translate Batch {batch_no}] unique {start_n}-{end_n}/{total_pending}, items={len(batch_texts)}")

        try:
            out, feedback = call_deepseek_batch(client, batch_texts, target, mode="translate")
            append_log(
                log_path,
                f"TRANSLATE_API {start_n}-{end_n}: model={feedback.model}, total_tokens={feedback.total_tokens}",
            )
        except Exception as exc:
            append_log(log_path, f"TRANSLATE_ERROR {start_n}-{end_n}: {exc}")
            out = [""] * len(batch_texts)

        if len(out) != len(batch_texts):
            if len(out) < len(batch_texts):
                out.extend([""] * (len(batch_texts) - len(out)))
            else:
                out = out[: len(batch_texts)]

        for translated, (source, replacements, marker_map) in zip(out, batch_meta):
            restored = restore_tokens(restore_markers(translated, marker_map), replacements)
            glossary[source] = restored if restored.strip() else source

    return glossary


def second_pass_shorten(
    translated_map: Dict[str, str],
    target: str,
    api_url: str,
    api_key: str,
    log_path: Path,
    max_batch_chars: int,
    max_chars: int,
) -> tuple[int, int]:
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("Missing dependency: openai. Install with: pip install openai") from exc

    client = OpenAI(api_key=api_key, base_url=api_url)

    failing_keys = [k for k, v in translated_map.items() if violation_reason(v, max_chars)]
    if not failing_keys:
        return 0, 0

    print(f"Second pass needed: {len(failing_keys)} unique text(s)")
    append_log(log_path, f"SECOND_PASS_INPUT={len(failing_keys)}")

    index = 0
    updated = 0
    while index < len(failing_keys):
        batch_keys: List[str] = []
        batch_inputs: List[str] = []
        chars = 0
        while index < len(failing_keys):
            key = failing_keys[index]
            val = translated_map.get(key, "")
            if batch_inputs and chars + len(val) > max_batch_chars:
                break
            batch_keys.append(key)
            batch_inputs.append(val)
            chars += len(val)
            index += 1

        try:
            out, feedback = call_deepseek_batch(client, batch_inputs, target, mode="shorten")
            append_log(log_path, f"SECOND_PASS_API items={len(batch_inputs)} tokens={feedback.total_tokens}")
        except Exception as exc:
            append_log(log_path, f"SECOND_PASS_ERROR items={len(batch_inputs)} err={exc}")
            out = batch_inputs

        if len(out) != len(batch_inputs):
            if len(out) < len(batch_inputs):
                out.extend(batch_inputs[len(out) :])
            else:
                out = out[: len(batch_inputs)]

        for key, new_text in zip(batch_keys, out):
            if new_text.strip():
                translated_map[key] = new_text
                updated += 1

    remain = sum(1 for v in translated_map.values() if violation_reason(v, max_chars))
    append_log(log_path, f"SECOND_PASS_UPDATED={updated}")
    append_log(log_path, f"SECOND_PASS_REMAIN={remain}")
    return updated, remain


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Translate CSV with DeepSeek, enforce no-Chinese and length < 700 by second-pass shortening"
    )
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", help="Output CSV path")
    parser.add_argument("--inplace", action="store_true", help="Overwrite input CSV")
    parser.add_argument("--target", default="en", help="Target language")
    parser.add_argument("--force", action="store_true", help="Retranslate rows with existing translation")
    parser.add_argument("--whitelist", default="tools/term_whitelist.csv", help="Term whitelist CSV")
    parser.add_argument("--max-batch-chars", type=int, default=1800, help="Max chars for first-pass batches")
    parser.add_argument("--second-pass-max-batch-chars", type=int, default=2200, help="Max chars for second-pass batches")
    parser.add_argument("--max-chars", type=int, default=700, help="Translation must be shorter than this value")
    parser.add_argument("--log", help="Optional log path")
    args = parser.parse_args()

    input_path = Path(args.input)
    if args.inplace and args.output:
        raise ValueError("Use either --inplace or --output, not both")
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    if args.inplace:
        output_path = input_path
    elif args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(f"{input_path.stem}.translated.short700{input_path.suffix}")

    api_url = os.environ.get("DEEPSEEK_API_URL") or DEFAULT_API_URL
    api_key = os.environ.get("DEEPSEEK_API_KEY") or DEFAULT_API_KEY

    whitelist = load_term_whitelist(Path(args.whitelist))
    log_path = Path(args.log) if args.log else output_path.with_suffix(output_path.suffix + f".translate.log.{int(time.time())}.txt")

    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if "translation" not in fieldnames:
        fieldnames.append("translation")

    unique_texts: List[str] = []
    seen: set[str] = set()
    for row in rows:
        original = (row.get("original_text") or "").strip()
        existing = (row.get("translation") or "").strip()
        if not original:
            continue
        if existing and not args.force:
            continue
        if original not in seen:
            seen.add(original)
            unique_texts.append(original)

    print(f"Input: {input_path}")
    print(f"Output: {output_path}")
    print(f"Rows: {len(rows)}, unique to translate: {len(unique_texts)}")
    append_log(log_path, f"INPUT={input_path}")
    append_log(log_path, f"OUTPUT={output_path}")
    append_log(log_path, f"TARGET={args.target}")
    append_log(log_path, f"MAX_CHARS={args.max_chars}")

    if unique_texts:
        translated_map = translate_unique_texts(
            unique_texts=unique_texts,
            target=args.target,
            api_url=api_url,
            api_key=api_key,
            log_path=log_path,
            max_batch_chars=args.max_batch_chars,
            seed_glossary=whitelist,
        )

        second_updated, remain = second_pass_shorten(
            translated_map=translated_map,
            target=args.target,
            api_url=api_url,
            api_key=api_key,
            log_path=log_path,
            max_batch_chars=args.second_pass_max_batch_chars,
            max_chars=args.max_chars,
        )

        for row in rows:
            original = (row.get("original_text") or "").strip()
            existing = (row.get("translation") or "").strip()
            if not original:
                continue
            if existing and not args.force:
                continue
            row["translation"] = translated_map.get(original, existing)

        violations = 0
        for row in rows:
            text = row.get("translation") or ""
            if violation_reason(text, args.max_chars):
                violations += 1

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"Second-pass rewritten: {second_updated}")
        print(f"Remaining violations after second pass: {remain}")
        print(f"Rows with violation in output: {violations}")
        print(f"Wrote: {output_path}")
        print(f"Log: {log_path}")
        append_log(log_path, f"SECOND_PASS_UPDATED={second_updated}")
        append_log(log_path, f"REMAIN_VIOLATIONS={remain}")
        append_log(log_path, f"ROW_VIOLATIONS={violations}")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print("No rows need translation.")
        print(f"Wrote: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
