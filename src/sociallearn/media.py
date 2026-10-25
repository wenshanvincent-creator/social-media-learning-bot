from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import cookie_file
from .store import Workspace, atomic_json, digest, now


def identify(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Use a full HTTPS video URL")
    host = parsed.hostname or ""
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]+|av\d+)/?", parsed.path)
        if not match:
            raise ValueError("Use a canonical Bilibili video URL, including p= for multipart")
        part = int(parse_qs(parsed.query).get("p", ["1"])[0])
        if part < 1:
            raise ValueError("Bilibili part must be positive")
        vid = match[1]
        return "bilibili", f"bilibili-{vid}-p{part}", f"https://www.bilibili.com/video/{vid}/?p={part}"
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        vid = parsed.path.lstrip("/") if host == "youtu.be" else parse_qs(parsed.query).get("v", [""])[0]
        if parsed.path.startswith(("/shorts/", "/embed/")):
            vid = parsed.path.split("/")[2]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            raise ValueError("Unrecognized YouTube video ID")
        return "youtube", f"youtube-{vid}", f"https://www.youtube.com/watch?v={vid}"
    raise ValueError("Only Bilibili and YouTube are implemented in phase 1")


class PlatformError(RuntimeError):
    def __init__(self, message, *, category="acquisition"):
        super().__init__(message)
        self.category = category


class QuietLog:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def handle_error(ws: Workspace, platform: str, exc: Exception):
    message = str(exc)
    codes = sorted(set(re.findall(r"\b(403|412|429|461|471)\b", message)))
    if codes or re.search(r"captcha|verify you|verification|not a bot|验证码|访问频繁", message, re.I):
        reason = "HTTP " + ",".join(codes) if codes else "verification challenge"
        ws.pause(platform, reason)
        raise PlatformError(f"{platform}: {reason}; access restricted; platform paused pending human review") from None
    if re.search(r"sign in|login|cookies|登录", message, re.I):
        raise PlatformError(f"{platform}: login/session required; no automatic retry") from None
    if type(exc).__name__ == "TransportError" or isinstance(exc, (ConnectionError, TimeoutError)):
        raise PlatformError(f"{platform}: network transport failed; no automatic retry", category="transport") from None
    raise PlatformError(f"{platform}: acquisition failed ({type(exc).__name__}); no source marked complete") from None


def ydl_options(cookies=None) -> dict:
    return {"quiet": True, "no_warnings": True, "logger": QuietLog(),
            "socket_timeout": 25, "retries": 0, "extractor_retries": 0,
            "cookiefile": cookies, "noplaylist": True}


def search(ws: Workspace, run_id: str, platform: str, query: str, limit: int = 5,
           cookies: bool = False) -> dict:
    from yt_dlp import YoutubeDL

    if not 1 <= limit <= 10:
        raise ValueError("Search limit must be 1..10")
    ws.check_access(run_id, platform)
    term = ("bilisearch" if platform == "bilibili" else "ytsearch") + f"{limit}:{query}"
    try:
        with cookie_file(ws, run_id, platform, cookies) as file:
            opts = {**ydl_options(file), "extract_flat": True, "playlistend": limit}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(term, download=False)
    except PermissionError:
        raise
    except Exception as exc:
        handle_error(ws, platform, exc)
    cards = []
    for entry in list(info.get("entries") or [])[:limit]:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if platform == "bilibili":
            url = re.sub(r"^http://", "https://", url)
            if not re.search(r"bilibili\.com/video/", url):
                continue  # Do not silently mix paid course products into video candidates.
        if platform == "youtube" and not url.startswith("https://"):
            url = "https://www.youtube.com/watch?v=" + str(entry["id"])
        cards.append({"title": entry.get("title"), "url": url,
                      "author": entry.get("uploader"), "duration": entry.get("duration"),
                      "published": entry.get("upload_date"), "status": "candidate_only"})
    result = {"platform": platform, "query": query, "observed_at": now(), "candidates": cards}
    atomic_json(ws.root / "state" / run_id / (platform + "-" + digest(query.encode())[:10] + ".json"), result)
    return result


def inspect(ws: Workspace, run_id: str, url: str, cookies=False) -> dict:
    from yt_dlp import YoutubeDL

    platform, source_id, canonical = identify(url)
    try:
        with cookie_file(ws, run_id, platform, cookies) as file:
            with YoutubeDL({**ydl_options(file), "ignore_no_formats_error": True}) as ydl:
                info = ydl.extract_info(canonical, download=False)
        if info.get("entries"):
            raise ValueError("Select one video/part before inspecting")
        return {"id": source_id, "url": canonical, "title": info.get("title"),
                "author": info.get("uploader"), "duration": info.get("duration"),
                "description": info.get("description"), "view_count": info.get("view_count"),
                "channel_follower_count": info.get("channel_follower_count"), "channel_id": info.get("channel_id"),
                "channel_is_verified": info.get("channel_is_verified"),
                "published_at": info.get("upload_date"), "observed_at": now(),
                "status": "metadata_only", "visual_status": "not_inspected"}
    except (ValueError, PermissionError):
        raise
    except Exception as exc:
        handle_error(ws, platform, exc)


def parse_subtitles(body: str, ext: str) -> list[dict]:
    if ext == "json3":
        events = json.loads(body).get("events", [])
        result = [{"start": e["tStartMs"] / 1000,
                   "end": (e["tStartMs"] + e.get("dDurationMs", 0)) / 1000,
                   "text": "".join(x.get("utf8", "") for x in e.get("segs", [])).strip()}
                  for e in events if "tStartMs" in e]
    elif ext == "json":
        result = [{"start": float(e["from"]), "end": float(e["to"]), "text": e["content"]}
                  for e in json.loads(body).get("body", [])]
    else:
        def seconds(value):
            parts = value.replace(",", ".").split(":")
            return sum(float(v) * 60 ** i for i, v in enumerate(reversed(parts)))
        result = []
        pattern = r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d+:)?\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)"
        for match in re.finditer(pattern, body, re.S):
            text = re.sub(r"<[^>]+>", "", match[3]).strip()
            result.append({"start": seconds(match[1]), "end": seconds(match[2]), "text": text})
    clean = []
    for item in result:
        if not item["text"] or item["end"] <= item["start"]:
            continue
        clean.append({"id": f"s{len(clean)+1:04}", **item})
    if not clean:
        raise ValueError("Subtitle track contained no usable timed segments")
    return clean


def fetch(ws: Workspace, run_id: str, url: str, *, cookies: bool = False,
          max_minutes: int = 30, asr: bool = False, model: str = "small", refresh: bool = False) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.networking import Request

    platform, source_id, canonical = identify(url)
    ws.check_access(run_id, platform)
    if cookies:
        ws.require_consent(run_id, platform)
    existing = ws.db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
    if existing:
        old = json.loads(existing[0])
        if not refresh and old.get("status") == "transcribed" and (ws.root / old["segments_path"]).exists():
            if old.get("segments_sha256") and digest((ws.root / old["segments_path"]).read_bytes()) != old["segments_sha256"]:
                raise ValueError("Cached transcript changed on disk; review or explicitly refresh")
            return {**old, "cached": True}
        if refresh:
            ws.snapshot_source(source_id)
    folder = ws.root / "private/raw" / source_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with cookie_file(ws, run_id, platform, cookies) as file:
            opts = ydl_options(file)
            # Metadata/captions do not require a downloadable media format.
            # Keep opts strict for the separate audio-download fallback below.
            with YoutubeDL({**opts, "ignore_no_formats_error": True}) as ydl:
                info = ydl.extract_info(canonical, download=False)
                if info.get("entries"):
                    # Full playlist results must not silently become the first part.
                    raise ValueError("Extractor returned a collection for a single-part URL")
                if info.get("is_live") or not info.get("duration") or info["duration"] > max_minutes * 60:
                    raise ValueError("Live/unknown/excess duration; choose a bounded source")
                source = {"id": source_id, "run_id": run_id, "platform": platform,
                          "url": canonical, "title": info.get("title"), "author": info.get("uploader"),
                          "published_at": info.get("upload_date"), "observed_at": now(),
                          "duration": info["duration"], "status": "metadata_only", "visual_status": "not_inspected"}
                # Manual tracks first; for automatic tracks avoid choosing a
                # translated Chinese track before the speaker's original captions.
                selected = None
                for origin, tracks in (("manual", info.get("subtitles", {})),
                                       ("automatic", info.get("automatic_captions", {}))):
                    languages = ["zh-Hans", "zh-CN", "zh", "ai-zh", "en", "en-orig", "ai-en"]
                    if origin == "automatic":
                        native = info.get("language") or ""
                        languages = [k for k in tracks if k.endswith("-orig")] + [native, native.split("-")[0]] + languages
                    for lang in dict.fromkeys(languages):
                        if tracks.get(lang):
                            selected = (origin, lang, tracks[lang])
                            break
                    if selected:
                        break
                if selected:
                    origin, language, tracks = selected
                    preference = {"json3": 0, "json": 1, "vtt": 2, "srt": 3}
                    usable = [t for t in tracks if t.get("ext") in preference]
                    if usable:
                        track = min(usable, key=lambda t: preference[t["ext"]])
                        request = Request(track["url"], headers=info.get("http_headers", {}))
                        with ydl.urlopen(request) as response:
                            raw = response.read(8_000_001)
                        if len(raw) > 8_000_000:
                            raise ValueError("Subtitle size exceeded limit")
                        (folder / ("original." + track["ext"])).write_bytes(raw)
                        segments = parse_subtitles(raw.decode("utf-8-sig"), track["ext"])
                        source.update(transcript_origin=origin, language=language)
                    else:
                        selected = None
                if not selected:
                    if not asr:
                        source["status"] = "no_captions"
                        return ws.save_source(source)
                    audio_opts = {**opts, "format": "bestaudio/best", "outtmpl": str(folder / "audio.%(ext)s"),
                                  "max_filesize": 200 * 1024 * 1024}
                    with YoutubeDL(audio_opts) as audio_ydl:
                        audio_info = audio_ydl.extract_info(canonical, download=True)
                        audio_path = Path(audio_ydl.prepare_filename(audio_info))
                    segments = transcribe(audio_path, model=model)
                    source.update(transcript_origin="local_whisper", asr_model=model,
                                  asr_compute="cpu/int8", language="auto_detected",
                                  transcript_review_status="needs_review")
                atomic_json(folder / "segments.json", segments)
                source.update(status="transcribed", segments_path=str((folder / "segments.json").relative_to(ws.root)),
                              segments_sha256=digest((folder / "segments.json").read_bytes()),
                              content_hash=digest(json.dumps(segments, ensure_ascii=False).encode()))
                return ws.save_source(source)
    except (PermissionError, ValueError):
        raise
    except Exception as exc:
        handle_error(ws, platform, exc)


def transcribe(path: Path, model: str = "small") -> list[dict]:
    if model not in ("tiny", "base", "small"):
        raise ValueError("Only lightweight multilingual Whisper models are enabled locally")
    from faster_whisper import WhisperModel

    engine = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=4)
    segments, _ = engine.transcribe(str(path), beam_size=5, vad_filter=False)
    result = [{"id": f"s{i:04}", "start": s.start, "end": s.end, "text": s.text.strip()}
              for i, s in enumerate(segments, 1) if s.text.strip()]
    if not result:
        raise ValueError("Whisper produced no speech segments")
    return result


def transcribe_local(ws: Workspace, run_id: str, source_id: str, model: str = "small") -> dict:
    """Read an already bound local video; no cookie/profile or network access."""
    if ws.run(run_id)["status"] != "active":
        raise ValueError("Run is closed")
    source = ws.source(source_id)
    if source.get("segments_path"):
        ws.read_segments(source)
        if source.get("transcript_origin") in ("manual", "automatic", "platform_page_track", "local_whisper"):
            return {"id": source_id, "status": source["status"], "cached": True,
                    "transcript_origin": source["transcript_origin"]}
        raise ValueError("Keep exported post text intact; it is not a timed video transcript")
    if not source.get("video_path") or not source.get("duration") or not 0 < source["duration"] <= 600:
        raise ValueError("Archive a bounded source video before local transcription")
    path = ws.root / source["video_path"]
    if digest(path.read_bytes()) != source["video_hash"]:
        raise ValueError("Source video hash changed")
    segments = transcribe(path, model)
    output = ws.root / "private/raw" / source_id / "segments.json"
    atomic_json(output, segments)
    source.update(run_id=run_id, status="transcribed", transcript_origin="local_whisper", asr_model=model,
                  asr_compute="cpu/int8", language="auto_detected", transcript_review_status="needs_review",
                  segments_path=str(output.relative_to(ws.root)), segments_sha256=digest(output.read_bytes()),
                  content_hash=digest(json.dumps(segments, ensure_ascii=False).encode()))
    ws.save_source(source)
    return {"id": source_id, "status": "transcribed", "segments": len(segments),
            "transcript_origin": "local_whisper", "transcript_review_status": "needs_review"}


def excerpt(ws: Workspace, source_id: str, start: float, end: float) -> dict:
    if not 0 <= start < end:
        raise ValueError("Use a positive ordered time range")
    source = ws.source(source_id)
    segments = ws.read_segments(source)
    return {"source": {k: source[k] for k in ("id", "url", "title", "transcript_origin", "visual_status")},
            "segments": [s for s in segments if s["start"] < end and s["end"] > start]}


def download_video(ws: Workspace, run_id: str, url: str, cookies=False, max_minutes=10) -> dict:
    from yt_dlp import YoutubeDL
    import imageio_ffmpeg

    platform, source_id, canonical = identify(url)
    ws.check_access(run_id, platform)
    if cookies:
        ws.require_consent(run_id, platform)
    row = ws.db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
    source = json.loads(row[0]) if row else {**inspect(ws, run_id, url, cookies), "run_id": run_id,
                                            "platform": platform, "transcript_origin": "none"}
    if not source.get("duration") or source["duration"] > max_minutes * 60:
        raise ValueError("Video exceeds analysis duration budget")
    if source.get("video_path"):
        path = ws.root / source["video_path"]
        if path.exists() and digest(path.read_bytes()) == source["video_hash"]:
            return {"id": source_id, "video_path": source["video_path"], "cached": True}
    folder = ws.root / "private/raw" / source_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with cookie_file(ws, run_id, platform, cookies) as file:
            opts = {**ydl_options(file), "format": "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
                    "outtmpl": str(folder / "video.%(ext)s"), "merge_output_format": "mp4",
                    "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(), "max_filesize": 150 * 1024 * 1024}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(canonical, download=True)
                path = Path(ydl.prepare_filename(info))
                if not path.exists():
                    raise ValueError("Expected video output is missing; not marked downloaded")
        source.update(video_path=str(path.relative_to(ws.root)), video_hash=digest(path.read_bytes()),
                      visual_status="downloaded_not_inspected")
        ws.save_source(source)
        return {"id": source_id, "video_path": source["video_path"], "bytes": path.stat().st_size,
                "visual_status": source["visual_status"]}
    except (ValueError, PermissionError):
        raise
    except Exception as exc:
        handle_error(ws, platform, exc)


def frame(ws: Workspace, source_id: str, at: float) -> dict:
    import imageio_ffmpeg

    source = ws.source(source_id)
    if not source.get("video_path"):
        raise ValueError("Download this source's video before extracting frames")
    video = ws.root / source["video_path"]
    if digest(video.read_bytes()) != source["video_hash"]:
        raise ValueError("Source video hash changed")
    if not 0 <= at < source["duration"]:
        raise ValueError("Frame timestamp outside source duration")
    output = ws.root / "private/raw" / source_id / f"frame-{at:.3f}.png"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-ss", str(at), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", str(output)],
                   check=True, capture_output=True)
    record = {"source_id": source_id, "time": at, "path": str(output.relative_to(ws.root)),
              "sha256": digest(output.read_bytes()), "video_hash": source["video_hash"], "status": "extracted_not_inspected"}
    source.setdefault("frames", {})[f"{at:.3f}"] = record
    ws.save_source(source)
    return record


def review_frames(ws: Workspace, source_id: str, times: list[float], observation: str, reviewer: str) -> dict:
    source = ws.source(source_id)
    if not observation.strip() or not reviewer.strip():
        raise ValueError("A real visual observation and reviewer are required")
    for at in times:
        record = source.get("frames", {}).get(f"{at:.3f}")
        if not record or digest((ws.root / record["path"]).read_bytes()) != record["sha256"]:
            raise ValueError("Frame missing or changed; inspect the actual frame first")
        record.update(status="inspected", reviewer=reviewer, inspected_at=now())
    receipt = {"times": times, "observation": observation, "reviewer": reviewer,
               "reviewed_at": now(), "scope": "listed_still_frames_only_not_full_motion"}
    source.setdefault("visual_reviews", []).append(receipt)
    source["visual_status"] = "sampled_frames_inspected"
    ws.save_source(source)
    return receipt

