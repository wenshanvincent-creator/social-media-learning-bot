from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for part in ("private/raw", "private/profiles", "knowledge", "state"):
            (self.root / part).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state/jobs.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, topic TEXT, created_at TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS grants (
                run_id TEXT, platform TEXT, allowed INTEGER, evidence TEXT,
                granted_at TEXT, PRIMARY KEY(run_id, platform));
            CREATE TABLE IF NOT EXISTS pauses (
                platform TEXT PRIMARY KEY, reason TEXT, observed_at TEXT);
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, run_id TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS loaded (
                session TEXT, block_id TEXT, hash TEXT,
                PRIMARY KEY(session, block_id));
        """)
        self.db.commit()

    def create_run(self, topic: str) -> dict:
        run = {"id": uuid.uuid4().hex[:12], "topic": topic, "created_at": now(), "status": "active"}
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (:id,:topic,:created_at,:status)", run)
        return run

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("Unknown run ID")
        return dict(row)

    def consent(self, run_id: str, platform: str, allowed: bool, evidence: str) -> dict:
        run = self.run(run_id)
        if run["status"] != "active":
            raise ValueError("Run is closed")
        if not evidence.strip():
            raise ValueError("Record the user's consent/denial, never cookie values")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?)",
                            (run_id, platform, int(allowed), evidence, now()))
        return {"run_id": run_id, "platform": platform, "allowed": allowed}

    def require_consent(self, run_id: str, platform: str) -> None:
        if self.run(run_id)["status"] != "active":
            raise PermissionError("Run closed; create a new run and obtain consent")
        row = self.db.execute("SELECT allowed FROM grants WHERE run_id=? AND platform=?",
                              (run_id, platform)).fetchone()
        if not row or not row[0]:
            raise PermissionError(f"Explicit cookie consent required for {platform} in this run")

    def check_access(self, run_id: str, platform: str) -> None:
        if self.run(run_id)["status"] != "active":
            raise ValueError("Run is closed")
        row = self.db.execute("SELECT reason FROM pauses WHERE platform=?", (platform,)).fetchone()
        if row:
            raise PermissionError(f"{platform} paused: {row[0]}; human resolution required")

    def pause(self, platform: str, reason: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?)", (platform, reason, now()))

    def resolve_pause(self, run_id: str, platform: str, evidence: str) -> dict:
        if self.run(run_id)["status"] != "active":
            raise ValueError("Run is closed")
        if not evidence.strip():
            raise ValueError("Record the human's resolution report, never credentials")
        row = self.db.execute("SELECT * FROM pauses WHERE platform=?", (platform,)).fetchone()
        if not row:
            raise ValueError("Platform is not paused")
        receipt = {"run_id": run_id, "platform": platform, "prior_pause": dict(row),
                   "human_evidence": evidence, "resolved_at": now()}
        path = self.root / "state/pause-resolutions" / (uuid.uuid4().hex + ".json")
        atomic_json(path, receipt)
        with self.db:
            self.db.execute("DELETE FROM pauses WHERE platform=?", (platform,))
        return {"platform": platform, "status": "human_reported_resolved", "receipt": str(path),
                "collection_restarted": False, "cookie_consent_granted": False}

    def save_source(self, source: dict) -> dict:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                            (source["id"], source["run_id"], json.dumps(source, ensure_ascii=False)))
        atomic_json(self.root / "private/raw" / source["id"] / "source.json", source)
        return source

    def snapshot_source(self, source_id: str) -> None:
        source = self.source(source_id)
        fingerprint = source.get("content_hash") or source.get("video_hash")
        if not fingerprint:
            return
        destination = self.root / "state/source-revisions" / source_id / fingerprint
        destination.mkdir(parents=True, exist_ok=True)
        if (destination / "source.json").exists():
            return
        if source.get("segments_path"):
            shutil.copyfile(self.root / source["segments_path"], destination / "segments.json")
        atomic_json(destination / "source.json", source)

    def source(self, source_id: str) -> dict:
        row = self.db.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown source: {source_id}")
        return json.loads(row[0])

    def read_segments(self, source: dict) -> list[dict]:
        if not source.get("segments_path"):
            raise ValueError("Source has no archived segments")
        raw = (self.root / source["segments_path"]).read_bytes()
        if source.get("segments_sha256") and digest(raw) != source["segments_sha256"]:
            raise ValueError("Archived source segments changed; review or refresh before citing")
        return json.loads(raw)

    def close_run(self, run_id: str) -> None:
        self.run(run_id)
        with self.db:
            self.db.execute("UPDATE runs SET status='closed' WHERE id=?", (run_id,))
            self.db.execute("DELETE FROM grants WHERE run_id=?", (run_id,))
        # Imported task sessions are distinct from the user's persistent browser profiles.
        folder = self.root / "private/sessions" / run_id
        for platform in ("youtube", "bilibili"):
            (folder / (platform + ".cookies")).unlink(missing_ok=True)
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()

