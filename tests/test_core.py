import json
import io
from datetime import date
from pathlib import Path

import pytest

from sociallearn.auth import login, netscape
from sociallearn.freshness import assess
from sociallearn.media import identify, parse_subtitles
from sociallearn.store import Workspace, atomic_json, digest
from sociallearn.wiki import Wiki
from sociallearn import auth, media


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path)
    run = ws.create_run("design")
    segments = [{"id": "s0001", "start": 0, "end": 3, "text": "Typography needs clear hierarchy."}]
    path = tmp_path / "private/raw/demo/segments.json"
    atomic_json(path, segments)
    ws.save_source({"id": "demo", "run_id": run["id"], "url": "https://example.org/demo",
                    "observed_at": "2026-10-24T00:00:00+00:00", "status": "transcribed",
                    "content_hash": digest(json.dumps(segments).encode()),
                    "segments_sha256": digest(path.read_bytes()),
                    "segments_path": str(path.relative_to(tmp_path))})
    yield ws, run
    ws.db.close()


def note(block_id="subtitle-layout", **updates):
    return {"id": block_id, "title": "字幕排版", "summary": "字幕层级和可读性",
            "kind": "principle", "applies_to": "技术讲解视频", "aliases": ["typography", "字幕"],
            "verification_status": "source_described", "body": "保持明确的字号层级，并检查实际画幅。",
            "citations": [{"source_id": "demo", "segment_ids": ["s0001"]}], **updates}


def test_consent_blocks_profile_open_and_does_not_transfer(workspace, monkeypatch):
    ws, run = workspace
    def unexpected(*args, **kwargs):
        pytest.fail("Profile must not be opened without consent")
    monkeypatch.setattr("sociallearn.auth._context", unexpected)
    with pytest.raises(PermissionError):
        login(ws, run["id"], "bilibili")
    ws.consent(run["id"], "bilibili", True, "human approved this task")
    another = ws.create_run("another")
    with pytest.raises(PermissionError):
        ws.require_consent(another["id"], "bilibili")
    ws.close_run(run["id"])
    with pytest.raises(PermissionError):
        ws.require_consent(run["id"], "bilibili")


def test_manual_login_can_review_a_pause_without_resuming_acquisition(workspace, monkeypatch):
    ws, run = workspace
    ws.pause("youtube", "HTTP 429")
    ws.consent(run["id"], "youtube", True, "human approved this test")
    class Page:
        def goto(self, *args, **kwargs): pass
        def bring_to_front(self): pass
    class Context:
        pages = [Page()]
        def cookies(self, *args): return [{"name": "SAPISID", "value": "synthetic"}]
        def close(self): pass
    class Engine:
        def stop(self): pass
    monkeypatch.setattr(auth, "_context", lambda *a, **k: (Engine(), Context()))
    assert login(ws, run["id"], "youtube")["status"] == "session_present"
    with pytest.raises(PermissionError, match="paused"):
        ws.check_access(run["id"], "youtube")


def test_imported_session_is_scoped_authorized_and_removed_at_run_close(workspace, monkeypatch):
    ws, run = workspace
    source = ws.root / "owned-cookie-fixture.txt"
    source.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsynthetic-secret\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        auth.import_cookie_file(ws, run["id"], "youtube", source)
    ws.consent(run["id"], "youtube", True, "human approval")
    auth.import_cookie_file(ws, run["id"], "youtube", source)
    imported = ws.root / "private/sessions" / run["id"] / "youtube.cookies"
    assert imported.is_file()
    monkeypatch.setattr(auth, "_context", lambda *a, **k: pytest.fail("Imported session must not open browser"))
    with auth.cookie_file(ws, run["id"], "youtube", True) as file:
        temporary = Path(file)
        assert temporary.is_file() and ".youtube.com" in temporary.read_text(encoding="utf-8")
    assert not temporary.exists()
    source.write_text(".other.example\tTRUE\t/\tTRUE\t0\tNAME\tsynthetic\n", encoding="utf-8")
    with pytest.raises(ValueError, match="current platform"):
        auth.import_cookie_file(ws, run["id"], "youtube", source)
    ws.close_run(run["id"])
    assert not imported.exists()
    assert source.exists()  # Never delete a human's original export.


def test_restriction_retains_error_code_without_logging_sensitive_exception(workspace):
    ws, _ = workspace
    with pytest.raises(media.PlatformError, match="HTTP 429") as exc:
        media.handle_error(ws, "youtube", RuntimeError("HTTP Error 429 https://example.invalid/?token=synthetic-secret"))
    assert "synthetic-secret" not in str(exc.value)
    assert ws.db.execute("SELECT reason FROM pauses WHERE platform='youtube'").fetchone()[0] == "HTTP 429"


@pytest.mark.parametrize("caption_mode", ["manual", "automatic", "none"])
def test_caption_metadata_does_not_require_video_formats_but_audio_download_stays_strict(workspace, monkeypatch, caption_mode):
    ws, run = workspace
    calls = []
    audio = ws.root / "private/raw/youtube-AAAAAAAAAAA/audio.fixture"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"stubbed ASR input; not actual audio")
    class YDL:
        def __init__(self, params): self.params = params
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download=False):
            calls.append(download)
            if not download:
                assert self.params["ignore_no_formats_error"] is True
            else:
                assert not self.params.get("ignore_no_formats_error")
            return {"duration": 30, "title": "Owned caption fixture", "formats": [], "language": "en",
                    "subtitles": {"en": [{"ext": "json3", "url": "https://example.invalid/captions"}]} if caption_mode == "manual" else {},
                    "automatic_captions": {"zh-Hans": [{"ext": "json3", "url": "https://example.invalid/translated"}],
                                           "en-orig": [{"ext": "json3", "url": "https://example.invalid/native"}]} if caption_mode == "automatic" else {}}
        def urlopen(self, request):
            if caption_mode == "automatic":
                assert request.url.endswith("/native")
            return io.BytesIO(json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Keep the complete instruction."}]}]}).encode())
        def prepare_filename(self, info): return str(audio)
    monkeypatch.setattr("yt_dlp.YoutubeDL", YDL)
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: [{"id": "s0001", "start": 0, "end": 1, "text": "Owned ASR fixture"}])
    result = media.fetch(ws, run["id"], "https://www.youtube.com/watch?v=AAAAAAAAAAA", asr=True)
    assert result["status"] == "transcribed" and len(ws.read_segments(result)) == 1
    assert result["transcript_origin"] == {"manual": "manual", "automatic": "automatic", "none": "local_whisper"}[caption_mode]
    if caption_mode == "automatic":
        assert result["language"] == "en-orig"
    assert calls == ([False, True] if caption_mode == "none" else [False])
    if caption_mode == "none":
        assert not audio.exists() and result["media_cleanup"]["audio"]["status"] == "deleted_after_conversion"


def test_platform_pause_survives_new_run(workspace):
    ws, run = workspace
    ws.pause("bilibili", "verification required")
    with pytest.raises(PermissionError):
        ws.check_access(ws.create_run("new")["id"], "bilibili")
    ws.check_access(run["id"], "youtube")


def test_human_pause_resolution_is_scoped_and_does_not_grant_cookies(workspace):
    ws, run = workspace
    ws.pause("bilibili", "synthetic access restriction")
    ws.pause("youtube", "another restriction")
    with pytest.raises(ValueError, match="resolution report"):
        ws.resolve_pause(run["id"], "bilibili", " ")
    report = ws.resolve_pause(run["id"], "bilibili", "Synthetic human confirmed the restriction was resolved")
    ws.check_access(run["id"], "bilibili")
    with pytest.raises(PermissionError):
        ws.check_access(run["id"], "youtube")
    with pytest.raises(PermissionError):
        ws.require_consent(run["id"], "bilibili")
    assert report["collection_restarted"] is False
    assert json.loads(Path(report["receipt"]).read_text(encoding="utf-8"))["prior_pause"]["reason"]


def test_cookie_export_is_domain_scoped():
    cookies = [{"name": "SESSDATA", "value": "synthetic", "domain": ".bilibili.com",
                "path": "/", "secure": True, "httpOnly": True, "expires": -1},
               {"name": "unrelated", "value": "private", "domain": "example.org", "path": "/"}]
    text = netscape(cookies, "bilibili.com")
    assert "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic" in text
    assert "unrelated" not in text


def test_multipart_identity_is_preserved():
    a = identify("https://www.bilibili.com/video/BV1xx411c7mD/?p=1&tracking=secret")
    b = identify("https://www.bilibili.com/video/BV1xx411c7mD/?p=2")
    assert a[1] != b[1]
    assert "tracking" not in a[2]
    with pytest.raises(ValueError):
        identify("https://bilibili.com.example.org/video/BV1xx411c7mD")


@pytest.mark.parametrize("ext,raw", [
    ("vtt", "WEBVTT\n\n00:00.000 --> 00:03.000\nHello\n\n"),
    ("srt", "1\n00:00:00,000 --> 00:00:03,000\nHello\n\n"),
    ("json", '{"body":[{"from":0,"to":3,"content":"Hello"}]}'),
    ("json3", '{"events":[{"tStartMs":0,"dDurationMs":3000,"segs":[{"utf8":"Hello"}]}]}'),
])
def test_subtitle_formats_keep_timing(ext, raw):
    result = parse_subtitles(raw, ext)
    assert result == [{"id": "s0001", "start": 0, "end": 3, "text": "Hello"}]


def test_empty_subtitle_is_not_success():
    with pytest.raises(ValueError):
        parse_subtitles("WEBVTT\n", "vtt")


def test_model_freshness_never_uses_edit_time():
    meta = {"kind": "model", "provider": "anthropic", "model_id": "synthetic-model-id",
            "official_url": "https://www.anthropic.com/news", "verified_at": "2026-10-01",
            "updated_at": "2026-10-24"}
    assert assess(meta, today=date(2026, 10, 24))["status"] == "review_due"
    meta["verified_at"] = "2026-10-24"
    assert assess(meta, today=date(2026, 10, 24), current=True)["status"] == "live_check_required"
    meta["official_url"] = "https://anthropic.com.example.org/news"
    assert assess(meta, today=date(2026, 10, 24))["status"] == "unverified"
    meta["verified_at"] = "2026-10-25"
    assert assess(meta, today=date(2026, 10, 24))["status"] == "invalid"


def test_principles_and_versioned_implementation_are_different():
    assert assess({"kind": "principle", "published_at": "1990-01-01"})["status"] == "usable"
    assert assess({"kind": "implementation", "verified_at": "2026-10-24"},
                  today=date(2026, 10, 24))["status"] == "unverified"
    assert assess({"kind": "implementation", "version": "1", "verified_at": "2026-10-24"},
                  today=date(2026, 10, 24), target_version="2")["status"] == "incompatible"


def test_selective_chinese_retrieval_budget_and_repeat(workspace):
    ws, _ = workspace
    wiki = Wiki(ws)
    wiki.put(note())
    wiki.put(note("color-system", title="配色", summary="颜色与对比", aliases=["color"],
                  body="配色案例，不适用字幕排版。", applies_to="配色方案"))
    result = wiki.search("字幕")
    assert [c["id"] for c in result["cards"]] == ["subtitle-layout"]
    assert result["body_loaded"] is False
    assert "body" not in result["cards"][0]
    limited = wiki.read(["subtitle-layout"], budget=1)
    assert not limited["blocks"] and limited["deferred"][0]["reason"] == "budget"
    first = wiki.read(["subtitle-layout"], session="context-a")
    assert "body" in first["blocks"][0]
    assert "body" not in wiki.read(["subtitle-layout"], session="context-a")["blocks"][0]
    assert "body" in wiki.read(["subtitle-layout"], session="context-after-compaction")["blocks"][0]


def test_changed_source_and_note_are_not_silently_reused(workspace):
    ws, _ = workspace
    wiki = Wiki(ws)
    saved = wiki.put(note())
    with pytest.raises(ValueError, match="current hash"):
        wiki.put(note(body="new"))
    wiki.put(note(body="new"), expected_hash=saved["hash"])
    source = ws.source("demo")
    source["content_hash"] = "changed"
    ws.save_source(source)
    result = wiki.read(["subtitle-layout"])
    assert not result["blocks"]
    assert result["deferred"][0]["reason"]["status"] == "source_changed"


def test_changed_frame_is_deferred_even_if_video_hash_did_not_change(workspace):
    ws, _ = workspace
    image = ws.root / "private/raw/demo/frame-1.png"
    image.write_bytes(b"synthetic frame original")
    source = ws.source("demo")
    source["frames"] = {"1.000": {"path": str(image.relative_to(ws.root)), "sha256": digest(image.read_bytes())}}
    ws.save_source(source)
    wiki = Wiki(ws)
    wiki.put(note(citations=[{"source_id": "demo", "frame_times": [1.0]}]))
    assert wiki.read(["subtitle-layout"])["blocks"]
    image.write_bytes(b"synthetic changed frame")
    result = wiki.read(["subtitle-layout"])
    assert not result["blocks"]
    assert result["deferred"][0]["reason"]["status"] == "source_changed"


def test_invalid_citation_does_not_enter_knowledge(workspace):
    ws, _ = workspace
    wiki = Wiki(ws)
    with pytest.raises(ValueError, match="segment IDs"):
        wiki.put(note(citations=[{"source_id": "demo", "segment_ids": ["invented"]}]))


def test_chinese_cli_survives_windows_pipe_encoding(tmp_path):
    import os
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-m", "sociallearn", "--root", str(tmp_path),
                             "init", "--topic", "字幕设计"], capture_output=True,
                            env={**os.environ, "PYTHONIOENCODING": "cp1252"}, check=True)
    assert json.loads(result.stdout.decode("utf-8"))["topic"] == "字幕设计"


def test_cookie_profile_is_closed_before_network_work(workspace, monkeypatch):
    from sociallearn.auth import cookie_file
    ws, run = workspace
    ws.consent(run["id"], "bilibili", True, "human authorization")
    events = []
    class Context:
        def cookies(self, url):
            return [{"name": "SESSDATA", "value": "synthetic", "domain": ".bilibili.com", "path": "/"}]
        def close(self):
            events.append("closed")
    class Engine:
        def stop(self):
            events.append("stopped")
    monkeypatch.setattr("sociallearn.auth._context", lambda *a, **kw: (Engine(), Context()))
    with cookie_file(ws, run["id"], "bilibili", True) as name:
        from pathlib import Path
        assert events == ["closed", "stopped"]
        path = Path(name)
        assert path.exists()
    assert not path.exists()


def test_modified_raw_segments_cannot_silently_support_claims(workspace):
    ws, _ = workspace
    wiki = Wiki(ws)
    wiki.put(note())
    source = ws.source("demo")
    path = ws.root / source["segments_path"]
    path.write_text('[{"id":"s0001","text":"Changed claim"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        ws.read_segments(source)
    with pytest.raises(ValueError, match="changed"):
        wiki.put(note("new-block"))
    assert wiki.read(["subtitle-layout"])["deferred"][0]["reason"]["status"] == "source_changed"


def test_source_snapshot_preserves_previous_text(workspace):
    ws, _ = workspace
    source = ws.source("demo")
    ws.snapshot_source("demo")
    path = ws.root / source["segments_path"]
    path.write_text("[]", encoding="utf-8")
    prior = ws.root / "state/source-revisions/demo" / source["content_hash"] / "segments.json"
    assert json.loads(prior.read_text(encoding="utf-8"))[0]["text"] == "Typography needs clear hierarchy."

