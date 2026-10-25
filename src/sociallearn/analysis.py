"""File handoffs for native agent workers; no model caller or browser access."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .store import Workspace, atomic_json, digest, now
from .wiki import Wiki, estimate

ANALYSIS_POLICY_VERSION = 2


class AnalysisJobs:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.directory = ws.root / "state/analysis-jobs"

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("Invalid analysis job ID")
        return self.directory / job_id

    def _job(self, job_id: str) -> dict:
        path = self._path(job_id) / "task.json"
        if not path.is_file():
            raise ValueError("Unknown analysis job")
        return json.loads(path.read_text(encoding="utf-8"))

    def _snapshot(self, source_id: str) -> dict:
        source = self.ws.source(source_id)
        if source.get("segments_path"):
            self.ws.read_segments(source)
        snapshot = {key: source.get(key) for key in ("content_hash", "video_hash", "segments_sha256")}
        if source.get("images"):
            Wiki(self.ws).validate_citations([{"source_id": source_id, "image_ids": list(source["images"])}])
            snapshot["images"] = {k: v["sha256"] for k, v in source["images"].items()}
        return snapshot

    def _current(self, job: dict) -> None:
        if self.ws.run(job["run_id"])["status"] != "active":
            raise ValueError("Analysis run is closed")
        if self._snapshot(job["source_id"]) != job["source_snapshot"]:
            raise ValueError("Analysis source changed; prepare a new job and review the evidence")
        if job.get("collected_text_path") and digest(Path(job["collected_text_path"]).read_bytes()) != job["collected_text_sha256"]:
            raise ValueError("Collected text changed; prepare a new analysis job")

    def _card(self, job: dict) -> dict:
        return {key: job[key] for key in ("id", "run_id", "source_id", "focus", "status", "summary", "candidates")} | {
            "packet_path": str(self._path(job["id"]) / "task.json"), "body_loaded": False}

    def prepare(self, run_id: str, source_id: str, focus: str, collection_id: str | None = None, force=False) -> dict:
        if self.ws.run(run_id)["status"] != "active":
            raise ValueError("Analysis run is closed")
        if not focus.strip() or len(focus) > 400:
            raise ValueError("Focus must be compact text, at most 400 characters")
        source = self.ws.source(source_id)
        snapshot = self._snapshot(source_id)
        if not source.get("segments_path") and not source.get("frames") and not source.get("images"):
            raise ValueError("Archive text or frames first; search metadata is not analysis evidence")
        default_mode = "transcript_excerpt" if source.get("segments_path") else "images" if source.get("images") else "sampled_stills"
        collected = None
        if collection_id:
            if not re.fullmatch(r"[a-f0-9]{12}", collection_id):
                raise ValueError("Invalid collection ID")
            manifest = json.loads((self.ws.root / "state/collections" / collection_id / "manifest.json").read_text(encoding="utf-8"))
            collected = next((item for item in manifest["selected"] if item["id"] == source_id), None)
            if not collected:
                raise ValueError("Source was not selected by this collection")
            if digest(Path(collected["markdown_path"]).read_bytes()) != collected["markdown_sha256"]:
                raise ValueError("Collected text changed")
            content = Path(collected["text_path"]).read_bytes()
            if digest(content) != collected["sha256"]:
                raise ValueError("Collected text changed")
            if json.loads(content).get("source_snapshot") != {k: source.get(k) for k in ("content_hash", "segments_sha256", "video_hash")}:
                raise ValueError("Collection source changed; collect again before analysis")
        if not force:
            key = " ".join(focus.split()).casefold()
            for path in sorted(self.directory.glob("*/task.json"), reverse=True):
                previous = json.loads(path.read_text(encoding="utf-8"))
                ids = previous.get("admitted", [])
                if (previous.get("analysis_policy_version") != ANALYSIS_POLICY_VERSION or previous["source_id"] != source_id
                    or " ".join(previous["focus"].split()).casefold() != key or previous["source_snapshot"] != snapshot
                    or previous.get("status") != "submitted" or not ids
                    or set(ids) != {c["id"] for c in previous["candidates"]}
                    or previous.get("collected_text_sha256") != (collected["markdown_sha256"] if collected else None)):
                    continue
                raw = (path.parent / "result.json").read_bytes()
                if digest(raw) != previous["result_hash"]:
                    continue
                current = Wiki(self.ws).read(ids, budget=100000,
                    current=bool(re.search(r"\b(latest|current)\b|最新|现在|目前", focus, re.I)))
                if current["deferred"] or any(b["hash"] != previous.get("admitted_hashes", {}).get(b["id"]) for b in current["blocks"]):
                    continue
                return {"status": "cached", "needs_analysis": False, "run_id": run_id, "source_id": source_id,
                    "cached_job_id": previous["id"], "methods": [{k: b[k] for k in ("id", "title", "hash")} for b in current["blocks"]],
                    "body_loaded": False}
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "run_id": run_id, "source_id": source_id, "focus": focus,
               "analysis_policy_version": ANALYSIS_POLICY_VERSION,
               "source_snapshot": snapshot, "created_at": now(), "status": "prepared",
               "summary": "", "candidates": [], "admitted": [],
               "source": {key: source.get(key) for key in ("title", "url", "published_at", "observed_at",
                          "transcript_origin", "transcript_review_status", "visual_status", "evidence_origin", "parent_source_id", "evidence_channel")},
               "worker_guidance": [
                   "Read an assigned focused text pack first; otherwise read collected_text_path when provided. Expand exact source segments only for a necessary citation or missing condition, rather than loading the entire transcript.",
                   "Acquisition, screening, Whisper and system OCR are deterministic tools. Do not run per-video collection, re-transcribe cached text or inspect images as a routine analysis step.",
                   "A timestamp-linked speaker explanation may support a source_described method without image review; do not claim visual inspection or execution.",
                   "If necessary prompt/code text is still missing, report its timestamp and gap; do not scan frames. The collector's bounded system OCR output remains marked needs_review.",
                   "Do not scan fixed intervals, extract continuous frame sequences or make/render a demo as a routine learning step. More images need a concrete missing-content reason and cost estimate.",
                   "For an explicitly assigned image-only or visual-review task, inspect only its bounded evidence and report actual coverage. Use native-agent image reading, not a local vision model.",
               ],
               "result_template": {"source_id": source_id, "summary": "", "coverage": {
                   "mode": default_mode,
                   "segment_ids": [], "frame_times": [], "image_ids": []}, "candidates": []},
               "result_contract": {"summary_max_characters": 600, "candidate_limit": 3,
                   "coverage_modes": ["transcript_excerpt", "transcript_full", "sampled_stills", "frame_sequence", "images"],
                   "candidate_format": "Complete sociallearn put payload, citations within reported coverage",
                   "candidate_types": "title, summary, applies_to and body are text; aliases, tags and not_for are string lists",
                   "no_novelty": "Return candidates: []; do not manufacture a duplicate note"},
               "workspace": str(self.ws.root),
               "source_record_path": str(self.ws.root / "private/raw" / source_id / "source.json")}
        if collected:
            job.update(collection_id=collection_id, collected_text_path=collected["markdown_path"], collected_text_sha256=collected["markdown_sha256"])
        atomic_json(self._path(job_id) / "task.json", job)
        return self._card(job)

    def submit(self, job_id: str, result: dict) -> dict:
        job = self._job(job_id)
        self._current(job)
        if job["status"] != "prepared":
            raise ValueError("Job already submitted; create a new job for a new analysis")
        if result.get("source_id") != job["source_id"]:
            raise ValueError("Result must refer to the assigned source")
        summary = result.get("summary", "")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
            raise ValueError("Return a nonempty summary of at most 600 characters")
        candidates = result.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 3:
            raise ValueError("At most three focused knowledge candidates per video")
        coverage = result.get("coverage", {})
        if coverage.get("mode") not in ("transcript_excerpt", "transcript_full", "sampled_stills", "frame_sequence", "images"):
            raise ValueError("Record the actual bounded review mode")
        citation = {"source_id": job["source_id"], "segment_ids": coverage.get("segment_ids", []),
                    "frame_times": coverage.get("frame_times", []), "image_ids": coverage.get("image_ids", [])}
        wiki = Wiki(self.ws)
        wiki.validate_citations([citation])
        if coverage["mode"] == "transcript_full":
            known = {s["id"] for s in self.ws.read_segments(self.ws.source(job["source_id"]))}
            if set(citation["segment_ids"]) != known:
                raise ValueError("Full transcript coverage needs every archived segment ID")
        if coverage["mode"] in ("sampled_stills", "frame_sequence") and not citation["frame_times"]:
            raise ValueError("Visual review requires extracted frame times")
        if coverage["mode"].startswith("transcript_") and not citation["segment_ids"]:
            raise ValueError("Transcript review requires segment IDs")
        if coverage["mode"] == "images" and not citation["image_ids"]:
            raise ValueError("Image review requires archived image IDs")
        cards, ids = [], set()
        for candidate in candidates:
            block_id = candidate.get("id", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", block_id) or block_id in ids:
                raise ValueError("Candidate IDs must be distinct lowercase slugs")
            ids.add(block_id)
            citations = candidate.get("citations", [])
            if any(c.get("source_id") != job["source_id"] for c in citations):
                raise ValueError("A video worker may cite only its assigned source")
            wiki.validate(candidate)
            for c in citations:
                if not set(c.get("segment_ids", [])) <= set(citation["segment_ids"]) or not set(
                        c.get("frame_times", [])) <= set(citation["frame_times"]) or not set(
                        c.get("image_ids", [])) <= set(citation["image_ids"]):
                    raise ValueError("Candidate citations must stay inside reported review coverage")
            cards.append({key: candidate[key] for key in ("id", "title", "summary")})
        # Workers write separate result files. Only the integrator admits wiki changes.
        result_path = self._path(job_id) / "result.json"
        atomic_json(result_path, result)
        job.update(status="submitted", summary=summary, candidates=cards, result_hash=digest(result_path.read_bytes()))
        atomic_json(self._path(job_id) / "task.json", job)
        return self._card(job)

    def list(self, run_id: str, limit: int = 10) -> dict:
        self.ws.run(run_id)
        if not 1 <= limit <= 20:
            raise ValueError("Job card limit must be 1..20")
        jobs = [json.loads(path.read_text(encoding="utf-8")) for path in self.directory.glob("*/task.json")]
        matches = sorted((j for j in jobs if j["run_id"] == run_id), key=lambda j: j["created_at"])
        return {"jobs": [self._card(j) for j in matches[:limit]], "total": len(matches), "body_loaded": False}

    def _candidate(self, job: dict, candidate_id: str) -> dict:
        self._current(job)
        if job["status"] != "submitted":
            raise ValueError("Analysis result is not submitted")
        raw = (self._path(job["id"]) / "result.json").read_bytes()
        if digest(raw) != job["result_hash"]:
            raise ValueError("Submitted analysis changed; create a new job and review the new result")
        result = json.loads(raw)
        for candidate in result["candidates"]:
            if candidate["id"] == candidate_id:
                return candidate
        raise ValueError("Candidate is not in this job")

    def read(self, job_id: str, candidate_id: str, budget: int = 3000) -> dict:
        job = self._job(job_id)
        candidate = self._candidate(job, candidate_id)
        cost = estimate(json.dumps(candidate, ensure_ascii=False))
        if budget <= 0 or cost > budget:
            raise ValueError(f"Whole candidate needs an estimated {cost} tokens; increase budget or defer it")
        return {"candidate": candidate, "estimated_tokens": cost, "measurement": "character_heuristic_not_tokenizer"}

    def admit(self, job_id: str, candidate_id: str, expected_hash: str | None = None) -> dict:
        job = self._job(job_id)
        candidate = self._candidate(job, candidate_id)
        if candidate_id in job["admitted"]:
            raise ValueError("Candidate already admitted")
        saved = Wiki(self.ws).put(candidate, expected_hash)
        job["admitted"].append(candidate_id)
        job.setdefault("admitted_hashes", {})[candidate_id] = saved["hash"]
        atomic_json(self._path(job_id) / "task.json", job)
        return saved
