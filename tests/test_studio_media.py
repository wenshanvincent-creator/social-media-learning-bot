import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import imageio_ffmpeg
import pytest

from sociallearn import adapters, media
from sociallearn.store import Workspace, atomic_json, digest
from sociallearn.wiki import Wiki


@pytest.fixture
def archive(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    run = ws.create_run("synthetic Studio media test")
    video = {"id": "1234567890", "url": "https://www.douyin.com/video/1234567890",
             "title": "Synthetic sample", "duration": 1}
    adapters.import_douyin(ws, run["id"], {"id": "discovery", "status": "completed", "result": {"videos": [video]}})
    yield ws, run["id"], video, tmp_path
    ws.db.close()


def block(block_id, citations):
    return {"id": block_id, "title": "Synthetic rule", "summary": "Synthetic evidence",
            "kind": "principle", "applies_to": "design", "verification_status": "agent_synthesis",
            "body": "A synthetic rule used only in the test.", "citations": citations}


def test_studio_video_to_frames_transcript_and_wiki_without_repeated_download(archive, monkeypatch):
    ws, run, video, folder = archive
    clip = folder / "owned-fixture.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x64:r=3:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                   check=True, capture_output=True)
    raw = clip.read_bytes()
    job = {"id": "download-job", "type": "download", "status": "completed", "video": video, "bytes": len(raw)}
    posts = []
    class Server(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append((self.path, self.headers["X-Studio-Client"],
                          json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.reply(json.dumps({"jobs": [job]}).encode())
        def do_GET(self):
            if self.path == "/api/state":
                self.reply(json.dumps({"jobs": [job]}).encode())
            else:
                assert self.path == "/api/downloads/download-job/file"
                self.reply(raw, "video/mp4")
        def reply(self, body, mime="application/json"):
            self.send_response(200); self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        ws.consent(run, "douyin", True, "synthetic authorization")
        origin = f"http://127.0.0.1:{server.server_port}"
        receipt = folder / "receipt.json"
        source_id = "douyin-" + video["id"]
        downloaded = adapters.studio_video(ws, run, source_id, origin, receipt)
        assert downloaded["visual_status"] == "downloaded_not_inspected"
        assert not (ws.root / "private/raw" / source_id / "video.part").exists()
        assert adapters.studio_video(ws, run, source_id, origin, receipt)["cached"] is True
        assert posts == [("/api/downloads", "1", {"ids": [video["id"]]})]
        media.frame(ws, source_id, 0.5)
        wiki = Wiki(ws)
        wiki.put(block("frame-rule", [{"source_id": source_id, "frame_times": [0.5]}]))
        calls = []
        def transcribe(path, model):
            calls.append((path, model))
            return [{"id": "s0001", "start": 0, "end": 1, "text": "Synthetic spoken instruction"}]
        monkeypatch.setattr(media, "transcribe", transcribe)
        assert media.transcribe_local(ws, run, source_id)["transcript_review_status"] == "needs_review"
        assert not (ws.root / downloaded["video_path"]).exists()
        assert "video_path" not in ws.source(source_id)
        assert clip.exists()  # The original owned input is not the managed copy.
        assert media.transcribe_local(ws, run, source_id)["cached"] is True and len(calls) == 1
        assert wiki.read(["frame-rule"])["blocks"]  # New transcript does not invalidate unchanged visual evidence.
        wiki.put(block("text-rule", [{"source_id": source_id, "segment_ids": ["s0001"]}]))
        adapters.import_douyin(ws, run, {"id": "later-search", "status": "completed", "result": {"videos": [video]}})
        assert ws.source(source_id)["transcript_origin"] == "local_whisper"
        assert wiki.read(["text-rule", "frame-rule"])["deferred"] == []
        changed = ws.source(source_id)
        changed["video_hash"] = "changed-video"
        ws.save_source(changed)
        assert wiki.read(["frame-rule"])["deferred"][0]["reason"]["status"] == "source_changed"
        assert wiki.read(["text-rule"])["blocks"]  # Only the affected evidence channel is invalidated.
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_studio_video_needs_consent_and_unknown_post_never_repeats(archive, monkeypatch):
    ws, run, video, folder = archive
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise ValueError("Submission outcome unknown")
    monkeypatch.setattr(adapters, "_studio_request", uncertain)
    args = (ws, run, "douyin-" + video["id"], "http://127.0.0.1:4321", folder / "receipt.json")
    with pytest.raises(PermissionError):
        adapters.studio_video(*args)
    assert calls == []
    ws.consent(run, "douyin", True, "synthetic authorization")
    with pytest.raises(ValueError, match="unknown"):
        adapters.studio_video(*args)
    with pytest.raises(ValueError, match="outcome unknown"):
        adapters.studio_video(*args)
    assert len(calls) == 1


def test_studio_completed_job_identity_must_match_source(archive, monkeypatch):
    ws, run, video, folder = archive
    ws.consent(run, "douyin", True, "synthetic authorization")
    monkeypatch.setattr(adapters, "_studio_request", lambda *a: {"jobs": [{"id": "known"}]})
    monkeypatch.setattr(adapters, "_studio_wait", lambda *a: {"id": "known", "type": "download", "status": "completed",
        "video": {"id": "9999999999", "url": "https://www.douyin.com/video/9999999999"}, "bytes": 10})
    with pytest.raises(ValueError, match="does not match"):
        adapters.studio_video(ws, run, "douyin-" + video["id"], "http://127.0.0.1:4321", folder / "receipt.json")


def test_existing_captions_are_not_replaced_by_local_whisper(archive, monkeypatch):
    ws, run, video, _ = archive
    source_id = "douyin-" + video["id"]
    path = ws.root / "private/raw" / source_id / "segments.json"
    atomic_json(path, [{"id": "s0001", "start": 0, "end": 1, "text": "Archived manual caption"}])
    source = ws.source(source_id)
    source.update(status="transcribed", transcript_origin="manual", segments_path=str(path.relative_to(ws.root)),
                  segments_sha256=digest(path.read_bytes()), content_hash=digest(path.read_bytes()))
    ws.save_source(source)
    def unexpected(*args):
        pytest.fail("Existing captions must win over Whisper")
    monkeypatch.setattr(media, "transcribe", unexpected)
    assert media.transcribe_local(ws, run, source_id)["transcript_origin"] == "manual"


def test_incomplete_file_is_removed_and_never_registered(tmp_path, monkeypatch):
    from io import BytesIO
    monkeypatch.setattr(adapters, "urlopen", lambda *args, **kwargs: BytesIO(b"incomplete file"))
    output = tmp_path / "video.mp4"
    with pytest.raises(ValueError, match="byte count"):
        adapters._studio_file("http://127.0.0.1:4321", {"id": "existing-job", "bytes": 500}, output, 1000)
    assert not output.exists() and not output.with_suffix(".part").exists()
