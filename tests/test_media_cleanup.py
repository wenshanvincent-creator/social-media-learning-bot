import pytest

from sociallearn import media, ocr
from sociallearn.store import Workspace, digest


def archive(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    run = ws.create_run("owned cleanup fixture")["id"]
    sid = "youtube-AAAAAAAAAAA"
    path = ws.root / "private/raw" / sid / "video.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"owned media, processing stubbed")
    source = {"id": sid, "run_id": run, "platform": "youtube", "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
              "duration": 10, "status": "metadata_only", "video_path": str(path.relative_to(ws.root)),
              "video_hash": digest(path.read_bytes()), "transcript_origin": "none"}
    ws.save_source(source)
    return ws, run, sid, path


@pytest.mark.parametrize("failure", ["transcribe", "save"])
def test_failed_conversion_or_transcript_persistence_keeps_input(tmp_path, monkeypatch, failure):
    ws, run, sid, path = archive(tmp_path)
    try:
        def transcribe(*args):
            if failure == "transcribe":
                raise ValueError("Owned conversion failure")
            return [{"id": "s0001", "start": 0, "end": 1, "text": "Owned transcript"}]
        monkeypatch.setattr(media, "transcribe", transcribe)
        if failure == "save":
            monkeypatch.setattr(media, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("Owned disk failure")))
        with pytest.raises((ValueError, OSError)):
            media.transcribe_local(ws, run, sid)
        assert path.is_file() and "media_cleanup" not in ws.source(sid)
    finally:
        ws.db.close()


@pytest.mark.parametrize("unsafe", ["external", "changed"])
def test_cleanup_refuses_original_external_file_or_changed_working_copy(tmp_path, unsafe):
    ws, _, sid, path = archive(tmp_path)
    try:
        if unsafe == "external":
            original = tmp_path / "video.mp4"
            original.write_bytes(path.read_bytes())
            source = ws.source(sid)
            source["video_path"] = str(original)
            ws.save_source(source)
            path = original
        else:
            path.write_bytes(b"replaced after conversion")
        with pytest.raises(ValueError, match="Refusing|changed"):
            media.cleanup_converted_media(ws, sid)
        assert path.exists()
    finally:
        ws.db.close()


def test_shared_import_copy_is_deleted_but_both_evidence_hashes_remain(tmp_path, monkeypatch):
    ws, run, sid, path = archive(tmp_path)
    try:
        parent = {**ws.source(sid), "id": "owned-parent", "video_source_id": sid, "content_hash": "parent-text"}
        ws.save_source(parent)
        child = ws.source(sid)
        child["parent_source_id"] = parent["id"]
        ws.save_source(child)
        monkeypatch.setattr(media, "transcribe", lambda *a: [{"id": "s0001", "start": 0, "end": 1, "text": "Owned transcript"}])
        media.transcribe_local(ws, run, sid)
        assert not path.exists()
        for source_id in (sid, parent["id"]):
            source = ws.source(source_id)
            assert "video_path" not in source and source["video_hash"] == child["video_hash"]
            assert source["media_cleanup"]["video"]["status"] == "deleted_after_conversion"
        assert ws.read_segments(ws.source(sid))[0]["text"] == "Owned transcript"
        assert ws.source(parent["id"])["content_hash"] == "parent-text"
    finally:
        ws.db.close()


@pytest.mark.parametrize("outcome", ["complete", "ocr_failure", "deferred"])
def test_ocr_cleanup_waits_for_saved_complete_output_and_reuses_cached_frames(tmp_path, monkeypatch, outcome):
    ws, run, sid, path = archive(tmp_path)
    try:
        downloads = []
        monkeypatch.setattr(media, "download_video", lambda *a, **k: downloads.append(sid))
        def frame(w, source_id, at):
            image = w.root / "private/raw" / source_id / f"frame-{at:.3f}.png"
            image.write_bytes(b"owned image, OCR stubbed")
            source = w.source(source_id)
            record = {"path": str(image.relative_to(w.root)), "sha256": digest(image.read_bytes()), "video_hash": source["video_hash"]}
            source.setdefault("frames", {})[f"{at:.3f}"] = record
            w.save_source(source)
            return record
        monkeypatch.setattr(media, "frame", frame)
        def recognize(image=None, language=""):
            if image is None:
                return {"available": True}
            if outcome == "ocr_failure":
                raise ocr.OcrUnavailable("Owned OCR failure")
            return {"text": "Keep parameter 24", "available": True}
        monkeypatch.setattr(ocr, "windows_ocr", recognize)
        cues = [{"at": 1, "segment_id": "s0001"}]
        if outcome == "deferred":
            cues.append({"at": 9, "segment_id": "s0002"})
        if outcome == "ocr_failure":
            with pytest.raises(ocr.OcrUnavailable):
                ocr.extract_screen_text(ws, run, sid, cues, max_images=1)
            assert path.exists()
        else:
            result = ocr.extract_screen_text(ws, run, sid, cues, max_images=1)
            assert path.exists() == (outcome == "deferred")
            assert (path.parent / "ocr-1.000.json").exists()
            if outcome == "complete":
                assert ocr.extract_screen_text(ws, run, sid, cues, max_images=1)["items"] == result["items"]
                assert downloads == [sid]  # Cached OCR survives deletion without another download.
    finally:
        ws.db.close()
