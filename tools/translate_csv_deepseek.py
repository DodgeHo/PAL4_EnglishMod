#!/usr/bin/env python3
import argparse
import csv
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


DEFAULT_API_KEY = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
DEFAULT_API_URL = 'https://api.deepseek.com'



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


def call_deepseek_batch(client, source_items: List[str], target: str) -> tuple[List[str], ApiFeedback]:
    system_message = (
        "You are a professional game localization translator. "
        "Translate each source string into natural, idiomatic target-language text for PAL4. "
        "Keep placeholders and tokens unchanged, including {NL} {CR} {TAB}, __TERM_N__, "
        "{TAG_COLOUR_OPEN}, {TAG_COLOUR_CLOSE}, {TAG_DC0_OPEN}, and {TAG_DC0_CLOSE}. "
        "Follow these style rules: "
        "(1) Keep key gameplay info at the beginning (target, effect, cost). "
        "(2) You may expand wording if needed, but keep total length as close as possible to source. "
        "(3) For short skill descriptions, prefer compact format like 'Hit enemy with ice, cost MP 15; ice damage (single)'. "
        "(4) Use Pinyin for Chinese personal names when possible, for example 慕容紫英 -> Murong Ziying. "
        "(5) Use terminology conventions: 冰咒 -> Ice Spell; 止血草 -> Healing Herb (HP); 精 -> HP; 气 -> TP; 神 -> MP; 精气神 -> HP/TP/MP. "
        "(6) Keep item/skill names consistent across rows. "
        "(7) Do not add extra notes, brackets, or explanations unless present in source."
    )
    user_message = (
        f"Target language: {target}\n"
        "Return ONLY a valid JSON array of translated strings in the same order and same length as input.\n"
        "Input JSON array:\n"
        + json.dumps(source_items, ensure_ascii=False)
    )

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
    total_batches = 0
    while index < len(pending):
        batch_start_time = time.time()
        batch_start_index = index
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

        total_batches += 1
        start_n = batch_start_index + 1
        end_n = index

        append_log(
            log_path,
            f"BATCH {start_n}-{end_n}: count={len(batch_texts)}, chars={total_chars}",
        )
        print(
            f"[Batch {total_batches}] unique {start_n}-{end_n}/{total_pending}, "
            f"items={len(batch_texts)}, chars={total_chars}"
        )

        try:
            translations, feedback = call_deepseek_batch(client, batch_texts, target)
            elapsed = time.time() - batch_start_time
            append_log(
                log_path,
                (
                    f"BATCH_API {start_n}-{end_n}: model={feedback.model}, "
                    f"prompt={feedback.prompt_tokens}, completion={feedback.completion_tokens}, "
                    f"total={feedback.total_tokens}, sec={elapsed:.2f}"
                ),
            )
            print(
                (
                    f"  API ok model={feedback.model or 'unknown'} "
                    f"tokens(p/c/t)={feedback.prompt_tokens}/{feedback.completion_tokens}/{feedback.total_tokens} "
                    f"time={elapsed:.2f}s"
                )
            )
        except Exception as exc:
            append_log(log_path, f"BATCH_ERROR {start_n}-{end_n}: {exc}")
            print(f"  API batch failed ({start_n}-{end_n}): {exc}")

            translations = []
            for item_index, marked in enumerate(batch_texts):
                item_n = start_n + item_index
                try:
                    single_out, single_feedback = call_deepseek_batch(client, [marked], target)
                    translations.append(single_out[0])
                    append_log(log_path, f"SINGLE_OK item={item_n}")
                    append_log(
                        log_path,
                        (
                            f"SINGLE_API item={item_n}: model={single_feedback.model}, "
                            f"prompt={single_feedback.prompt_tokens}, completion={single_feedback.completion_tokens}, "
                            f"total={single_feedback.total_tokens}"
                        ),
                    )
                    print(
                        (
                            f"  single retry ok item={item_n}/{total_pending}, "
                            f"tokens={single_feedback.total_tokens}"
                        )
                    )
                except Exception as exc2:
                    translations.append("")
                    append_log(log_path, f"SINGLE_ERROR item={item_n}: {exc2}")
                    print(f"  single retry failed item={item_n}/{total_pending}: {exc2}")

        if len(translations) != len(batch_texts):
            append_log(log_path, f"BATCH_LEN_MISMATCH expected={len(batch_texts)} got={len(translations)}")
            if len(translations) < len(batch_texts):
                translations.extend([""] * (len(batch_texts) - len(translations)))
            else:
                translations = translations[: len(batch_texts)]

        for translated, (source, replacements, marker_map) in zip(translations, batch_meta):
            restored = restore_tokens(restore_markers(translated, marker_map), replacements)
            final_text = restored if restored.strip() else source
            glossary[source] = final_text

        progress = (end_n / total_pending) * 100 if total_pending else 100.0
        print(f"  progress {end_n}/{total_pending} unique ({progress:.1f}%)")

    return glossary


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate a PAL4 visible-text CSV using DeepSeek")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", help="Output CSV path")
    parser.add_argument("--inplace", action="store_true", help="Overwrite the input CSV")
    parser.add_argument("--target", default="en", help="Target language code or name")
    parser.add_argument("--force", action="store_true", help="Retranslate rows with existing translation values")
    parser.add_argument("--whitelist", default="tools/term_whitelist.csv", help="Term whitelist CSV path")
    parser.add_argument("--max-batch-chars", type=int, default=1800, help="Approximate max chars per API batch")
    parser.add_argument("--log", help="Optional log path")
    args = parser.parse_args()

    input_path = Path(args.input)
    if args.inplace and args.output:
        raise ValueError("Use either --inplace or --output, not both.")

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    if args.inplace:
        output_path = input_path
    elif args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(f"{input_path.stem}.translated{input_path.suffix}")

    api_url = os.environ.get("DEEPSEEK_API_URL") or DEFAULT_API_URL
    api_key = os.environ.get("DEEPSEEK_API_KEY") or DEFAULT_API_KEY
    if not os.environ.get('DEEPSEEK_API_URL'):
        print('DEEPSEEK_API_URL not set; using embedded default URL.')
    if not os.environ.get('DEEPSEEK_API_KEY'):
        print('DEEPSEEK_API_KEY not set; using embedded default API key.')

    whitelist = load_term_whitelist(Path(args.whitelist))
    log_path = Path(args.log) if args.log else output_path.with_suffix(output_path.suffix + f".translate.log.{int(time.time())}.txt")

    append_log(log_path, f"INPUT={input_path}")
    append_log(log_path, f"OUTPUT={output_path}")
    append_log(log_path, f"TARGET={args.target}")
    append_log(log_path, f"WHITELIST={Path(args.whitelist)}")
    append_log(log_path, f"WHITELIST_TERMS={len(whitelist)}")

    print(f"Input: {input_path}")
    print(f"Output: {output_path}")
    print(f"Target language: {args.target}")
    print(f"Whitelist: {Path(args.whitelist)} ({len(whitelist)} terms)")

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

    append_log(log_path, f"TOTAL_ROWS={len(rows)}")
    append_log(log_path, f"UNIQUE_TO_TRANSLATE={len(unique_texts)}")
    append_log(log_path, f"UNIQUE_PRESEEDED={sum(1 for text in unique_texts if text in whitelist)}")

    print(f"CSV rows total: {len(rows)}")
    print(f"Unique source texts selected: {len(unique_texts)}")

    if not unique_texts:
        print("No rows need translation")
        append_log(log_path, "NO_WORK")
        print(f"Log: {log_path}")
        return 0

    translated_map = translate_unique_texts(
        unique_texts=unique_texts,
        target=args.target,
        api_url=api_url,
        api_key=api_key,
        log_path=log_path,
        max_batch_chars=args.max_batch_chars,
        seed_glossary=whitelist,
    )

    for row in rows:
        original = (row.get("original_text") or "").strip()
        existing = (row.get("translation") or "").strip()
        if not original:
            continue
        if existing and not args.force:
            continue
        row["translation"] = translated_map.get(original, existing)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    append_log(log_path, f"DONE translated_unique={len(translated_map)}")
    print(f"Wrote translated CSV to {output_path}")
    print(f"Log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())