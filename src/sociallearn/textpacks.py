"""Budgeted, source-linked text windows. No model calls or network acquisition."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from itertools import zip_longest
from pathlib import Path

from .cleaning import compact_segments
from .store import Workspace, atomic_json, digest
from .wiki import estimate, terms
from .screening import METHODS
from .citations import compact_ids


PACK_VERSION = 5


class Meter:
    def __init__(self, encoding=None):
        self.encoding = encoding
        self.encoder = None
        if encoding:
            try:
                import tiktoken
            except ImportError as exc:
                raise ValueError("Install the optional measure dependency to use a reference tokenizer") from exc
            self.encoder = tiktoken.get_encoding(encoding)

    def count(self, text):
        return len(self.encoder.encode(text, disallowed_special=())) if self.encoder else estimate(text)

    @property
    def label(self):
        return f"reference_tokenizer:{self.encoding}; plain text, not full request/billing" if self.encoding else "character_heuristic_not_tokenizer"


def render(passages, *, compact=True):
    parts = []
    for index, passage in enumerate(passages, 1):
        citations = "; ".join(c["source_id"] + ":" + ((compact_ids(c["segment_ids"]) if compact else ",".join(c["segment_ids"])) if c["segment_ids"] else "frames=" + ",".join(map(str, c["frame_times"]))) for c in passage["citations"])
        parts.append(f"[P{index} {citations}]\n{passage['text']}")
    return "\n\n".join(parts)


def passages_for(source_id, segments, *, sentence_windows=False):
    chunks, pending, size, fence = [], [], 0, None
    def flush():
        if pending:
            chunk = {"source_id": source_id, "segment_ids": [sid for s in pending for sid in s["segment_ids"]],
                     "text": "\n".join(s["text"] for s in pending)}
            if "start" in pending[0]:
                chunk.update(start=pending[0]["start"], end=pending[-1]["end"])
            chunks.append(chunk)
    for segment in segments:
        markers = re.findall(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)", segment["text"])
        # A large code/prompt segment stays whole; no text is sliced to fit.
        boundary = bool(pending) and (not sentence_windows or bool(re.search(r"[。！？.!?][\"'”’）)]?\s*$", pending[-1]["text"])))
        if pending and fence is None and (markers or len(segment["text"]) >= 200 or (size + len(segment["text"]) > (180 if sentence_windows else 900) and boundary) or re.match(r"(?:rule|step|chapter)\s+\d|第[一二三四五六七八九十\d]+[条步章]", segment["text"], re.I)):
            flush()
            pending, size = [], 0
        pending.append(segment)
        size += len(segment["text"])
        # Fenced blocks may span many archived segments. Keep all lines together.
        for marker, suffix in markers:
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence = None
        if fence is None and (markers or len(segment["text"]) >= 200):
            flush()
            pending, size = [], 0
    flush()
    return chunks


class TextPacks:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.directory = ws.root / "state/text-packs"

    def _load(self, source_ids, collection_id=None):
        selected = None
        if collection_id:
            if not re.fullmatch(r"[a-f0-9]{12}", collection_id):
                raise ValueError("Invalid collection ID")
            manifest = json.loads((self.ws.root / "state/collections" / collection_id / "manifest.json").read_text(encoding="utf-8"))
            selected = {s["id"]: s for s in manifest["selected"]}
            if not source_ids:
                source_ids = list(selected)
        if not source_ids:
            raise ValueError("Select archived sources or a collection with selected sources")
        records = []
        for source_id in dict.fromkeys(source_ids):
            if selected is not None and source_id not in selected:
                raise ValueError("Source was not selected by this collection")
            source = self.ws.source(source_id)
            raw = self.ws.read_segments(source)
            segments, _ = compact_segments(raw, source.get("transcript_origin", ""))
            screen_text = None
            if selected is not None:
                item = selected[source_id]
                content = Path(item["text_path"]).read_bytes()
                if digest(content) != item["sha256"]:
                    raise ValueError("Collected text changed")
                payload = json.loads(content)
                if payload.get("source_snapshot") != {k: source.get(k) for k in ("content_hash", "segments_sha256", "video_hash")}:
                    raise ValueError("Collection source changed; collect again before packing")
                segments = payload["segments"]
                screen_text = payload.get("screen_text")
            records.append({"id": source_id, "title": source.get("title"), "url": source.get("url"),
                "published_at": source.get("published_at"), "transcript_origin": source.get("transcript_origin"),
                "review_status": source.get("transcript_review_status"), "segments": segments,
                "screen_text": screen_text,
                "snapshot": digest(json.dumps({"segments": raw, "screen_text": screen_text,
                    "source": {k: source.get(k) for k in ("content_hash", "video_hash", "title", "url", "published_at", "transcript_origin", "transcript_review_status")}}, ensure_ascii=False, sort_keys=True).encode())})
        return records

    def prepare(self, source_ids, focus, *, budget=2500, encoding=None, collection_id=None, balanced=False, questions=None, required_citations=None):
        if not focus.strip() or len(focus) > 400 or not 1 <= budget <= 100000:
            raise ValueError("Use a compact focus and a budget in 1..100000")
        questions = list(dict.fromkeys(questions or []))
        if len(questions) > 8 or any(not q.strip() or len(q) > 400 for q in questions):
            raise ValueError("Use up to eight nonempty compact learning questions")
        balanced = balanced or bool(questions)
        sources = self._load(source_ids, collection_id)
        required_citations = required_citations or []
        if required_citations and not questions:
            raise ValueError("Required citations are for question-window refinement")
        known = {s["id"]: {sid for seg in s["segments"] for sid in seg["segment_ids"]} for s in sources}
        if not isinstance(required_citations, list) or any(not isinstance(c, dict) or
               not isinstance(c.get("segment_ids"), list) or not c["segment_ids"] or
               any(not isinstance(sid, str) for sid in c["segment_ids"]) or not isinstance(c.get("source_id"), str) or c["source_id"] not in known or
               not set(c["segment_ids"]) <= known[c["source_id"]] for c in required_citations):
            raise ValueError("Required citations must be exact segment IDs in selected sources")
        meter = Meter(encoding)
        request = {"version": PACK_VERSION, "sources": {s["id"]: s["snapshot"] for s in sources},
                   "source_order": [s["id"] for s in sources], "collection_id": collection_id,
                   "focus": focus.strip(), "budget": budget, "encoding": encoding}
        if balanced:
            request["selection"] = "balanced_sources_v2"
        if questions:
            request.update(questions=questions, selection="question_windows_v2")
        if required_citations:
            request["required_citations"] = required_citations
        pack_id = digest(json.dumps(request, ensure_ascii=False, sort_keys=True).encode())[:20]
        path = self.directory / (pack_id + ".json")
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            return self._card(cached, True)
        chunks, offsets = [], {}
        for source in sources:
            start = len(chunks)
            chunks.extend(passages_for(source["id"], source["segments"], sentence_windows=bool(questions)))
            for item in (source["screen_text"] or {}).get("items", []):
                chunks.append({"source_id": source["id"], "segment_ids": [], "frame_times": item["frame_times"],
                    "start": min(item["frame_times"]), "end": max(item["frame_times"]),
                    "text": "[Screen prompt/code OCR, needs_review]\n" + item["text"]})
            # Untimed web paragraphs retain archive order; no timestamps are invented.
            chunks[start:] = sorted(chunks[start:], key=lambda c: c.get("start", 0))
            offsets[source["id"]] = (start, len(chunks))
        query = set(terms(focus))
        token_sets = [set(terms(c["text"])) for c in chunks]
        frequencies = Counter(term for tokens in token_sets for term in tokens)
        scores = [sum(math.log(1 + len(chunks)/(1+frequencies[t])) for t in query & ts) for ts in token_sets]
        question_scores = []
        if questions:
            for question in questions:
                qt = set(terms(question))
                question_scores.append([sum(math.log(1 + len(chunks)/(1+frequencies[t])) for t in qt & ts) for ts in token_sets])
            scores = [sum(row[i] for row in question_scores) for i in range(len(chunks))]
        ranked = sorted((i for i, score in enumerate(scores) if score > 0), key=lambda i: (-scores[i], i))
        if balanced:
            groups = []
            for source in sources:
                lo, hi = offsets[source["id"]]
                groups.append(sorted(range(lo, hi), key=lambda i: (
                    -(scores[i] + .5 * sum(bool(re.search(pattern, chunks[i]["text"], re.I)) for pattern in METHODS.values())), i)))
            ranked = [i for row in zip_longest(*groups) for i in row if i is not None]
        selected, deferred = set(), []
        seeds = []
        if questions:
            # One complete neighboring window per source, then up to two
            # different sources per question. No semantic paraphrase or voting.
            windows = {}
            for i, chunk in enumerate(chunks):
                lo, hi = offsets[chunk["source_id"]]
                windows[i] = set(range(max(lo, i-1), min(hi, i+2)))
            for citation in required_citations:
                wanted = set(citation["segment_ids"])
                for i, chunk in enumerate(chunks):
                    if chunk["source_id"] == citation["source_id"] and wanted.intersection(chunk["segment_ids"]):
                        selected |= windows[i]
            required_cost = meter.count(render(self._dedupe([chunks[j] for j in sorted(selected)])))
            if required_cost > budget:
                raise ValueError(f"Required evidence and neighboring context need {required_cost} body units; increase budget")
            def merit(row, i):
                return sum(row[j] for j in windows[i]) / math.sqrt(sum(len(chunks[j]["text"]) for j in windows[i]) or 1)
            source_best = [max(range(lo, hi), key=lambda i: (merit(scores, i), -i))
                           for lo, hi in offsets.values() if lo < hi]
            by_question = []
            for row in question_scores:
                best = [max(range(lo, hi), key=lambda i: (merit(row, i), -i))
                        for lo, hi in offsets.values() if lo < hi]
                by_question.append(sorted((i for i in best if merit(row, i) > 0), key=lambda i: (-merit(row, i), i))[:2])
            ranked = list(dict.fromkeys(source_best + [i for layer in zip_longest(*by_question) for i in layer if i is not None]))
            for i in ranked:
                window = windows[i]
                candidate = self._dedupe([chunks[j] for j in sorted(selected | window)])
                if meter.count(render(candidate)) <= budget:
                    selected |= window
                else:
                    deferred.append({"source_id": chunks[i]["source_id"], "segment_ids": chunks[i]["segment_ids"], "reason": "whole_question_window_exceeds_budget"})
        elif balanced:
            seeds = [group[0] for group in groups if group]
            # Reserve one whole passage per source before any source's neighbors
            # can consume the shared budget. Code/fenced blocks stay intact.
            for i in seeds:
                candidate = self._dedupe([chunks[j] for j in sorted(selected | {i})])
                if meter.count(render(candidate, compact=False)) <= budget:
                    selected.add(i)
            for i in seeds:
                if i not in selected:
                    continue
                lo, hi = offsets[chunks[i]["source_id"]]
                window = set(range(max(lo, i-1), min(hi, i+2)))
                candidate = self._dedupe([chunks[j] for j in sorted(selected | window)])
                if meter.count(render(candidate, compact=False)) <= budget:
                    selected |= window
        for i in ([] if questions else ranked):
            lo, hi = offsets[chunks[i]["source_id"]]
            window = set(range(max(lo, i-1), min(hi, i+2)))
            candidate = self._dedupe([chunks[j] for j in sorted(selected | window)])
            if meter.count(render(candidate, compact=False)) <= budget:
                selected |= window
            else:
                deferred.append({"source_id": chunks[i]["source_id"], "segment_ids": chunks[i]["segment_ids"], "reason": "whole_context_window_exceeds_budget"})
        passages = self._dedupe([chunks[i] for i in sorted(selected)])
        body = render(passages)
        baseline = self._dedupe(chunks, merge=False)
        full = render(baseline)
        pack = {"id": pack_id, "request": request, "source_ids": [s["id"] for s in sources],
                "sources": [{k: s[k] for k in ("id", "title", "url", "published_at", "transcript_origin", "review_status")} for s in sources],
                "passages": passages, "deferred": deferred, "body": body,
                "metrics": {"measurement": meter.label, "budget": budget, "returned_units": meter.count(body),
                    "budget_scope": "passage body including inline citations; surrounding tool metadata is additional",
                    "full_clean_units": meter.count(full), "returned_utf8_bytes": len(body.encode()),
                    "full_clean_utf8_bytes": len(full.encode()), "full_passages": len(chunks),
                    "returned_passages": len(passages), "merged_exact_passages": len(selected)-len(passages)},
                "coverage": "keyword-ranked whole passages plus one neighboring passage each side; not proof of complete methods",
                "unmatched": not bool(ranked), "omitted_ocr": any("frame_times" in c and i not in selected for i, c in enumerate(chunks)) or
                    any((s["screen_text"] or {}).get("omitted_cues") for s in sources)}
        if balanced:
            represented = {c["source_id"] for p in passages for c in p["citations"]}
            pack["unrepresented_sources"] = [s["id"] for s in sources if s["id"] not in represented]
            pack["weak_topic_match"] = [s["id"] for s in sources if s["id"] in represented and not any(scores[i] > 0 for i in range(*offsets[s["id"]]))]
            pack["context_deferred_sources"] = [chunks[i]["source_id"] for i in seeds if i in selected and
                not set(range(max(offsets[chunks[i]["source_id"]][0], i-1), min(offsets[chunks[i]["source_id"]][1], i+2))).issubset(selected)]
            pack["coverage"] = "one whole passage per source before neighboring context; not full-video learning or verified agreement"
            pack["metrics"].update(selected_sources=len(sources), represented_sources=len(represented))
        if questions:
            pack["coverage"] = "lexical question windows with complete neighboring units; not full-video coverage or verified agreement"
            pack["question_gaps"] = [q for q, row in zip(questions, question_scores) if not any(row[i] > 0 for i in selected)]
            pack["question_selection_needs_review"] = True
        atomic_json(path, pack)
        return self._card(pack, False)

    def _dedupe(self, chunks, merge=True):
        result, seen = [], {}
        for chunk in chunks:
            # Exact text only, including indentation and punctuation. Similar variants remain separate.
            key = chunk["text"]
            citation = {k: chunk[k] for k in ("source_id", "segment_ids", "start", "end") if k in chunk}
            if "frame_times" in chunk:
                citation["frame_times"] = chunk["frame_times"]
            if merge and key in seen:
                result[seen[key]]["citations"].append(citation)
            else:
                seen[key] = len(result)
                result.append({"text": key, "citations": [citation]})
        return result

    def _card(self, pack, cached):
        # Old on-disk packs retain their diagnostics, but their wire body can
        # use lossless citation ranges without reselecting or refilling text.
        body = render(pack["passages"])
        metrics = {**pack["metrics"], "returned_units": Meter(pack["request"].get("encoding")).count(body),
                   "returned_utf8_bytes": len(body.encode())}
        if pack["request"]["version"] < 5:
            metrics["stored_body_units"] = pack["metrics"]["returned_units"]
            metrics["stored_metrics_format"] = "historical verbose citations; returned_units uses lossless display ranges"
        return {"id": pack["id"], "status": "no_match" if pack["unmatched"] else "budget_deferred" if not pack["passages"] else "prepared", "cached": cached,
                "source_ids": pack["source_ids"], "metrics": metrics, "coverage": pack["coverage"],
                "deferred_count": len(pack["deferred"]), "omitted_ocr": pack["omitted_ocr"],
                "packet_path": str(self.directory / (pack["id"] + ".json")), "body_loaded": False}

    def read(self, pack_id, *, details=False):
        if not re.fullmatch(r"[a-f0-9]{20}", pack_id):
            raise ValueError("Invalid text pack ID")
        pack = json.loads((self.directory / (pack_id + ".json")).read_text(encoding="utf-8"))
        current = self._load(pack["source_ids"], pack["request"].get("collection_id"))
        if {s["id"]: s["snapshot"] for s in current} != pack["request"]["sources"]:
            raise ValueError("Source changed; prepare a new text pack")
        cited = {c["source_id"] for p in pack["passages"] for c in p["citations"]}
        body = render(pack["passages"])
        if not details:
            review = []
            if pack["unmatched"]:
                review.append("no_match")
            elif not pack["passages"] or pack["deferred"]:
                review.append("budget_deferred")
            if pack["passages"] and (pack["metrics"]["returned_passages"] + pack["metrics"]["merged_exact_passages"] < pack["metrics"]["full_passages"]):
                review.append("excerpt")
            if pack["omitted_ocr"]:
                review.append("ocr_incomplete")
            if pack.get("unrepresented_sources"):
                review.append("unrepresented_sources:" + ",".join(pack["unrepresented_sources"]))
            if pack.get("weak_topic_match"):
                review.append("weak_topic_match:" + ",".join(pack["weak_topic_match"]))
            if pack.get("context_deferred_sources"):
                review.append("context_deferred:" + ",".join(pack["context_deferred_sources"]))
            if pack.get("question_gaps"):
                review.append("unmatched_questions:" + " | ".join(pack["question_gaps"]))
            if pack.get("question_selection_needs_review"):
                review.append("question_selection_needs_review")
            ocr_sources = {c["source_id"] for p in pack["passages"] for c in p["citations"] if "frame_times" in c}
            for source in pack["sources"]:
                if source["id"] not in cited:
                    continue
                status = source["review_status"]
                if source["id"] in ocr_sources:
                    status = "needs_review"
                elif not status:
                    status = "needs_review" if source["transcript_origin"] in ("local_whisper", "automatic", "platform_page_track") else "review_unknown"
                if status in ("needs_review", "review_unknown"):
                    review.append(source["id"] + ":" + status)
            return {"body": body, **({"review": review} if review else {})}
        return {"id": pack_id, "sources": [s for s in pack["sources"] if s["id"] in cited], "body": body,
                "metrics": self._card(pack, True)["metrics"], "coverage": pack["coverage"], "deferred_count": len(pack["deferred"]),
                "omitted_ocr": pack["omitted_ocr"], "measurement_scope": "Body count excludes these metadata fields and the surrounding model conversation"}
