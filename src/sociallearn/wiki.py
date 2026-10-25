from __future__ import annotations

import json
import math
import re
from pathlib import Path

import yaml

from .freshness import assess
from .store import Workspace, digest, now


def terms(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]*|[\u3400-\u9fff]+", text.lower())
    result = []
    for token in tokens:
        if re.search(r"[\u3400-\u9fff]", token):
            result.extend(token[i:i+2] for i in range(len(token)-1))
            if len(token) == 1:
                result.append(token)
        else:
            result.append(token)
    return list(dict.fromkeys(result))


def read_document(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"Missing frontmatter: {path.name}")
    _, header, body = raw.split("---\n", 2)
    return yaml.safe_load(header), body.strip()


def estimate(text: str) -> int:
    # Conservative character heuristic, never reported as tokenizer telemetry.
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return math.ceil(cjk + (len(text) - cjk) / 3)


class Wiki:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.ws.db.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (id TEXT PRIMARY KEY, path TEXT, hash TEXT, meta TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(id UNINDEXED, route, body);
        """)
        self.ws.db.commit()

    def validate_citations(self, citations: list[dict]) -> dict:
        hashes = {}
        if not citations:
            raise ValueError("Knowledge needs precise source citations")
        for citation in citations:
            source = self.ws.source(citation["source_id"])
            requested = set(citation.get("segment_ids", []))
            frames = citation.get("frame_times", [])
            images = citation.get("image_ids", [])
            segments = self.ws.read_segments(source) if requested and source.get("segments_path") else []
            known = {s["id"] for s in segments}
            if requested and not requested <= known:
                raise ValueError("Citation segment IDs missing or outside source")
            if not requested and not frames and not images:
                raise ValueError("Citation segment IDs, frame times or image IDs are required")
            snapshot = hashes.setdefault(source["id"], {})
            if requested:
                snapshot["text"] = source.get("content_hash")
            for at in frames:
                record = source.get("frames", {}).get(f"{float(at):.3f}")
                if not record or digest((self.ws.root / record["path"]).read_bytes()) != record["sha256"]:
                    raise ValueError("Cited frame missing or changed")
                snapshot["video"] = source.get("video_hash")
                snapshot.setdefault("frames", {})[f"{float(at):.3f}"] = record["sha256"]
            for image_id in images:
                record = source.get("images", {}).get(image_id)
                if not record or digest((self.ws.root / record["path"]).read_bytes()) != record["sha256"]:
                    raise ValueError("Cited image missing or changed")
                snapshot.setdefault("images", {})[image_id] = record["sha256"]
        return hashes

    def validate(self, payload: dict) -> tuple[dict, str, dict]:
        """Validate a complete candidate without writing knowledge or revisions."""
        meta = dict(payload)
        body = meta.pop("body", None)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Knowledge body must be nonempty text")
        body = body.strip()
        block_id = meta.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", block_id):
            raise ValueError("Block ID must be a lowercase slug")
        for key in ("title", "summary", "kind", "applies_to", "verification_status"):
            if not meta.get(key):
                raise ValueError(f"Missing {key}")
        for key, maximum in (("title", 160), ("summary", 220), ("applies_to", 400)):
            if not isinstance(meta[key], str) or len(meta[key]) > maximum:
                raise ValueError(f"{key} must be compact text (at most {maximum} characters)")
        if meta["kind"] not in ("principle", "case", "procedure", "implementation", "trend", "model"):
            raise ValueError("Unknown knowledge kind")
        if meta["verification_status"] not in ("source_described", "agent_synthesis"):
            raise ValueError("Initial tool accepts source descriptions/synthesis, not unproven 'tested' claims")
        meta["source_hashes"] = self.validate_citations(meta.get("citations", []))
        status = assess(meta)
        if status["status"] == "invalid":
            raise ValueError(status["reason"])
        if meta["kind"] == "model" and meta.get("verified_at"):
            official = [self.ws.source(c["source_id"]) for c in meta["citations"]
                        if self.ws.source(c["source_id"])["url"] == meta.get("official_url")]
            if not official or status["status"] == "unverified":
                raise ValueError("Model verification requires archived official URL evidence and exact model ID")
            if any(str(s["observed_at"])[:10] < str(meta["verified_at"])[:10] for s in official):
                raise ValueError("Verification date is newer than archived official evidence")
        return meta, body, status

    def put(self, payload: dict, expected_hash: str | None = None) -> dict:
        meta, body, status = self.validate(payload)
        block_id = meta["id"]
        path = self.ws.root / "knowledge/blocks" / (block_id + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = path.read_bytes()
            old_hash = digest(old)
            if expected_hash != old_hash:
                raise ValueError("Existing block changed; pass its current hash before replacing")
            revision = self.ws.root / "state/revisions" / block_id / (old_hash + ".md")
            revision.parent.mkdir(parents=True, exist_ok=True)
            revision.write_bytes(old)
        meta["updated_at"] = now()  # Never used as a fact-verification date.
        document = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + body + "\n"
        temp = path.with_suffix(".tmp")
        temp.write_text(document, encoding="utf-8", newline="\n")
        temp.replace(path)
        self.reindex()
        return {"id": block_id, "hash": digest(path.read_bytes()), "freshness": status}

    def reindex(self) -> dict:
        # Markdown is canonical; the index is expendable and never modifies notes.
        docs = []
        for path in (self.ws.root / "knowledge/blocks").glob("*.md"):
            meta, body = read_document(path)
            route = " ".join([meta["title"], meta["summary"], *meta.get("aliases", []),
                              *meta.get("tags", []), str(meta.get("applies_to", ""))])
            docs.append((meta, path, body, route))
        with self.ws.db:
            self.ws.db.execute("DELETE FROM blocks")
            self.ws.db.execute("DELETE FROM block_fts")
            for meta, path, body, route in docs:
                self.ws.db.execute("INSERT INTO blocks VALUES (?,?,?,?)", (
                    meta["id"], str(path.relative_to(self.ws.root)), digest(path.read_bytes()),
                    json.dumps(meta, ensure_ascii=False, default=str)))
                self.ws.db.execute("INSERT INTO block_fts VALUES (?,?,?)",
                                   (meta["id"], " ".join(terms(route)), " ".join(terms(body))))
        return {"indexed": len(docs)}

    def _status(self, meta, target_version=None, current=False):
        result = assess(meta, target_version=target_version, current=current)
        for source_id, original_hash in meta.get("source_hashes", {}).items():
            try:
                source = self.ws.source(source_id)
            except ValueError:
                return {"status": "source_missing", "reason": "referenced source is unavailable locally"}
            if isinstance(original_hash, dict):
                changed = ("text" in original_hash and source.get("content_hash") != original_hash["text"] or
                           "video" in original_hash and source.get("video_hash") != original_hash["video"] or
                           any(source.get("frames", {}).get(at, {}).get("sha256") != sha
                               for at, sha in original_hash.get("frames", {}).items()) or
                           any(source.get("images", {}).get(i, {}).get("sha256") != sha
                               for i, sha in original_hash.get("images", {}).items()))
            else:
                # Pilot notes written before separate text/video fingerprints remain readable.
                text_cited = any(c["source_id"] == source_id and c.get("segment_ids") for c in meta["citations"])
                changed = source.get("content_hash" if text_cited else "video_hash") != original_hash
            if changed:
                return {"status": "source_changed", "reason": "source changed since this block was written"}
        try:
            self.validate_citations(meta.get("citations", []))
        except (ValueError, OSError):
            return {"status": "source_changed", "reason": "cited source segments or frames missing or changed"}
        return result

    def search(self, query: str, limit: int = 5, *, target_version=None, current=False) -> dict:
        if not 1 <= limit <= 10:
            raise ValueError("Card limit must be 1..10")
        tokens = terms(query)
        if not tokens:
            return {"cards": [], "coverage": "no_query_terms"}
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        statement = """
            SELECT b.* FROM block_fts f JOIN blocks b ON b.id=f.id
            WHERE block_fts MATCH ? ORDER BY bm25(block_fts,0,5,1) LIMIT ?
        """
        rows = self.ws.db.execute(statement, ("route : (" + match + ")", limit)).fetchall()
        match_scope = "route"
        if not rows:
            rows = self.ws.db.execute(statement, (match, limit)).fetchall()
            match_scope = "body_fallback_needs_relevance_check"
        cards = []
        for row in rows:
            meta = json.loads(row["meta"])
            card = {"id": meta["id"], "title": meta["title"], "summary": meta["summary"],
                          "applies_to": meta["applies_to"], "not_for": meta.get("not_for", []),
                          "kind": meta["kind"], "version": meta.get("version"),
                          "verified_at": str(meta["verified_at"]) if meta.get("verified_at") else None,
                          "freshness": self._status(meta, target_version, current), "hash": row["hash"]}
            if meta["kind"] == "model":
                card.update({key: meta.get(key) for key in ("provider", "model_id", "official_url")})
            cards.append(card)
        return {"cards": cards, "coverage": "matches_only_not_proof_of_completeness", "match_scope": match_scope,
                "body_loaded": False}

    def read(self, block_ids: list[str], *, budget: int = 3000, session: str | None = None,
             current=False, target_version=None, historical=False) -> dict:
        if budget <= 0:
            raise ValueError("Budget must be positive")
        blocks, deferred, used = [], [], 0
        for block_id in dict.fromkeys(block_ids):
            row = self.ws.db.execute("SELECT * FROM blocks WHERE id=?", (block_id,)).fetchone()
            if not row:
                raise ValueError(f"Block not indexed: {block_id}")
            path = self.ws.root / row["path"]
            raw = path.read_bytes()
            if digest(raw) != row["hash"]:
                raise ValueError("Markdown changed; reindex before reading")
            meta, body = read_document(path)
            status = self._status(meta, target_version, current)
            if status["status"] != "usable" and not historical:
                deferred.append({"id": block_id, "reason": status})
                continue
            prior = self.ws.db.execute("SELECT hash FROM loaded WHERE session=? AND block_id=?",
                                       (session, block_id)).fetchone() if session else None
            if prior and prior[0] == row["hash"]:
                blocks.append({"id": block_id, "status": "already_returned_in_this_context", "hash": row["hash"]})
                continue
            packet = {"id": block_id, "title": meta["title"], "applies_to": meta["applies_to"],
                      "not_for": meta.get("not_for", []), "body": body, "citations": meta["citations"],
                      "verification_status": meta["verification_status"], "freshness": status, "hash": row["hash"]}
            if meta["kind"] in ("model", "implementation", "trend"):
                packet.update({key: str(meta[key]) if meta.get(key) is not None else None
                               for key in ("version", "verified_at", "review_after")})
            if meta["kind"] == "model":
                packet.update({key: meta.get(key) for key in ("provider", "model_id", "official_url")})
            packet["sources"] = [{"id": sid, "url": self.ws.source(sid)["url"]}
                                 for sid in meta.get("source_hashes", {})]
            packet["source_warnings"] = [{"id": sid, "status": self.ws.source(sid)["transcript_review_status"]}
                                         for sid in meta.get("source_hashes", {})
                                         if self.ws.source(sid).get("transcript_review_status") == "needs_review"]
            cost = estimate(json.dumps(packet, ensure_ascii=False))
            if used + cost > budget:
                deferred.append({"id": block_id, "reason": "budget", "estimated_tokens": cost})
                continue
            blocks.append(packet)
            used += cost
            if session:
                with self.ws.db:
                    self.ws.db.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?)", (session, block_id, row["hash"]))
        return {"blocks": blocks, "deferred": deferred, "estimated_tokens": used,
                "measurement": "character_heuristic_not_tokenizer", "budget": budget}

