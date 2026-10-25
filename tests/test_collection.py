import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from sociallearn import cleaning, collection, media, ocr, screening
from sociallearn.analysis import AnalysisJobs
from sociallearn.store import Workspace, atomic_json, digest
from sociallearn.cli import main


def lesson():
    return [{"id": f"s{i}", "start": i * 5, "end": i * 5 + 4,
             "text": f"Step {i}: first set typography contrast. For example use a different title weight because the design needs clear hierarchy. Avoid crowding when space is limited. Then check the layout with example {i}."}
            for i in range(8)]


def metadata(source_id, **extra):
    return {"id": source_id, "url": "https://www.youtube.com/watch?v=" + source_id.split("-")[-1],
            "title": "Typography design tutorial", "duration": 60, "platform": "youtube",
            "published_at": "20161106", "description": "Original design explanation", **extra}


def test_quality_is_not_bought_with_followers_and_unknown_is_not_zero():
    p = screening.policy_config()
    source = metadata("youtube-AAAAAAAAAAA", view_count=100_000_000, channel_follower_count=10_000_000)
    result = screening.screen(source, [{"id": "s1", "text": "Buy now and subscribe. Join my course."}], "typography", "principle", p)
    assert result["decision"] == "rejected" and result["reasons"] == ["high_call_to_action_segment_ratio"]
    useful = screening.screen(metadata("youtube-AAAAAAAAAAA"), lesson(), "typography design", "principle", p)
    assert useful["decision"] == "selected" and useful["dimensions"]["popularity"] is None
    assert "popularity" in useful["missing_dimensions"]


def test_claim_freshness_and_repost_evidence_are_separate():
    p = screening.policy_config()
    old = metadata("youtube-AAAAAAAAAAA")
    assert screening.metadata_gate(old, "principle", p, date(2026, 10, 24)) is None
    assert screening.metadata_gate(old, "implementation", p, date(2026, 10, 24))["decision"] == "review"
    assert screening.metadata_gate({**old, "description": "Music credit to Composer"}, "principle", p) is None
    assert screening.metadata_gate({**old, "title": "Reuploaded tutorial"}, "principle", p)["decision"] == "rejected"
    text = "\n".join(x["text"] for x in lesson())
    duplicate = screening.screen(old, lesson(), "typography", "principle", p, {"first": screening.fingerprint(text)})
    assert duplicate["duplicate_of"] == "first" and duplicate["originality"] == "not_determined"


def test_cleanup_preserves_code_and_citation_map():
    original = [{"id": "s1", "start": 0, "end": 1, "text": "[Music]"},
                {"id": "s2", "start": 1, "end": 2, "text": "<c>Keep &amp; compare</c>"},
                {"id": "s3", "start": 2, "end": 3, "text": "Keep & compare"},
                {"id": "s4", "start": 3, "end": 4, "text": '    if (x < 3) { return "[Music]"; }\n<component />'}]
    result, receipt = cleaning.compact_segments(original, "manual")
    assert result[0]["segment_ids"] == ["s2", "s3"]
    assert result[1]["text"] == original[-1]["text"]
    assert original[0]["text"] == "[Music]" and receipt["clean_text_bytes"] < receipt["raw_text_bytes"]
    compact = cleaning.compact_ocr({"status": "extracted", "items": [{"at": 10, "text": "prompt{}", "lines": [], "confidence": None}, {"at": 20, "text": "prompt{}"}]})
    assert compact["items"] == [{"frame_times": [10, 20], "text": "prompt{}", "needs_review": True}]


def test_one_topic_call_screens_and_returns_only_selected_text(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    ids = ["youtube-" + letter * 11 for letter in "ABCD"]
    calls = []
    monkeypatch.setattr(media, "search", lambda *a: {"candidates": [metadata(i) for i in ids]})
    monkeypatch.setattr(media, "inspect", lambda w, r, url, cookies: metadata(media.identify(url)[1], title="Reuploaded tutorial" if "BBBB" in url else "Typography design tutorial"))
    def fetch(w, run, url, **kwargs):
        source_id = media.identify(url)[1]
        calls.append(source_id)
        segments = lesson() if source_id != ids[2] else [{"id": "s1", "start": 0, "end": 2, "text": "It looks cool"}]
        path = w.root / "private/raw" / source_id / "segments.json"
        atomic_json(path, segments)
        return w.save_source({**metadata(source_id), "run_id": run, "status": "transcribed", "transcript_origin": "manual",
            "segments_path": str(path.relative_to(w.root)), "segments_sha256": digest(path.read_bytes())})
    monkeypatch.setattr(media, "fetch", fetch)
    monkeypatch.setattr(ocr, "windows_ocr", lambda *a, **k: pytest.fail("No screen cue should invoke OCR"))
    result = collection.collect_topic(ws, "typography design", limit=2, search_limit=4, include_text=True)
    assert result["status"] == "ready" and result["run_status"] == "closed" and result["llm_calls"] == 0
    assert [s["id"] for s in result["selected"]] == [ids[0]] and ids[1] not in calls
    assert len(result["texts"]) == 1 and not result["wiki_written"]
    decisions = {x["id"]: x["decision"] for x in result["decisions"]}
    assert decisions[ids[1]] == "rejected" and decisions[ids[2]] == "review" and decisions[ids[3]] == "rejected"
    assert Path(result["selected"][0]["markdown_path"]).is_file()
    analysis_run = ws.create_run("organize selected text")
    jobs = AnalysisJobs(ws)
    job = jobs.prepare(analysis_run["id"], ids[0], "Extract methods", result["id"])
    task = json.loads(Path(job["packet_path"]).read_text(encoding="utf-8"))
    assert task["collected_text_path"] == result["selected"][0]["markdown_path"]
    with pytest.raises(ValueError, match="not selected"):
        jobs.prepare(analysis_run["id"], ids[2], "Try to bypass screening", result["id"])
    original_source = ws.source(ids[0])
    ws.save_source({**original_source, "content_hash": "changed after collection"})
    with pytest.raises(ValueError, match="Collection source changed"):
        jobs.prepare(analysis_run["id"], ids[0], "Old collection with new source", result["id"])
    ws.save_source(original_source)
    Path(task["collected_text_path"]).write_text("changed after handoff", encoding="utf-8")
    with pytest.raises(ValueError, match="Collected text changed"):
        jobs._current(task)
    monkeypatch.setattr(ocr, "screen_cues", lambda *a: [{"at": 5, "segment_id": "s1"}])
    def blocked_ocr(*args, **kwargs):
        ws.pause("youtube", "synthetic OCR video-download restriction")
        raise media.PlatformError("youtube: access restricted")
    monkeypatch.setattr(ocr, "extract_screen_text", blocked_ocr)
    partial = collection.collect_topic(ws, "typography design", limit=2, search_limit=4)
    assert partial["status"] == "blocked" and len(partial["selected"]) == 1
    assert "Screen text incomplete: blocked" in Path(partial["selected"][0]["markdown_path"]).read_text(encoding="utf-8")
    ws.db.close()


def test_pause_and_missing_cookie_permission_stop_before_search(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    monkeypatch.setattr(media, "search", lambda *a: pytest.fail("Search must not run"))
    ws.pause("youtube", "synthetic restriction")
    blocked = collection.collect_topic(ws, "typography")
    assert blocked["status"] == "blocked" and blocked["run_status"] == "closed"
    assert ws.db.execute("SELECT 1 FROM pauses WHERE platform='youtube'").fetchone()
    run = ws.create_run("cookie check")
    denied = collection.collect_topic(ws, "design", platform="bilibili", run_id=run["id"], cookies=True)
    assert denied["status"] == "blocked" and denied["selected"] == []
    ws.db.close()


def test_targeted_ocr_is_cached_but_replaced_video_cannot_reuse_old_still(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    run = ws.create_run("OCR")
    source = {**metadata("youtube-AAAAAAAAAAA"), "run_id": run["id"], "video_hash": "video-one"}
    ws.save_source(source)
    counts = {"frames": 0, "ocr": 0}
    monkeypatch.setattr(media, "download_video", lambda *a, **k: None)
    def frame(w, source_id, at):
        counts["frames"] += 1
        path = w.root / "private/raw" / source_id / "frame.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(w.source(source_id)["video_hash"].encode())
        value = {"path": str(path.relative_to(w.root)), "sha256": digest(path.read_bytes()), "video_hash": w.source(source_id)["video_hash"]}
        current = w.source(source_id)
        current.setdefault("frames", {})[f"{at:.3f}"] = value
        w.save_source(current)
        return value
    def recognize(image=None, language=""):
        if image is None:
            return {"available": True}
        counts["ocr"] += 1
        return {"engine": "test-double", "text": 'Complete prompt: keep {symbols} and "quotes".'}
    monkeypatch.setattr(media, "frame", frame)
    monkeypatch.setattr(ocr, "windows_ocr", recognize)
    cues = ocr.screen_cues([{"id": "s1", "start": 1, "end": 5, "text": "Here is the full prompt shown on screen"}], 60)
    for _ in range(2):
        ocr.extract_screen_text(ws, run["id"], source["id"], cues)
    assert counts == {"frames": 1, "ocr": 1}
    current = ws.source(source["id"])
    current["video_hash"] = "video-two"
    ws.save_source(current)
    ocr.extract_screen_text(ws, run["id"], source["id"], cues)
    assert counts == {"frames": 2, "ocr": 2}
    ws.db.close()


def test_topic_cli_batches_queries_caps_creators_and_covers_twelve_sources(tmp_path, monkeypatch, capsys):
    """Routing acceptance: screening is stubbed; its quality heuristics have separate checks."""
    ids = ["youtube-" + str(i).zfill(11) for i in range(20)]
    searches, inspections, fetches = [], [], []
    def search(w, run, platform, query, limit, cookies):
        start = 5 * len(searches)
        searches.append((query, limit))
        return {"candidates": [metadata(s) for s in ids[start:start + limit]]}
    def inspect(w, run, url, cookies):
        sid = media.identify(url)[1]
        inspections.append(sid)
        n = ids.index(sid)
        return metadata(sid, channel_id="shared-creator" if n < 5 else "creator-" + str(n))
    def fetch(w, run, url, **kwargs):
        sid = media.identify(url)[1]
        fetches.append(sid)
        segments = [{**s, "text": "Owned lesson " + sid + ": " + s["text"]} for s in lesson()]
        path = w.root / "private/raw" / sid / "segments.json"
        atomic_json(path, segments)
        return w.save_source({**metadata(sid), "run_id": run, "status": "transcribed", "transcript_origin": "manual",
            "segments_path": str(path.relative_to(w.root)), "segments_sha256": digest(path.read_bytes())})
    monkeypatch.setattr(media, "search", search)
    monkeypatch.setattr(media, "inspect", inspect)
    monkeypatch.setattr(media, "fetch", fetch)
    monkeypatch.setattr(screening, "screen", lambda *a: {"decision": "selected", "score": 90})
    assert main(["--root", str(tmp_path), "learn-topic", "typography design", "--ocr", "off"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(searches) == 3 and [n for _, n in searches] == [10, 10, 10]
    assert len(inspections) == len(set(inspections)) == 20 and len(fetches) == 12
    manifest = json.loads(next((tmp_path / "state/collections").glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["llm_calls"] == 0 and manifest["run_status"] == "closed"
    assert max(Counter(s["creator"] for s in manifest["selected"]).values()) == 2
    assert any(c["reasons"] == ["per_creator_limit"] for c in manifest["candidates"])
    assert all(s["id"] + ":" in payload["body"] for s in manifest["selected"])
    assert set(payload) <= {"body", "review"} and "manifest_path" not in payload
    pack = json.loads(next((tmp_path / "state/text-packs").glob("*.json")).read_text(encoding="utf-8"))
    assert pack["metrics"]["represented_sources"] == 12 and not pack["unrepresented_sources"]


def test_one_transport_failure_does_not_discard_other_videos(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    ids = ["youtube-" + str(i).zfill(11) for i in range(4)]
    monkeypatch.setattr(media, "search", lambda *a: {"candidates": [metadata(s) for s in ids]})
    def inspect(w, run, url, cookies):
        sid = media.identify(url)[1]
        if sid == ids[1]:
            raise media.PlatformError("synthetic metadata transport failure", category="transport")
        return metadata(sid)
    def fetch(w, run, url, **kwargs):
        sid = media.identify(url)[1]
        if sid == ids[2]:
            raise media.PlatformError("synthetic transcript transport failure", category="transport")
        path = w.root / "private/raw" / sid / "segments.json"
        atomic_json(path, lesson())
        return w.save_source({**metadata(sid), "run_id": run, "status": "transcribed", "transcript_origin": "manual",
            "segments_path": str(path.relative_to(w.root)), "segments_sha256": digest(path.read_bytes())})
    monkeypatch.setattr(media, "inspect", inspect)
    monkeypatch.setattr(media, "fetch", fetch)
    monkeypatch.setattr(screening, "screen", lambda *a: {"decision": "selected", "score": 90})
    result = collection.collect_topic(ws, "typography", limit=2, search_limit=4, ocr_mode="off")
    assert result["status"] == "ready" and [s["id"] for s in result["selected"]] == [ids[0], ids[3]]
    decisions = {c["id"]: c["reasons"] for c in result["decisions"]}
    assert decisions[ids[1]] == ["metadata_transport_failure"] and decisions[ids[2]] == ["transcript_transport_failure"]
    assert not ws.db.execute("SELECT 1 FROM pauses").fetchone()
    ws.db.close()
