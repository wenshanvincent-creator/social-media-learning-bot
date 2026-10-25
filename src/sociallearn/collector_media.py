"""Import bounded collector evidence without exporting signed media URLs."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import adapters, media
from .store import Workspace, atomic_json, digest, now


def import_redcrack_media(ws: Workspace, run_id: str, directory: Path) -> dict:
    adapters._active(ws, run_id)
    directory = directory.resolve()
    note = json.loads((directory / "note.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "downloads.json").read_text(encoding="utf-8"))
    note_id = note.get("note_id", "")
    if not re.fullmatch(r"[0-9a-f]{24}", note_id) or manifest.get("note_id") != note_id:
        raise ValueError("Media manifest and note identity do not match")
    if note.get("canonical_url") != "https://www.xiaohongshu.com/explore/" + note_id:
        raise ValueError("Exported note canonical URL does not match its ID")
    if manifest.get("schema_version") != 1 or manifest.get("status") not in ("complete", "partial"):
        raise ValueError("Import only a terminal complete/partial RedCrack media manifest")
    assets, omitted, indices = [], [], set()
    declared = note.get("media", [])
    for record in manifest.get("files", []):
        index = record.get("index")
        if not isinstance(index, int) or not 1 <= index <= len(declared) or index in indices:
            raise ValueError("Media index missing, duplicated or outside the exported note")
        indices.add(index)
        if record.get("status") not in ("downloaded", "skipped"):
            omitted.append({"index": index, "reason": "upstream_not_downloaded"})
            continue
        asset, kind = declared[index - 1], record.get("kind")
        if kind not in ("image", "video") or asset.get("kind") != kind or urlparse(
                record.get("url", "")).path != urlparse(asset.get("url", "")).path:
            raise ValueError("Downloaded media does not match the note's indexed asset")
        name = record.get("file", "")
        if not name or Path(name).name != name:
            raise ValueError("Media filename must stay inside the explicit export directory")
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
            raise ValueError("Media file missing or redirects outside its export directory")
        size = path.stat().st_size
        if not size or size != record.get("size"):
            raise ValueError("Exported media byte count does not match its manifest")
        if index > 20 or size > (32 if kind == "image" else 150) * 1024 * 1024:
            omitted.append({"index": index, "reason": "analysis_budget"})
            continue
        if digest(path.read_bytes()) != record.get("sha256"):
            raise ValueError("Exported media hash does not match its manifest")
        item = {"index": index, "kind": kind, "path": path, "sha256": record["sha256"]}
        if kind == "video":
            import imageio_ffmpeg
            stream = imageio_ffmpeg.read_frames(str(path))
            try:
                duration = next(stream).get("duration", 0)
            except (RuntimeError, StopIteration):
                raise ValueError("Exported media is not a readable video") from None
            finally:
                stream.close()
            if not 0 < duration <= 600:
                omitted.append({"index": index, "reason": "duration_budget"})
                continue
            item["duration"] = duration
        assets.append(item)
    missing = set(range(1, len(declared) + 1)) - indices
    if missing and manifest["status"] == "complete":
        raise ValueError("Complete manifest lacks declared media records")
    omitted.extend({"index": i, "reason": "upstream_missing_record"} for i in sorted(missing))
    if sum(a["kind"] == "video" for a in assets) > 1:
        raise ValueError("Choose a note with one primary exported video")
    # Validate the whole selected manifest before changing source state or copying files.
    adapters.register_redcrack_post(ws, run_id, note)
    source_id = "xiaohongshu-" + note_id
    source = ws.source(source_id)
    for item in assets:
        asset_id = f"image-{item['index']:03}" if item["kind"] == "image" else "video"
        target = ws.root / "private/raw" / source_id / (asset_id + item["path"].suffix.lower())
        existing = source.get("images", {}).get(asset_id) if item["kind"] == "image" else (
            {"path": source["video_path"], "sha256": source["video_hash"]} if source.get("video_path") else None)
        if existing and existing["sha256"] == item["sha256"]:
            old_path = ws.root / existing["path"]
            if old_path.is_file() and digest(old_path.read_bytes()) == item["sha256"]:
                continue
        if existing:
            old_path = ws.root / existing["path"]
            if old_path.is_file() and digest(old_path.read_bytes()) == existing["sha256"]:
                revision = ws.root / "state/source-revisions" / source_id / existing["sha256"] / old_path.name
                revision.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(old_path, revision)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item["path"], target)
        if item["kind"] == "image":
            source.setdefault("images", {})[asset_id] = {"id": asset_id, "path": str(target.relative_to(ws.root)),
                "sha256": item["sha256"], "status": "archived_not_inspected", "imported_at": now()}
        else:
            source.update(video_path=str(target.relative_to(ws.root)), video_hash=item["sha256"], duration=item["duration"])
        source["visual_status"] = "media_archived_not_inspected"
    source["media_import"] = {"collector": "RedCrack", "observed_manifest_status": manifest["status"],
                              "imported_at": now(), "omitted": omitted}
    video_source_id = source_id + "-video" if source.get("video_path") else None
    if video_source_id:
        source["video_source_id"] = video_source_id
    ws.save_source(source)
    if video_source_id:
        row = ws.db.execute("SELECT data FROM sources WHERE id=?", (video_source_id,)).fetchone()
        child = json.loads(row[0]) if row else {"status": "metadata_only", "transcript_origin": "none"}
        if child.get("video_hash") and child["video_hash"] != source["video_hash"]:
            ws.snapshot_source(video_source_id)
            for key in ("segments_path", "segments_sha256", "content_hash", "frames", "visual_reviews", "asr_model", "asr_compute", "transcript_review_status"):
                child.pop(key, None)
            child.update(status="metadata_only", transcript_origin="none", visual_status="downloaded_not_inspected")
        child.update({key: source.get(key) for key in ("url", "platform", "title", "author", "published_at",
                     "video_path", "video_hash", "duration")})
        child.update(id=video_source_id, run_id=run_id, parent_source_id=source_id, evidence_channel="video")
        child.setdefault("observed_at", source.get("observed_at"))
        child.setdefault("visual_status", "downloaded_not_inspected")
        ws.save_source(child)
    return {"id": source_id, "status": "partial" if omitted or manifest["status"] == "partial" else "imported",
            "images": len(source.get("images", {})), "video_archived": bool(source.get("video_path")), "video_source_id": video_source_id,
            "visual_status": source["visual_status"], "omitted": omitted}


def source_images(ws: Workspace, source_id: str, offset=0, limit=5) -> dict:
    if offset < 0 or not 1 <= limit <= 20:
        raise ValueError("Use a nonnegative offset and image limit 1..20")
    source = ws.source(source_id)
    images = sorted(source.get("images", {}).values(), key=lambda x: x["id"])
    selected = images[offset:offset + limit]
    return {"id": source_id, "url": source["url"], "title": source.get("title"), "evidence_origin": source.get("evidence_origin"),
            "video_source_id": source.get("video_source_id"), "images": [{**i, "absolute_path": str(ws.root / i["path"])} for i in selected],
            "total": len(images), "next_offset": offset + len(selected) if offset + len(selected) < len(images) else None}


def review_images(ws: Workspace, source_id: str, ids: list[str], observation: str, reviewer: str) -> dict:
    from .wiki import Wiki
    if not observation.strip() or not reviewer.strip():
        raise ValueError("Record an actual image observation and reviewer")
    Wiki(ws).validate_citations([{"source_id": source_id, "image_ids": ids}])
    source = ws.source(source_id)
    for image_id in ids:
        source["images"][image_id].update(status="inspected", reviewer=reviewer, inspected_at=now())
    receipt = {"image_ids": ids, "observation": observation, "reviewer": reviewer,
               "reviewed_at": now(), "scope": "listed_images_only_not_video_motion"}
    source.setdefault("visual_reviews", []).append(receipt)
    source["visual_status"] = "sampled_images_inspected"
    ws.save_source(source)
    return receipt


# The pinned Studio validation.ts media-domain policy; every caption redirect is rechecked.
MEDIA_DOMAINS = ("douyinvod.com", "douyin.com", "bytecdn.cn", "bytecdn.com", "bytedance.com", "bytedance.net",
                 "pstatp.com", "snssdk.com", "byteimg.com", "ibytedtos.com", "ibyteimg.com", "bytegoofy.com", "akamaized.net")


def _caption_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or not any(
            parsed.hostname == h or (parsed.hostname or "").endswith("." + h) for h in MEDIA_DOMAINS):
        raise ValueError("Caption address is outside the pinned collector media policy")
    return value


class _CaptionRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _caption_url(newurl))


def _caption_bytes(url: str) -> bytes:
    request = Request(_caption_url(url), headers={"Referer": "https://www.douyin.com/"})
    with build_opener(_CaptionRedirect()).open(request, timeout=20) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Caption track exceeds the analysis text budget")
    return raw


def studio_captions(ws: Workspace, run_id: str, source_id: str, receipt: Path, timeout=60, refresh=False) -> dict:
    ws.require_consent(run_id, "douyin")
    ws.check_access(run_id, "douyin")
    if not 1 <= timeout <= 120:
        raise ValueError("Studio observation timeout must be 1..120 seconds")
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    origin = adapters.studio_origin(saved["origin"])
    if saved.get("run_id") != run_id or not saved.get("job_id"):
        raise ValueError("Use this run's existing single-video Studio resolve receipt")
    source = ws.source(source_id)
    if source.get("platform") != "douyin":
        raise ValueError("Select a discovered Douyin source")
    if not refresh and source.get("segments_path") and source.get("transcript_origin") in ("manual", "automatic", "platform_page_track"):
        ws.read_segments(source)
        return {"id": source_id, "status": "transcribed", "cached": True, "transcript_origin": source["transcript_origin"], "asr_started": False}
    job = adapters._studio_wait(origin, saved["job_id"], timeout)
    if not job:
        return {"job_id": saved["job_id"], "status": "observation_timeout_not_terminal", "resume_receipt": str(receipt)}
    if job["status"] != "completed":
        return adapters.import_douyin(ws, run_id, job)
    videos = job.get("result", {}).get("videos", [])
    if job.get("type") != "resolve" or len(videos) != 1 or videos[0].get("url") != source["url"]:
        raise ValueError("Caption evidence needs the selected video's completed resolve job")
    descriptor = adapters._studio_request(origin, "jobs/" + quote(job["id"], safe="") + "/source-media")
    if descriptor.get("schemaVersion") != 1 or descriptor.get("canonicalUrl") != source["url"] or "douyin-" + str(descriptor.get("videoId")) != source_id:
        raise ValueError("Page caption descriptor and selected video do not match")
    tracks = descriptor.get("subtitleTracks", [])
    if not tracks:
        source["caption_discovery"] = {"job_id": job["id"], "status": "no_page_tracks", "coverage": "page_tracks_only", "observed_at": now()}
        ws.save_source(source)
        return {"id": source_id, "status": "no_page_tracks", "coverage": "page_tracks_only", "asr_started": False}
    # HTML track elements do not reliably distinguish human and automatic captions.
    track = min(tracks, key=lambda t: (not str(t.get("language", "")).lower().startswith(("zh", "en"))))
    try:
        raw = _caption_bytes(track["url"])
    except HTTPError as exc:
        media.handle_error(ws, "douyin", exc)
    except URLError:
        raise ValueError("Page caption read failed; keep the resolve receipt, no automatic ASR") from None
    text = raw.decode("utf-8-sig")
    ext = "json3" if text.lstrip().startswith("{") and "events" in json.loads(text) else "json" if text.lstrip().startswith("{") else "vtt"
    segments = media.parse_subtitles(text, ext)
    if not source.get("duration") or any(s["start"] < 0 or s["end"] > source["duration"] + 1 for s in segments):
        raise ValueError("Caption timing is outside the selected video; review alignment before importing")
    if source.get("segments_path"):
        ws.snapshot_source(source_id)
    folder = ws.root / "private/raw" / source_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("page-captions." + ext)).write_bytes(raw)
    atomic_json(folder / "segments.json", segments)
    source.update(run_id=run_id, status="transcribed", transcript_origin="platform_page_track", language=track.get("language"),
                  observed_at=now(), transcript_review_status="needs_review", segments_path=str((folder / "segments.json").relative_to(ws.root)),
                  segments_sha256=digest((folder / "segments.json").read_bytes()), content_hash=digest(json.dumps(segments, ensure_ascii=False).encode()),
                  caption_discovery={"job_id": job["id"], "status": "page_track_archived", "coverage": "page_tracks_only"})
    source.pop("asr_model", None)
    source.pop("asr_compute", None)
    ws.save_source(source)
    return {"id": source_id, "status": "transcribed", "transcript_origin": "platform_page_track", "segments": len(segments),
            "transcript_review_status": "needs_review", "asr_started": False}
