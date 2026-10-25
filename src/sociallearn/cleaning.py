"""Mechanical cleanup only; source records remain unchanged on disk."""
from __future__ import annotations

import html
import re


NO_SPEECH = re.compile(r"^\s*[\[(（【]\s*(music|applause|silence|laughter|音乐|掌声|静音|笑声)\s*[\])）】]\s*$", re.I)


def clean_text(text: str, *, captions=False) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if captions:
        text = html.unescape(re.sub(r"</?(?:c(?:\.[\w-]+)?|v|b|i|u)(?:\s+[^>]*)?>", "", text))
    # Do not strip punctuation, code indentation, literal brackets or internal newlines.
    return "\n".join(line for line in text.split("\n") if not NO_SPEECH.fullmatch(line)).strip("\n")


def compact_segments(segments: list[dict], origin="") -> tuple[list[dict], dict]:
    cleaned = []
    removed_empty = merged_duplicates = 0
    for segment in segments:
        text = clean_text(segment["text"], captions=origin in ("manual", "automatic", "platform_page_track"))
        if not text.strip():
            removed_empty += 1
            continue
        timed = "start" in segment
        adjacent = bool(cleaned) and ((timed and "end" in cleaned[-1] and segment["start"] <= cleaned[-1]["end"] + .5)
                                     or (not timed and "start" not in cleaned[-1]))
        if adjacent and cleaned[-1]["text"] == text:
            if timed:
                cleaned[-1]["end"] = max(cleaned[-1]["end"], segment["end"])
            cleaned[-1]["segment_ids"].append(segment["id"])
            merged_duplicates += 1
        else:
            cleaned.append({"segment_ids": [segment["id"]], "text": text,
                            **{k: segment[k] for k in ("start", "end") if k in segment}})
    raw_bytes = len("\n".join(s["text"] for s in segments).encode("utf-8"))
    clean_bytes = len("\n".join(s["text"] for s in cleaned).encode("utf-8"))
    return cleaned, {"input_segments": len(segments), "output_segments": len(cleaned), "empty_or_background_only_removed": removed_empty,
                     "adjacent_exact_duplicates_merged": merged_duplicates, "raw_text_bytes": raw_bytes,
                     "clean_text_bytes": clean_bytes, "measurement": "UTF-8 text bytes; not model tokens"}


def compact_ocr(screen_text: dict) -> dict:
    items, seen = [], set()
    for item in screen_text.get("items", []):
        text = clean_text(item["text"])
        if not text.strip():
            continue
        if text in seen:
            next(x for x in items if x["text"] == text)["frame_times"].append(item["at"])
            continue
        seen.add(text)
        items.append({"frame_times": [item["at"]], "text": text, "needs_review": True})
    result = {"status": screen_text["status"], "items": items}
    for key in ("reason", "omitted_cues", "coverage"):
        if screen_text.get(key):
            result[key] = screen_text[key]
    return result
