"""Bounded topic acquisition. LLM reasoning and wiki admission happen afterwards."""
from __future__ import annotations

import json
import math
import uuid
from collections import Counter
from pathlib import Path

from . import cleaning, media, ocr, screening
from .store import Workspace, atomic_json, digest, now


def collect_topic(ws: Workspace, topic: str, *, run_id=None, platform="youtube", limit=12,
                  search_limit=30, max_minutes=20, total_minutes=180, cookies=False,
                  model="small", ocr_mode="auto", ocr_language="", max_images=3,
                  kind="auto", policy=None, include_text=False, max_per_creator=2) -> dict:
    if not topic.strip() or len(topic) > 400:
        raise ValueError("Topic must be nonempty and at most 400 characters")
    if platform not in ("youtube", "bilibili") or kind not in ("auto", "principle", "implementation", "model", "trend"):
        raise ValueError("Unsupported platform or knowledge kind")
    if not 1 <= limit <= search_limit <= 30 or not 1 <= max_minutes <= 30 or not 1 <= total_minutes <= 240 or not 0 <= max_images <= 3 or not 1 <= max_per_creator <= 12:
        raise ValueError("Use 1..30 candidates, bounded 1..30 minute videos, <=240 total minutes, <=3 OCR images and 1..12 videos per creator")
    if model not in ("tiny", "base", "small") or ocr_mode not in ("auto", "off"):
        raise ValueError("Unsupported ASR model or OCR mode")
    if cookies and run_id is None:
        raise PermissionError("Cookie use needs an existing run with explicit human consent")
    policy = screening.policy_config(policy)
    kind = screening.infer_kind(topic) if kind == "auto" else kind
    owned_run = run_id is None
    run_id = run_id or ws.create_run(topic)["id"]
    batch_id = uuid.uuid4().hex[:12]
    folder = ws.root / "state/collections" / batch_id
    report = {"id": batch_id, "run_id": run_id, "topic": topic, "platform": platform, "kind": kind,
              "created_at": now(), "status": "collecting", "policy": policy, "candidates": [], "selected": [],
              "processed_seconds": 0, "ocr_images": 0, "ocr_budget_reserved": 0, "llm_calls": 0,
              "limits": "Heuristic screening, not proven depth/originality. Model claims still require live official verification."}
    manifest = folder / "manifest.json"
    selected_texts = []
    seen_text = {}
    creators = Counter()
    atomic_json(manifest, report)
    try:
        ws.check_access(run_id, platform)
        if cookies:
            ws.require_consent(run_id, platform)
        suffixes = (" tutorial 教程 explained", " practical workflow examples 实例", " mistakes limitations alternatives 常见问题")
        cards = []
        report["queries"] = []
        remaining_candidates = search_limit
        for suffix in suffixes:
            count = min(10, remaining_candidates)
            query = topic + suffix
            report["queries"].append({"query": query, "limit": count})
            atomic_json(manifest, report)
            search = media.search(ws, run_id, platform, query, count, cookies)
            cards.extend(search["candidates"][:count])
            remaining_candidates -= count
            if not remaining_candidates:
                break
        candidates, seen_ids = [], set()
        for rank, card in enumerate(cards):
            _, source_id, canonical = media.identify(card["url"])
            if source_id in seen_ids:
                report["candidates"].append({"id": source_id, "decision": "rejected", "reasons": ["duplicate_video_id"]})
                continue
            seen_ids.add(source_id)
            ws.check_access(run_id, platform)
            try:
                metadata = {**media.inspect(ws, run_id, canonical, cookies), "platform": platform}
            except media.PlatformError as exc:
                if exc.category != "transport":
                    raise
                report["candidates"].append({"id": source_id, "url": canonical, "rank": rank,
                    "decision": "review", "reasons": ["metadata_transport_failure"]})
                atomic_json(manifest, report)
                continue
            row = {"id": source_id, "title": metadata.get("title"), "url": canonical,
                   "published_at": metadata.get("published_at"), "rank": rank,
                   "decision": "pending", "reasons": [],
                   "creator": metadata.get("channel_id") or metadata.get("author"),
                   "view_count": metadata.get("view_count"), "channel_follower_count": metadata.get("channel_follower_count")}
            report["candidates"].append(row)
            gate = screening.metadata_gate(metadata, kind, policy)
            duration = metadata.get("duration")
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= max_minutes*60:
                row.update(decision="review", reasons=["unknown_or_excess_duration"])
            elif gate:
                row.update(gate)
            else:
                candidates.append((metadata, row))
            atomic_json(manifest, report)
        # Popularity is only a within-platform tie-break after tutorial-title and date signals.
        candidates.sort(key=lambda pair: (
            not any(word in (pair[0].get("title") or "").lower() for word in ("tutorial", "explained", "breakdown", "教程", "教学")),
            screening.age_days(pair[0].get("published_at")) or 0 if kind != "principle" else 0,
            -(screening.popularity(pair[0]) or 0), pair[1]["rank"]))
        for metadata, row in candidates:
            if len(report["selected"]) >= limit:
                row.update(decision="deferred", reasons=["selected_video_limit"])
                continue
            creator = metadata.get("channel_id") or metadata.get("author")
            if creator and creators[creator] >= max_per_creator:
                row.update(decision="deferred", reasons=["per_creator_limit"])
                continue
            if report["processed_seconds"] + metadata["duration"] > total_minutes*60:
                row.update(decision="deferred", reasons=["total_duration_budget"])
                continue
            try:
                source = media.fetch(ws, run_id, metadata["url"], cookies=cookies,
                                     max_minutes=max_minutes, asr=True, model=model)
            except media.PlatformError as exc:
                if exc.category != "transport":
                    raise
                row.update(decision="review", reasons=["transcript_transport_failure"])
                atomic_json(manifest, report)
                continue
            if source.get("status") != "transcribed":
                row.update(decision="review", reasons=["no_usable_transcript"])
                continue
            segments = ws.read_segments(source)
            clean_segments, cleanup = cleaning.compact_segments(segments, source["transcript_origin"])
            row["cleanup"] = cleanup
            report["processed_seconds"] += source["duration"]
            verdict = screening.screen(metadata, clean_segments, topic, kind, policy, seen_text)
            row.update(verdict)
            if verdict["decision"] != "selected":
                atomic_json(manifest, report)
                continue
            seen_text[source["id"]] = screening.fingerprint("\n".join(s["text"] for s in clean_segments))
            if creator:
                creators[creator] += 1
            screen_text = {"status": "disabled", "items": []}
            remaining = max_images - report["ocr_budget_reserved"]
            cues = ocr.screen_cues(segments, source["duration"]) if ocr_mode == "auto" else []
            if ocr_mode == "auto" and not cues:
                screen_text = {"status": "not_requested_no_cue", "items": []}
            elif cues and remaining == 0:
                screen_text = {"status": "deferred_image_budget", "items": [], "omitted_cues": len(cues)}
            elif cues:
                report["ocr_budget_reserved"] += min(len(cues), remaining)
                try:
                    screen_text = ocr.extract_screen_text(ws, run_id, source["id"], cues,
                        cookies=cookies, language=ocr_language, max_images=remaining)
                    report["ocr_images"] += len(screen_text["items"])
                except ocr.OcrUnavailable as exc:
                    screen_text = {"status": "unavailable", "reason": str(exc), "items": [], "omitted_cues": len(cues)}
                except (PermissionError, media.PlatformError) as exc:
                    screen_text = {"status": "blocked", "reason": str(exc), "items": [], "omitted_cues": len(cues)}
                    report["error"] = {"type": type(exc).__name__, "message": str(exc)}
            text = {"source": {k: metadata[k] for k in ("id", "url", "title", "author", "published_at") if metadata.get(k) is not None},
                    "source_snapshot": {k: ws.source(source["id"]).get(k) for k in ("content_hash", "segments_sha256", "video_hash")},
                    "transcript_origin": source["transcript_origin"],
                    "segments": clean_segments}
            if source.get("transcript_review_status"):
                text["transcript_review_status"] = source["transcript_review_status"]
            if screen_text["status"] not in ("disabled", "not_requested_no_cue"):
                text["screen_text"] = cleaning.compact_ocr(screen_text)
            path = folder / (source["id"] + ".json")
            atomic_json(path, text)
            markdown = "# " + (source.get("title") or source["id"]) + "\n\n" + source["url"] + "\n\n"
            markdown += f"Published: {source.get('published_at') or 'unknown'}; transcript: {source['transcript_origin']}"
            if source.get("transcript_review_status"):
                markdown += "; " + source["transcript_review_status"]
            markdown += "\n\n"
            markdown += "\n".join(f"[{','.join(s['segment_ids'])} {s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in clean_segments)
            for item in text.get("screen_text", {}).get("items", []):
                markdown += f"\n\n## Screen text at {item['frame_times']}s — OCR, needs review\n\n" + item["text"]
            if screen_text["status"] in ("unavailable", "blocked", "deferred_image_budget") or screen_text.get("omitted_cues"):
                markdown += "\n\nScreen text incomplete: " + screen_text["status"] + "; missing cues are not recovered in this packet.\n"
            path.with_suffix(".md").write_text(markdown + "\n", encoding="utf-8")
            report["selected"].append({"id": source["id"], "title": source.get("title"), "url": source["url"],
                "creator": creator,
                "published_at": source.get("published_at"), "text_path": str(path), "markdown_path": str(path.with_suffix(".md")),
                "sha256": digest(path.read_bytes()), "markdown_sha256": digest(path.with_suffix(".md").read_bytes()), "segment_count": len(clean_segments),
                "transcript_origin": source["transcript_origin"], "ocr_status": screen_text["status"]})
            selected_texts.append(text)
            atomic_json(manifest, report)
            if report.get("error"):
                break
        report["status"] = "blocked" if report.get("error") else "ready" if report["selected"] else "no_admissible_sources"
    except (PermissionError, media.PlatformError) as exc:
        report.update(status="blocked", error={"type": type(exc).__name__, "message": str(exc)})
    except Exception as exc:
        report.update(status="failed", error={"type": type(exc).__name__, "message": "Collection failed; see candidate states; no success claimed"})
        raise
    finally:
        report["finished_at"] = now()
        if owned_run:
            ws.close_run(run_id)
        report["run_status"] = ws.run(run_id)["status"]
        atomic_json(manifest, report)
    result = {k: report[k] for k in ("id", "run_id", "topic", "platform", "kind", "status", "run_status", "selected", "llm_calls")}
    result.update(manifest_path=str(manifest), decisions=[{k: c[k] for k in ("id", "decision", "reasons", "score") if k in c} for c in report["candidates"]],
                  body_loaded=include_text, wiki_written=False)
    if "error" in report:
        result["error"] = report["error"]
    if include_text:
        result["texts"] = selected_texts
    return result
