"""System OCR with bounded transcript-cue timestamps. No LLM/image reader calls."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import media
from .store import Workspace, atomic_json, digest, now


class OcrUnavailable(RuntimeError):
    pass


def windows_ocr(image: Path | None = None, language: str = "") -> dict:
    if sys.platform != "win32":
        raise OcrUnavailable("Windows system OCR is unavailable on this OS")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
               str(Path(__file__).parent / "scripts/windows-ocr.ps1"), "-Language", language]
    command += ["-ImagePath", str(image.resolve())] if image else ["-Probe"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise OcrUnavailable("Windows OCR exceeded its operation deadline") from exc
    if result.returncode:
        raise OcrUnavailable("Windows OCR failed; check installed OCR languages and the local image")
    return json.loads(result.stdout.decode("utf-8-sig"))


def screen_cues(segments: list[dict], duration: float) -> list[dict]:
    cues = []
    for segment in segments:
        text = segment["text"]
        if re.search(r"\b(prompt|code|parameter)\b|提示词|代码|参数", text, re.I) and re.search(
                r"\b(screen|shown|copy|full|paste|below|here)\b|屏幕|显示|完整|复制|这段|如下|这里", text, re.I):
            at = round(min((segment["start"] + segment["end"]) / 2, duration - .01), 3)
            if at >= 0 and all(abs(c["at"] - at) >= 8 for c in cues):
                cues.append({"at": at, "segment_id": segment["id"], "reason": "explicit_screen_text_cue"})
    return cues


def extract_screen_text(ws: Workspace, run_id: str, source_id: str, cues: list[dict], *,
                        cookies=False, language="", max_images=3) -> dict:
    source = ws.source(source_id)
    if not cues:
        return {"status": "not_requested_no_cue", "items": [], "omitted_cues": 0}
    if not windows_ocr(language=language).get("available"):
        raise OcrUnavailable("Requested Windows OCR language is not installed")
    media.download_video(ws, run_id, source["url"], cookies, max_minutes=30)
    items = []
    for cue in cues[:max_images]:
        source = ws.source(source_id)
        frame = source.get("frames", {}).get(f"{cue['at']:.3f}")
        if not frame or frame.get("video_hash") != source["video_hash"] or not (ws.root / frame["path"]).is_file() or digest((ws.root / frame["path"]).read_bytes()) != frame["sha256"]:
            frame = media.frame(ws, source_id, cue["at"])
        output = ws.root / "private/raw" / source_id / f"ocr-{cue['at']:.3f}.json"
        existing = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        source = ws.source(source_id)
        if existing and existing.get("frame_sha256") == frame["sha256"] and existing.get("video_hash") == source["video_hash"] and existing.get("requested_language") == language:
            item = existing
        else:
            item = {**windows_ocr(ws.root / frame["path"], language), **cue,
                    "source_id": source_id, "frame_sha256": frame["sha256"], "video_hash": source["video_hash"],
                    "requested_language": language, "extracted_at": now(), "needs_review": True,
                    "citation": {"source_id": source_id, "frame_times": [cue["at"]]}}
            atomic_json(output, item)
        items.append(item)
    return {"status": "extracted_needs_review", "items": items, "omitted_cues": max(0, len(cues)-max_images),
            "coverage": "transcript_cue_stills_only; unmentioned/silent screen text may be missed"}
