"""Thin adapters for user-managed collectors. No upstream scraping code is copied."""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .store import Workspace, atomic_json, digest, now


def _active(ws: Workspace, run_id: str):
    if ws.run(run_id)["status"] != "active":
        raise ValueError("Run is closed")


def register_redcrack_post(ws, run_id, note):
    note_id = str(note.get("note_id", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{24}", note_id):
        raise ValueError("Invalid RedCrack note ID")
    source_id = "xiaohongshu-" + note_id
    row = ws.db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
    previous = json.loads(row[0]) if row else {}
    description = note.get("description", "").strip()
    source = {"status": "metadata_only", "visual_status": "not_inspected",
              "coverage": "exported_text_only_detail_depth_not_inferred", **previous,
              "id": source_id, "run_id": run_id, "platform": "xiaohongshu",
              "url": "https://www.xiaohongshu.com/explore/" + note_id,
              "imported_at": now()}
    # A later search card is a thinner observation, not a deletion of archived text.
    for key in ("title", "author", "published_at", "observed_at"):
        if note.get(key) not in (None, ""):
            if key == "observed_at" and previous.get("segments_path") and not description:
                source["metadata_observed_at"] = note[key]
            else:
                source[key] = note[key]
        else:
            source.setdefault(key, "" if key in ("title", "author") else None)
    if description:
        pieces = [description[i:i+1200] for i in range(0, len(description), 1200)]
        segments = [{"id": f"p{i:04}", "text": text} for i, text in enumerate(pieces, 1)]
        content_hash = digest(json.dumps(segments, ensure_ascii=False).encode())
        folder = ws.root / "private/raw" / source_id
        if previous and previous.get("content_hash") != content_hash:
            ws.snapshot_source(source_id)
        atomic_json(folder / "segments.json", segments)
        source.update(status="post_text_archived", transcript_origin="exported_post_text",
                      segments_path=str((folder / "segments.json").relative_to(ws.root)),
                      segments_sha256=digest((folder / "segments.json").read_bytes()),
                      content_hash=content_hash)
    ws.save_source(source)
    return {k: source[k] for k in ("id", "url", "title", "status", "coverage")}


def import_redcrack(ws: Workspace, run_id: str, folder: Path) -> dict:
    _active(ws, run_id)
    task = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    status = task.get("status")
    if status in ("login_required", "verification_required"):
        ws.pause("xiaohongshu", "upstream export requires login/verification")
        return {"status": status, "sources": []}
    if status not in ("complete", "partial"):
        raise ValueError("RedCrack export is not terminal complete/partial; do not infer empty success")
    result = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    if result.get("schema_version") != 1 or result.get("complete") is not True:
        raise ValueError("RedCrack schema/complete contract mismatch")
    notes = result.get("notes")
    if not isinstance(notes, list) or result.get("count") != len(notes):
        raise ValueError("RedCrack count/notes contract mismatch")
    # Validate all identities before changing state. Never propagate xsec/media URLs.
    if any(not re.fullmatch(r"[0-9a-fA-F]{24}", str(n.get("note_id", ""))) for n in notes):
        raise ValueError("Invalid RedCrack note ID")
    return {"status": status, "sources": [register_redcrack_post(ws, run_id, note) for note in notes]}


def redcrack_search(ws: Workspace, run_id: str, query: str, repo: Path, python: Path,
                    expected_commit: str, limit=5) -> dict:
    ws.require_consent(run_id, "xiaohongshu")
    ws.check_access(run_id, "xiaohongshu")
    if not 1 <= limit <= 8:
        raise ValueError("Adapter limit must be 1..8")
    repo = repo.resolve()
    if not (repo / "xhs.py").is_file() or not python.is_file():
        raise ValueError("Configure an existing RedCrack checkout and its Python executable")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    if head != expected_commit:
        raise ValueError("RedCrack checkout differs from the explicitly pinned commit")
    folder = ws.root / "private/collector-runs" / uuid.uuid4().hex
    profile = ws.root / "private/profiles/xiaohongshu"
    result = subprocess.run([str(python), str(repo / "xhs.py"), "search", query,
                             "--limit", str(limit), "--max-scrolls", "1",
                             "--profile", str(profile), "--output", str(folder)],
                            cwd=repo, capture_output=True)
    # Upstream owns browser locks, pacing and warning pauses. Never copy or change them.
    if result.returncode in (3, 4):
        ws.pause("xiaohongshu", "upstream collector requests human login/verification")
        return {"status": "human_action_required", "exit_code": result.returncode}
    if not (folder / "task.json").exists():
        raise ValueError("Collector produced no task receipt; not treated as success")
    first = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    if result.returncode == 0 and first.get("status") == "complete":
        first_results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        if not first_results.get("notes"):
            return {**import_redcrack(ws, run_id, folder), "upstream_commit": head, "exit_code": 0}
        search_receipt_hash = digest((folder / "task.json").read_bytes())
        # metadata-only belongs to the download subcommand in the pinned upstream CLI.
        result = subprocess.run([str(python), str(repo / "xhs.py"), "download", "--from-json",
                                 str(folder / "results.json"), "--metadata-only", "--profile", str(profile),
                                 "--output", str(folder)], cwd=repo, capture_output=True)
        if result.returncode in (3, 4):
            ws.pause("xiaohongshu", "upstream detail reader requests human action")
            return {"status": "human_action_required", "exit_code": result.returncode}
        if digest((folder / "task.json").read_bytes()) == search_receipt_hash:
            raise ValueError("Detail command produced no new task receipt; search cards are not full-post evidence")
    report = import_redcrack(ws, run_id, folder)
    report.update(upstream_commit=head, exit_code=result.returncode)
    return report


def studio_origin(value: str) -> str:
    p = urlparse(value)
    if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1") or p.path not in ("", "/") or p.query or p.fragment or p.username is not None or p.password is not None:
        raise ValueError("Douyin Studio origin must be a local HTTP origin")
    return value.rstrip("/")


def _studio_request(origin: str, route: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Studio-Client": "1"} if data else {}
    try:
        with urlopen(Request(origin + "/api/" + route, data=data, headers=headers), timeout=15) as response:
            return json.loads(response.read(4_000_000).decode("utf-8"))
    except URLError:
        raise ValueError("Studio observation/submission failed; resume the saved receipt, never blindly resubmit") from None


def import_douyin(ws: Workspace, run_id: str, job: dict) -> dict:
    _active(ws, run_id)
    if job.get("status") != "completed":
        if re.search(r"验证|登录|限制|扫码|频繁|HTTP (403|412|429)", job.get("error", "")):
            ws.pause("douyin", "upstream workbench requests human action")
        return {"job_id": job.get("id"), "status": job.get("status"), "sources": []}
    videos = job.get("result", {}).get("videos", [])
    for v in videos:
        if not re.fullmatch(r"\d{8,30}", str(v.get("id", ""))):
            raise ValueError("Invalid Douyin ID")
        if v.get("url") != "https://www.douyin.com/video/" + v["id"]:
            raise ValueError("Douyin video URL/ID mismatch")
    sources = []
    for v in videos:
        source_id = "douyin-" + v["id"]
        row = ws.db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        source = {"status": "metadata_only", "visual_status": "not_inspected", **previous,
                  "id": source_id, "run_id": run_id, "platform": "douyin", "url": v["url"], "imported_at": now()}
        fields = {"title": v.get("title"), "author": v.get("author"), "published_at": v.get("createdAt"),
                  "observed_at": (v.get("detailContext") or v.get("searchContext") or {}).get("observedAt")}
        for key, value in fields.items():
            if value not in (None, ""):
                source["metadata_observed_at" if key == "observed_at" and previous.get("segments_path") else key] = value
        if not source.get("video_path"):
            source["duration"] = v.get("duration") or previous.get("duration")
        source.setdefault("title", "")
        source.setdefault("author", "")
        ws.save_source(source)
        sources.append({k: source[k] for k in ("id", "url", "title", "status")})
    return {"job_id": job.get("id"), "status": "completed", "sources": sources,
            "coverage": "metadata_only_not_video_learning"}


def studio_search(ws: Workspace, run_id: str, query: str, origin: str, receipt: Path,
                  limit=5, timeout=60) -> dict:
    ws.require_consent(run_id, "douyin")
    ws.check_access(run_id, "douyin")
    origin = studio_origin(origin)
    if not 1 <= limit <= 8 or not 1 <= timeout <= 120:
        raise ValueError("Studio adapter limit 1..8, observation timeout 1..120 seconds")
    if receipt.exists():
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if (saved["run_id"], saved["origin"], saved["query"]) != (run_id, origin, query):
            raise ValueError("Receipt belongs to another run/origin/query")
        if not saved.get("job_id"):
            raise ValueError("Prior submission outcome unknown; inspect Studio state before manual recovery")
    else:
        saved = {"run_id": run_id, "origin": origin, "query": query, "submitted_at": now(), "job_id": None}
        atomic_json(receipt, saved)  # Unknown POST outcomes cannot trigger a second POST on retry.
        submitted = _studio_request(origin, "search", {"query": query, "limit": limit})
        if not submitted.get("id"):
            raise ValueError("Studio did not return a job ID; keep unknown-submission receipt")
        saved["job_id"] = submitted["id"]
        atomic_json(receipt, saved)
    job = _studio_wait(origin, saved["job_id"], timeout)
    if job:
        report = import_douyin(ws, run_id, job)
        saved.update(status=report["status"], observed_at=now())
        atomic_json(receipt, saved)
        return report
    return {"job_id": saved["job_id"], "status": "observation_timeout_not_terminal", "resume_receipt": str(receipt)}


def _studio_wait(origin: str, job_id: str, timeout: int):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = _studio_request(origin, "state")
        job = next((j for j in state.get("jobs", []) if j.get("id") == job_id), None)
        if job and job.get("status") in ("completed", "failed", "cancelled"):
            return job
        time.sleep(min(2, max(0, end - time.monotonic())))
    return None


def _studio_file(origin: str, job: dict, path: Path, max_bytes: int) -> dict:
    import imageio_ffmpeg

    partial = path.with_suffix(".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        try:
            route = "/api/downloads/" + quote(job["id"], safe="") + "/file"
            with urlopen(Request(origin + route), timeout=30) as response, partial.open("wb") as output:
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Studio media exceeds the analysis size budget")
                    output.write(chunk)
        except URLError:
            raise ValueError("Studio file observation failed; resume its existing download receipt") from None
        if size != job["bytes"]:
            raise ValueError("Studio download byte count does not match its completed job")
        stream = imageio_ffmpeg.read_frames(str(partial))
        try:
            metadata = next(stream)
        except (RuntimeError, StopIteration):
            raise ValueError("Studio file is not a readable video") from None
        finally:
            stream.close()
        path_duration = metadata.get("duration", 0)
        if not 0 < path_duration <= 600:
            raise ValueError("Actual video duration is outside the ten-minute analysis budget")
        partial.replace(path)
        return {"bytes": size, "duration": path_duration, "video_hash": digest(path.read_bytes())}
    finally:
        partial.unlink(missing_ok=True)


def studio_video(ws: Workspace, run_id: str, source_id: str, origin: str, receipt: Path,
                 timeout: int = 60) -> dict:
    """Delegate downloading to Studio; never expose or reconstruct CDN URLs."""
    ws.require_consent(run_id, "douyin")
    ws.check_access(run_id, "douyin")
    origin = studio_origin(origin)
    if not 1 <= timeout <= 120:
        raise ValueError("Studio observation timeout must be 1..120 seconds")
    source = ws.source(source_id)
    video_id = source_id.removeprefix("douyin-")
    canonical = "https://www.douyin.com/video/" + video_id
    if source.get("platform") != "douyin" or source.get("url") != canonical or not re.fullmatch(r"\d{8,30}", video_id):
        raise ValueError("Select a previously discovered canonical Douyin source")
    if not source.get("duration") or not 0 < source["duration"] <= 600:
        raise ValueError("Inspect a video of known duration up to ten minutes before downloading")
    if source.get("video_path"):
        path = ws.root / source["video_path"]
        if not path.is_file() or digest(path.read_bytes()) != source["video_hash"]:
            raise ValueError("Archived video changed; review it before reacquisition")
        return {"id": source_id, "video_path": source["video_path"], "cached": True}
    if receipt.exists():
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if (saved.get("run_id"), saved.get("origin"), saved.get("source_id")) != (run_id, origin, source_id):
            raise ValueError("Download receipt belongs to another run/origin/source")
        if not saved.get("job_id"):
            raise ValueError("Prior download submission outcome unknown; inspect Studio before manual recovery")
    else:
        saved = {"run_id": run_id, "origin": origin, "source_id": source_id, "submitted_at": now(), "job_id": None}
        atomic_json(receipt, saved)
        result = _studio_request(origin, "downloads", {"ids": [video_id]})
        jobs = result.get("jobs", [])
        if len(jobs) != 1 or not jobs[0].get("id"):
            raise ValueError("Studio did not return exactly one download job; keep unknown-submission receipt")
        saved["job_id"] = jobs[0]["id"]
        atomic_json(receipt, saved)
    job = _studio_wait(origin, saved["job_id"], timeout)
    if not job:
        return {"job_id": saved["job_id"], "status": "observation_timeout_not_terminal", "resume_receipt": str(receipt)}
    saved.update(status=job["status"], observed_at=now())
    atomic_json(receipt, saved)
    if job["status"] != "completed":
        return import_douyin(ws, run_id, job)
    video = job.get("video", {})
    if job.get("type") != "download" or video.get("id") != video_id or video.get("url") != canonical:
        raise ValueError("Completed download job does not match the selected video")
    max_bytes = 150 * 1024 * 1024
    if not isinstance(job.get("bytes"), int) or not 0 < job["bytes"] <= max_bytes:
        raise ValueError("Completed download size is missing or exceeds the 150 MB analysis budget")
    path = ws.root / "private/raw" / source_id / "video.mp4"
    info = _studio_file(origin, job, path, max_bytes)
    source.update(run_id=run_id, video_path=str(path.relative_to(ws.root)), video_hash=info["video_hash"],
                  duration=info["duration"], visual_status="downloaded_not_inspected",
                  media_receipt={"collector": "douyin-studio", "job_id": job["id"], "bytes": info["bytes"],
                                 "acquired_at": now()})
    source.setdefault("transcript_origin", "none")
    ws.save_source(source)
    return {"id": source_id, "video_path": source["video_path"], "bytes": info["bytes"],
            "visual_status": source["visual_status"], "job_id": job["id"]}
