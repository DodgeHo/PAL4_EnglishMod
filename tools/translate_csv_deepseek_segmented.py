#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

DEFAULT_API_KEY = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
DEFAULT_API_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass
class RowMeta:
    row_index: int
    line_start: int
    line_end: int


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


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_csv_with_line_meta(path: Path) -> tuple[list[str], list[dict[str, str]], list[RowMeta]]:
    rows: list[dict[str, str]] = []
    meta: list[RowMeta] = []

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])

        prev_end_line = 1  # header
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

    return True


def choose_segment(row: dict[str, str], meta: RowMeta, config: dict[str, Any]) -> str:
    # first-match wins
    for seg in config.get("segments", []):
        if row_matches_scope(row, meta, seg.get("scope", {})):
            return str(seg.get("name", "unnamed"))
    return "default"


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


def call_chat_once(client, model: str, system_message: str, user_message: str) -> tuple[str, ApiFeedback]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stream=False,
    )

    content = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    feedback = ApiFeedback(
        model=str(getattr(response, "model", "")),
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
    )
    return str(content), feedback


def generate_segment_prompt(client, model: str, target: str, requirement_text: str, segment_name: str) -> str:
    system_message = (
        "You are a localization prompt engineer. Build a strict translation instruction prompt. "
        "Output plain text only, no markdown, no JSON, no commentary."
    )
    user_message = (
        f"Target language: {target}\\n"
        f"Segment name: {segment_name}\\n"
        "User requirements (must be enforced):\\n"
        f"{requirement_text}\\n\\n"
        "Return a concise, imperative instruction prompt for a translation model. "
        "Must explicitly preserve placeholders/tokens: {NL} {CR} {TAB} __TERM_N__ "
        "{TAG_COLOUR_OPEN} {TAG_COLOUR_CLOSE} {TAG_DC0_OPEN} {TAG_DC0_CLOSE}."
    )

    content, _ = call_chat_once(client, model, system_message, user_message)
    return content.strip()


def call_translate_batch(
    client,
    model: str,
    source_items: List[str],
    target: str,
    segment_prompt: str,
) -> tuple[List[str], ApiFeedback]:
    system_message = (
        segment_prompt
        + "\\nReturn ONLY a JSON array of translated strings in exactly the same order and count as input."
    )
    user_message = (
        f"Target language: {target}\\n"
        "Input JSON array:\\n"
        + json.dumps(source_items, ensure_ascii=False)
    )

    content, feedback = call_chat_once(client, model, system_message, user_message)
    payload = parse_json_array_response(content)

    return [str(item) for item in payload], feedback


def parse_json_array_response(content: str) -> List[Any]:
    text = (content or "").strip()
    if not text:
        raise ValueError("Empty response content")

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_\-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        payload = json.loads(text)
    except Exception:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Failed to parse JSON array. Raw response: {content[:800]}")
        payload = json.loads(text[start : end + 1])

    if not isinstance(payload, list):
        raise ValueError(f"Response JSON is not an array. Raw response: {content[:800]}")

    return payload


def translate_single_resilient(
    client,
    model: str,
    source: str,
    target: str,
    segment_prompt: str,
    log_path: Path,
    seg_name: str,
    item_label: str,
) -> str:
    stricter_prompt = (
        segment_prompt
        + "\nReturn ONLY a valid JSON array with exactly one translated string. "
          "No markdown fences, no explanation, no extra text outside the array."
    )
    try:
        out, fb = call_translate_batch(client, model, [source], target, stricter_prompt)
        result = out[0] if out else source
        append_log(log_path, f"SEG_SINGLE_OK {seg_name} {item_label}: tokens={fb.total_tokens}")
        return result if result.strip() else source
    except Exception as exc:
        append_log(log_path, f"SEG_SINGLE_FAIL {seg_name} {item_label}: {exc}")
        print(f"  single fallback failed {seg_name} {item_label}, keeping source")
        return source


def translate_batch_resilient(
    client,
    model: str,
    source_items: List[str],
    target: str,
    segment_prompt: str,
    log_path: Path,
    seg_name: str,
    batch_label: str,
) -> List[str]:
    try:
        out, fb = call_translate_batch(client, model, source_items, target, segment_prompt)
        append_log(
            log_path,
            f"SEG_BATCH_OK {seg_name} {batch_label}: in={len(source_items)} out={len(out)} tokens={fb.total_tokens}",
        )
        if len(out) < len(source_items):
            out.extend([""] * (len(source_items) - len(out)))
        elif len(out) > len(source_items):
            out = out[: len(source_items)]
        return out
    except Exception as exc:
        append_log(
            log_path,
            f"SEG_BATCH_FAIL {seg_name} {batch_label}: in={len(source_items)} err={exc}",
        )
        print(f"  batch failed {seg_name} {batch_label} ({len(source_items)} items) -> retrying one by one")

    # Flat fallback: retry each item individually (no recursion)
    results: List[str] = []
    for k, src in enumerate(source_items):
        results.append(
            translate_single_resilient(
                client, model, src, target, segment_prompt, log_path, seg_name, f"{batch_label}.i{k+1}"
            )
        )
    return results


def load_checkpoint(path: Path) -> Dict[str, str]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_checkpoint(path: Path, data: Dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=None)


def chunk_texts(items: List[str], max_batch_chars: int) -> List[List[str]]:
    batches: List[List[str]] = []
    current: List[str] = []
    chars = 0

    for item in items:
        item_len = len(item)
        if current and chars + item_len > max_batch_chars:
            batches.append(current)
            current = []
            chars = 0
        current.append(item)
        chars += item_len

    if current:
        batches.append(current)

    return batches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Segment-aware DeepSeek CSV translator: requirements -> prompt -> translation"
    )
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", help="Output CSV path")
    parser.add_argument("--inplace", action="store_true", help="Overwrite input CSV directly")
    parser.add_argument("--config", required=True, help="Segment config JSON path")
    parser.add_argument("--target", default="en", help="Target language")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name")
    parser.add_argument("--max-batch-chars", type=int, default=1500, help="Approximate chars per translation batch")
    parser.add_argument("--force", action="store_true", help="Retranslate rows with existing translation")
    parser.add_argument("--dry-run", action="store_true", help="Only classify rows and generate prompts; no translation output")
    parser.add_argument("--log", help="Optional log path")
    parser.add_argument("--checkpoint", help="Checkpoint JSON path for resume support (default: <output>.checkpoint.json)")
    parser.add_argument("--segment", help="Only translate rows assigned to this segment name (skip all other segments)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if args.inplace and args.output:
        raise ValueError("Use either --inplace or --output, not both.")

    if args.inplace:
        output_path = input_path
    elif args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(f"{input_path.stem}.segmented{input_path.suffix}")

    config_path = Path(args.config)

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"Config JSON not found: {config_path}")

    api_url = os.environ.get("DEEPSEEK_API_URL") or DEFAULT_API_URL
    api_key = os.environ.get("DEEPSEEK_API_KEY") or DEFAULT_API_KEY

    log_path = Path(args.log) if args.log else output_path.with_suffix(output_path.suffix + f".seglog.{int(time.time())}.txt")
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else output_path.with_suffix(".checkpoint.json")

    config = load_json(config_path)
    fieldnames, rows, metas = parse_csv_with_line_meta(input_path)
    if "translation" not in fieldnames:
        fieldnames.append("translation")

    # Build requirement map
    segment_requirements: Dict[str, str] = {
        "default": str(config.get("default", {}).get("requirements", "Translate naturally and consistently."))
    }
    for seg in config.get("segments", []):
        name = str(seg.get("name", "unnamed"))
        req = str(seg.get("requirements", "Translate naturally and consistently."))
        segment_requirements[name] = req

    # Assign rows to segments
    row_segment: List[str] = []
    segment_row_count: Dict[str, int] = {}
    for row, meta in zip(rows, metas):
        seg_name = choose_segment(row, meta, config)
        row_segment.append(seg_name)
        segment_row_count[seg_name] = segment_row_count.get(seg_name, 0) + 1

    append_log(log_path, f"INPUT={input_path}")
    append_log(log_path, f"OUTPUT={output_path}")
    append_log(log_path, f"CONFIG={config_path}")
    append_log(log_path, f"ROWS={len(rows)}")
    append_log(log_path, f"SEGMENT_ROWS={segment_row_count}")

    print(f"Input rows: {len(rows)}")
    print("Segment row counts:")
    for name, cnt in sorted(segment_row_count.items()):
        print(f"  {name}: {cnt}")

    client = None
    if api_key:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError("Missing dependency: openai. Install with: pip install openai") from exc
        client = OpenAI(api_key=api_key, base_url=api_url)
    elif not args.dry_run:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    # Step 1: requirements -> prompt (one prompt per segment)
    segment_prompts: Dict[str, str] = {}
    for seg_name, req_text in segment_requirements.items():
        if client is None:
            # Dry-run without API key: use requirements directly as fallback prompt.
            prompt = req_text
        else:
            prompt = generate_segment_prompt(client, args.model, args.target, req_text, seg_name)
        segment_prompts[seg_name] = prompt
        append_log(log_path, f"SEG_PROMPT[{seg_name}]={prompt}")

    print("Generated segment prompts:")
    for seg_name in sorted(segment_prompts):
        short = segment_prompts[seg_name].replace("\n", " ")
        if len(short) > 140:
            short = short[:140] + "..."
        print(f"  {seg_name}: {short}")

    if args.dry_run:
        if client is None:
            print("DEEPSEEK_API_KEY not set; dry-run used raw requirements as fallback prompts.")
        print("Dry-run enabled, no translation performed.")
        print(f"Log: {log_path}")
        return 0

    # Step 2: translate by segment with unique-text dedupe inside each segment
    # Load checkpoint so interrupted runs resume without re-translating completed items
    checkpoint: Dict[str, str] = load_checkpoint(checkpoint_path)
    checkpoint_loaded = len(checkpoint)
    if checkpoint_loaded:
        print(f"Resumed from checkpoint: {checkpoint_loaded} items already translated ({checkpoint_path})")
        append_log(log_path, f"CHECKPOINT_LOAD={checkpoint_loaded} path={checkpoint_path}")

    only_segment = args.segment or None
    if only_segment:
        print(f"--segment filter active: only translating '{only_segment}' rows")

    segment_unique_texts: Dict[str, List[str]] = {}
    for row, seg_name in zip(rows, row_segment):
        if only_segment and seg_name != only_segment:
            continue
        source = (row.get("original_text") or "").strip()
        existing = (row.get("translation") or "").strip()
        if not source:
            continue
        if source in checkpoint:
            continue
        if existing and not args.force:
            continue

        segment_unique_texts.setdefault(seg_name, [])
        if source not in segment_unique_texts[seg_name]:
            segment_unique_texts[seg_name].append(source)

    total_unique = sum(len(v) for v in segment_unique_texts.values())
    print(f"Items to translate: {total_unique} (skipped from checkpoint: {checkpoint_loaded})")

    translated_by_segment: Dict[str, Dict[str, str]] = {}
    for seg_name, sources in segment_unique_texts.items():
        if not sources:
            translated_by_segment[seg_name] = {}
            continue

        prompt = segment_prompts.get(seg_name, segment_prompts["default"])
        translated_by_segment[seg_name] = {}

        # protect tags/tokens before sending to model
        protected_pairs: List[Tuple[str, str, Dict[str, str]]] = []
        for src in sources:
            protected, repl = protect_tokens(src)
            protected_pairs.append((src, protected, repl))

        batches = chunk_texts([p[1] for p in protected_pairs], args.max_batch_chars)
        cursor = 0
        total_batches = len(batches)
        print(f"Segment {seg_name}: unique={len(sources)}, batches={total_batches}")

        for batch_i, batch in enumerate(batches, start=1):
            out = translate_batch_resilient(
                client,
                args.model,
                batch,
                args.target,
                prompt,
                log_path,
                seg_name,
                f"#{batch_i}",
            )

            for j, translated in enumerate(out):
                src, _, repl = protected_pairs[cursor + j]
                restored = restore_tokens(translated, repl)
                result = restored if restored.strip() else src
                translated_by_segment[seg_name][src] = result
                checkpoint[src] = result

            cursor += len(batch)

            # Persist checkpoint after every batch so Ctrl+C can resume
            save_checkpoint(checkpoint_path, checkpoint)
            print(f"  [{seg_name}] batch {batch_i}/{total_batches} done, checkpoint saved")

    # write rows
    for i, row in enumerate(rows):
        seg_name = row_segment[i]
        if only_segment and seg_name != only_segment:
            continue
        source = (row.get("original_text") or "").strip()
        existing = (row.get("translation") or "").strip()
        if not source:
            continue
        if existing and not args.force:
            continue
        # prefer segment result, then checkpoint (from previous run), then existing
        result = (
            translated_by_segment.get(seg_name, {}).get(source)
            or checkpoint.get(source)
            or existing
        )
        row["translation"] = result

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Clean up checkpoint on successful completion
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        print(f"Checkpoint deleted (run completed successfully)")

    append_log(log_path, "DONE")
    print(f"Wrote: {output_path}")
    print(f"Log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
