import json
from pathlib import Path

import pytest

from sociallearn.analysis import AnalysisJobs
from sociallearn.store import Workspace, atomic_json, digest
from sociallearn.wiki import Wiki


@pytest.fixture
def setup(tmp_path):
    ws = Workspace(tmp_path)
    run = ws.create_run("video design")
    path = tmp_path / "private/raw/demo/segments.json"
    segments = [{"id": f"s{i}", "start": i, "end": i + 1, "text": "Synthetic design evidence"} for i in (1, 2)]
    atomic_json(path, segments)
    ws.save_source({"id": "demo", "run_id": run["id"], "url": "https://example.org/demo",
                    "content_hash": digest(path.read_bytes()), "segments_sha256": digest(path.read_bytes()),
                    "segments_path": str(path.relative_to(tmp_path)), "status": "transcribed"})
    jobs = AnalysisJobs(ws)
    job = jobs.prepare(run["id"], "demo", "Readability")
    yield ws, run, jobs, job
    ws.db.close()


def result():
    return {"source_id": "demo", "summary": "One actionable principle, viewed only the first excerpt.",
            "coverage": {"mode": "transcript_excerpt", "segment_ids": ["s1"]},
            "candidates": [{"id": "type-contrast", "title": "字体对比", "summary": "保持阅读层级",
                            "kind": "principle", "applies_to": "字幕", "verification_status": "agent_synthesis",
                            "body": "A complete method with applicability and limitations.",
                            "citations": [{"source_id": "demo", "segment_ids": ["s1"]}]}]}


def test_handoff_cards_do_not_load_bodies_or_admit_automatically(setup):
    ws, run, jobs, job = setup
    saved = jobs.submit(job["id"], result())
    assert saved["body_loaded"] is False and "body" not in saved["candidates"][0]
    cards = jobs.list(run["id"])
    assert cards["body_loaded"] is False and "body" not in cards["jobs"][0]["candidates"][0]
    assert not Wiki(ws).search("字幕")["cards"]
    with pytest.raises(ValueError, match="increase budget"):
        jobs.read(job["id"], "type-contrast", budget=1)
    assert jobs.read(job["id"], "type-contrast")["candidate"]["body"]
    jobs.admit(job["id"], "type-contrast")
    assert Wiki(ws).search("字幕")["cards"][0]["id"] == "type-contrast"


@pytest.mark.parametrize("with_text,expected_mode", [(True, "transcript_excerpt"), (False, "images")])
def test_available_images_do_not_displace_transcript_learning(setup, with_text, expected_mode):
    ws, run, jobs, _ = setup
    source = ws.source("demo")
    image = ws.root / "private/raw/demo/screen.png"
    image.write_bytes(b"synthetic screen evidence; no visual inspection claimed")
    source["images"] = {"image-001": {"path": str(image.relative_to(ws.root)), "sha256": digest(image.read_bytes())}}
    if not with_text:
        source.pop("segments_path")
    ws.save_source(source)
    packet = jobs.prepare(run["id"], "demo", "Learn the explained method")
    task = json.loads(Path(packet["packet_path"]).read_text(encoding="utf-8"))
    assert task["result_template"]["coverage"]["mode"] == expected_mode


def test_stale_worker_cannot_admit_after_source_refresh(setup):
    ws, _, jobs, job = setup
    jobs.submit(job["id"], result())
    source = ws.source("demo")
    source["content_hash"] = "new revision"
    ws.save_source(source)
    with pytest.raises(ValueError, match="source changed"):
        jobs.admit(job["id"], "type-contrast")
    assert not (ws.root / "knowledge/blocks/type-contrast.md").exists()


def test_full_video_claim_and_unread_citation_are_rejected(setup):
    _, _, jobs, job = setup
    payload = result()
    payload["coverage"]["mode"] = "transcript_full"
    with pytest.raises(ValueError, match="every archived segment"):
        jobs.submit(job["id"], payload)
    payload = result()
    payload["candidates"][0]["citations"][0]["segment_ids"] = ["s2"]
    with pytest.raises(ValueError, match="inside reported review coverage"):
        jobs.submit(job["id"], payload)


def test_workers_have_separate_results_and_changed_result_needs_review(setup):
    ws, run, jobs, first = setup
    second = jobs.prepare(run["id"], "demo", "Different question")
    jobs.submit(first["id"], result())
    jobs.submit(second["id"], result())
    assert len(jobs.list(run["id"])["jobs"]) == 2
    path = ws.root / "state/analysis-jobs" / first["id"] / "result.json"
    changed = result()
    changed["candidates"][0]["body"] = "Changed after submission"
    atomic_json(path, changed)
    with pytest.raises(ValueError, match="analysis changed"):
        jobs.admit(first["id"], "type-contrast")
    assert jobs.read(second["id"], "type-contrast")["candidate"]["body"] != "Changed after submission"


def test_search_cards_cannot_be_promoted_to_analysis_evidence(setup):
    ws, run, jobs, _ = setup
    ws.save_source({"id": "card", "run_id": run["id"], "status": "metadata_only"})
    with pytest.raises(ValueError, match="search metadata"):
        jobs.prepare(run["id"], "card", "Try to learn from a title")


def test_malformed_worker_block_is_rejected_before_submission(setup):
    ws, _, jobs, job = setup
    payload = result()
    payload["candidates"][0]["applies_to"] = ["Invalid list-shaped field"]
    with pytest.raises(ValueError, match="applies_to must be compact text"):
        jobs.submit(job["id"], payload)
    assert jobs.list(job["run_id"])["jobs"][0]["status"] == "prepared"
    assert not (ws.root / "knowledge/blocks/type-contrast.md").exists()


def test_unchanged_admitted_learning_is_reused_across_runs_but_not_changed_sources(setup):
    ws, _, jobs, job = setup
    jobs.submit(job["id"], result())
    jobs.admit(job["id"], "type-contrast")
    new = ws.create_run("reuse already learned method")
    cached = jobs.prepare(new["id"], "demo", " Readability ")
    assert cached["status"] == "cached" and cached["needs_analysis"] is False
    assert not cached["body_loaded"] and cached["methods"][0]["id"] == "type-contrast"
    assert jobs.prepare(new["id"], "demo", "Readability", force=True)["status"] == "prepared"
    assert jobs.prepare(new["id"], "demo", "A different task")["status"] == "prepared"
    source = ws.source("demo")
    source["content_hash"] = "new source content"
    ws.save_source(source)
    assert jobs.prepare(new["id"], "demo", "Readability")["status"] == "prepared"
