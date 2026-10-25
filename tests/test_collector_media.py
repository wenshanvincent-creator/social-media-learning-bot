import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import imageio_ffmpeg
import pytest

from sociallearn import adapters, collector_media, media
from sociallearn.analysis import AnalysisJobs
from sociallearn.store import Workspace, atomic_json, digest
from sociallearn.wiki import Wiki


@pytest.fixture
def archive(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    run = ws.create_run("synthetic collector media")
    yield ws, run["id"], tmp_path
    ws.db.close()


def export(folder, kinds):
    note_id = "a" * 24
    folder.mkdir(exist_ok=True)
    entries, records = [], []
    for i, (kind, raw) in enumerate(kinds, 1):
        name = f"{i:03}-{kind}." + ("mp4" if kind == "video" else "png")
        (folder / name).write_bytes(raw)
        url = f"https://cdn.example.org/{name}?sign=synthetic-private-signature"
        entries.append({"kind": kind, "url": url})
        records.append({"index": i, "kind": kind, "file": name, "size": len(raw), "sha256": digest(raw),
                        "url": url, "status": "downloaded"})
    atomic_json(folder / "note.json", {"note_id": note_id, "title": "Owned synthetic example", "description": "Exported post explanation",
        "canonical_url": "https://www.xiaohongshu.com/explore/" + note_id, "media": entries})
    atomic_json(folder / "downloads.json", {"schema_version": 1, "note_id": note_id, "status": "complete", "files": records})
    return "xiaohongshu-" + note_id


def candidate(source_id, image_id="image-001"):
    return {"id": "image-design-rule", "title": "Image observation", "summary": "Synthetic image evidence",
            "kind": "case", "applies_to": "layout", "verification_status": "agent_synthesis", "body": "A synthetic visual observation.",
            "citations": [{"source_id": source_id, "image_ids": [image_id]}]}


def test_images_have_precise_worker_citations_and_independent_freshness(archive):
    ws, run, root = archive
    # File hashes are the subject of this test; no claim of real image viewing is made.
    source_id = export(root / "export", [("image", b"synthetic first image"), ("image", b"synthetic second image")])
    imported = collector_media.import_redcrack_media(ws, run, root / "export")
    assert imported["images"] == 2 and imported["visual_status"] == "media_archived_not_inspected"
    assert "synthetic-private-signature" not in json.dumps(ws.source(source_id))
    page = collector_media.source_images(ws, source_id, limit=1)
    assert page["next_offset"] == 1 and Path(page["images"][0]["absolute_path"]).is_file()
    jobs = AnalysisJobs(ws)
    job = jobs.prepare(run, source_id, "Inspect the first image only")
    payload = {"source_id": source_id, "summary": "First image checked", "coverage": {"mode": "images", "image_ids": ["image-001"]},
               "candidates": [candidate(source_id, "image-002")]}
    with pytest.raises(ValueError, match="inside reported review coverage"):
        jobs.submit(job["id"], payload)
    payload["candidates"] = [candidate(source_id)]
    jobs.submit(job["id"], payload)
    jobs.admit(job["id"], "image-design-rule")
    wiki = Wiki(ws)
    source = ws.source(source_id)
    (ws.root / source["segments_path"]).write_bytes(b"unrelated broken post text")
    assert wiki.read(["image-design-rule"])["blocks"]
    export(root / "export", [("image", b"synthetic first image"), ("image", b"new second image")])
    collector_media.import_redcrack_media(ws, run, root / "export")
    assert wiki.read(["image-design-rule"])["blocks"]  # An uncited image change does not stale this rule.
    old = ws.source(source_id)["images"]["image-001"]
    export(root / "export", [("image", b"replacement first image"), ("image", b"new second image")])
    collector_media.import_redcrack_media(ws, run, root / "export")
    assert wiki.read(["image-design-rule"])["deferred"][0]["reason"]["status"] == "source_changed"
    revision = ws.root / "state/source-revisions" / source_id / old["sha256"] / Path(old["path"]).name
    assert revision.read_bytes() == b"synthetic first image"


def test_bad_manifest_cannot_create_source_or_read_outside_export(archive):
    ws, run, root = archive
    source_id = export(root / "export", [("image", b"image")])
    manifest = json.loads((root / "export/downloads.json").read_text(encoding="utf-8"))
    manifest["files"][0]["file"] = "../outside.png"
    atomic_json(root / "export/downloads.json", manifest)
    with pytest.raises(ValueError, match="filename"):
        collector_media.import_redcrack_media(ws, run, root / "export")
    with pytest.raises(ValueError, match="Unknown source"):
        ws.source(source_id)


def test_video_channel_preserves_post_text_and_can_use_shared_asr_and_frames(archive, monkeypatch):
    ws, run, root = archive
    clip = root / "owned.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x64:r=3:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                   capture_output=True, check=True)
    source_id = export(root / "export", [("video", clip.read_bytes())])
    result = collector_media.import_redcrack_media(ws, run, root / "export")
    child_id = result["video_source_id"]
    assert child_id != source_id and ws.source(child_id)["parent_source_id"] == source_id
    monkeypatch.setattr(media, "transcribe", lambda *a: [{"id": "s0001", "start": 0, "end": 1, "text": "Synthetic spoken example"}])
    media.transcribe_local(ws, run, child_id)
    assert ws.read_segments(ws.source(source_id))[0]["text"] == "Exported post explanation"
    assert ws.read_segments(ws.source(child_id))[0]["text"] == "Synthetic spoken example"
    assert media.frame(ws, child_id, 0.5)["status"] == "extracted_not_inspected"
    collector_media.import_redcrack_media(ws, run, root / "export")
    assert ws.source(child_id)["transcript_origin"] == "local_whisper"
    wiki = Wiki(ws)
    wiki.put({**candidate(child_id), "id": "spoken-rule", "citations": [{"source_id": child_id, "segment_ids": ["s0001"]}]})
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=64x64:r=3:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                   capture_output=True, check=True)
    export(root / "export", [("video", clip.read_bytes())])
    collector_media.import_redcrack_media(ws, run, root / "export")
    assert "segments_path" not in ws.source(child_id) and "frames" not in ws.source(child_id)
    assert ws.source(child_id)["visual_status"] == "downloaded_not_inspected"
    assert ws.read_segments(ws.source(source_id))[0]["text"] == "Exported post explanation"
    assert wiki.read(["spoken-rule"])["deferred"][0]["reason"]["status"] == "source_changed"


def studio(archive, monkeypatch, tracks):
    ws, run, root = archive
    video = {"id": "1234567890", "url": "https://www.douyin.com/video/1234567890", "duration": 1}
    job = {"id": "resolve-job", "type": "resolve", "status": "completed", "result": {"videos": [video]}}
    adapters.import_douyin(ws, run, job)
    ws.consent(run, "douyin", True, "synthetic human authorization")
    receipt = root / "resolve.json"
    atomic_json(receipt, {"run_id": run, "origin": "http://127.0.0.1:4321", "job_id": job["id"]})
    monkeypatch.setattr(adapters, "_studio_wait", lambda *a: job)
    descriptor = {"schemaVersion": 1, "canonicalUrl": video["url"], "videoId": video["id"], "subtitleTracks": tracks}
    monkeypatch.setattr(adapters, "_studio_request", lambda *a: descriptor)
    return ws, run, "douyin-" + video["id"], receipt, descriptor


def test_page_captions_are_bound_to_resolve_job_and_precede_whisper(archive, monkeypatch):
    args = studio(archive, monkeypatch, [{"url": "https://a.douyin.com/captions.vtt?token=synthetic-private-signature", "language": "zh"}])
    ws, run, source_id, receipt, descriptor = args
    monkeypatch.setattr(collector_media, "_caption_bytes", lambda *a: b"WEBVTT\n\n00:00.000 --> 00:00.900\nSynthetic caption\n\n")
    report = collector_media.studio_captions(ws, run, source_id, receipt)
    assert report["asr_started"] is False and report["transcript_review_status"] == "needs_review"
    assert "synthetic-private-signature" not in json.dumps(ws.source(source_id))
    assert ws.read_segments(ws.source(source_id))[0]["text"] == "Synthetic caption"
    assert media.transcribe_local(ws, run, source_id)["transcript_origin"] == "platform_page_track"
    assert collector_media.studio_captions(ws, run, source_id, receipt)["cached"] is True
    descriptor["videoId"] = "9999999999"
    with pytest.raises(ValueError, match="do not match"):
        collector_media.studio_captions(ws, run, source_id, receipt, refresh=True)


def test_page_track_absence_is_scoped_and_access_failure_pauses(archive, monkeypatch):
    ws, run, source_id, receipt, descriptor = studio(archive, monkeypatch, [])
    result = collector_media.studio_captions(ws, run, source_id, receipt)
    assert result["status"] == "no_page_tracks" and result["coverage"] == "page_tracks_only"
    assert result["asr_started"] is False
    descriptor["subtitleTracks"] = [{"url": "https://a.douyin.com/track.vtt"}]
    def restricted(*a):
        raise HTTPError("https://a.douyin.com/track.vtt", 403, "Forbidden", {}, None)
    monkeypatch.setattr(collector_media, "_caption_bytes", restricted)
    with pytest.raises(media.PlatformError):
        collector_media.studio_captions(ws, run, source_id, receipt)
    with pytest.raises(PermissionError):
        ws.check_access(run, "douyin")


@pytest.mark.parametrize("url", ["http://a.douyin.com/track.vtt", "https://douyin.com.example.org/track.vtt", "https://:secret@a.douyin.com/track.vtt"])
def test_caption_host_policy_rejects_untrusted_locations(url):
    with pytest.raises(ValueError, match="media policy"):
        collector_media._caption_url(url)
