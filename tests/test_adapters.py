import json
from pathlib import Path

import pytest

from sociallearn import adapters
from sociallearn.store import Workspace, atomic_json


@pytest.fixture
def setup(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    run = ws.create_run("synthetic adapter contract test")
    yield ws, run, tmp_path
    ws.db.close()


def test_redcrack_import_preserves_text_but_excludes_access_tokens(setup):
    ws, run, folder = setup
    atomic_json(folder / "task.json", {"status": "complete"})
    atomic_json(folder / "results.json", {"schema_version": 1, "complete": True, "count": 1, "notes": [
        {"note_id": "a" * 24, "title": "synthetic typography note", "description": "A real exported text field.",
         "url": "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=synthetic-private-token",
         "media": [{"url": "https://cdn.example.org/private-media"}]}]})
    result = adapters.import_redcrack(ws, run["id"], folder)
    source = ws.source(result["sources"][0]["id"])
    assert "xsec_token" not in json.dumps(source)
    assert "private-media" not in json.dumps(result)
    assert ws.read_segments(source)[0]["text"] == "A real exported text field."
    assert source["observed_at"] is None  # Do not turn import time into observation time.


def test_incomplete_redcrack_is_not_empty_success(setup):
    ws, run, folder = setup
    atomic_json(folder / "task.json", {"status": "running"})
    with pytest.raises(ValueError, match="not terminal"):
        adapters.import_redcrack(ws, run["id"], folder)
    atomic_json(folder / "task.json", {"status": "verification_required"})
    adapters.import_redcrack(ws, run["id"], folder)
    with pytest.raises(PermissionError):
        ws.check_access(run["id"], "xiaohongshu")


def test_redcrack_search_reimport_keeps_archived_text_and_citations(setup):
    from sociallearn.wiki import Wiki
    ws, run, folder = setup
    atomic_json(folder / "task.json", {"status": "complete"})
    note = {"note_id": "a" * 24, "title": "Detailed title", "description": "Archived actual explanation.",
            "observed_at": "2026-10-24T00:00:00+00:00"}
    def export(value):
        atomic_json(folder / "results.json", {"schema_version": 1, "complete": True, "count": 1, "notes": [value]})
        return adapters.import_redcrack(ws, run["id"], folder)["sources"][0]["id"]
    source_id = export(note)
    before = ws.source(source_id)
    wiki = Wiki(ws)
    wiki.put({"id": "post-method", "title": "Synthetic method", "summary": "Archived explanation",
              "kind": "principle", "applies_to": "design", "verification_status": "source_described",
              "body": "The explanation is preserved.", "citations": [{"source_id": source_id, "segment_ids": ["p0001"]}]})
    export({"note_id": "a" * 24, "title": "", "description": "", "observed_at": "2026-10-24T02:00:00+00:00"})
    after = ws.source(source_id)
    assert after["status"] == "post_text_archived"
    assert after["title"] == before["title"] and after["observed_at"] == before["observed_at"]
    assert after["metadata_observed_at"] == "2026-10-24T02:00:00+00:00"
    assert after["content_hash"] == before["content_hash"]
    assert ws.read_segments(after)[0]["text"] == note["description"]
    assert wiki.read(["post-method"])["blocks"][0]["body"]
    export({**note, "description": "A changed explanation."})
    assert wiki.read(["post-method"])["deferred"][0]["reason"]["status"] == "source_changed"
    revision = ws.root / "state/source-revisions" / source_id / before["content_hash"] / "segments.json"
    assert json.loads(revision.read_text(encoding="utf-8"))[0]["text"] == note["description"]


def test_douyin_metadata_cannot_masquerade_as_transcript(setup):
    ws, run, _ = setup
    job = {"id": "synthetic-job", "status": "completed", "result": {"videos": [
        {"id": "1234567890", "url": "https://www.douyin.com/video/1234567890", "title": "Synthetic title"}]}}
    result = adapters.import_douyin(ws, run["id"], job)
    source = ws.source(result["sources"][0]["id"])
    assert source["status"] == "metadata_only"
    with pytest.raises(ValueError, match="no archived segments"):
        ws.read_segments(source)
    job["result"]["videos"][0]["url"] = "https://www.douyin.com/video/9999999999"
    with pytest.raises(ValueError, match="mismatch"):
        adapters.import_douyin(ws, run["id"], job)


def test_studio_consent_checked_before_network(setup, monkeypatch):
    ws, run, folder = setup
    def no_network(*a, **kw):
        pytest.fail("Network called without consent")
    monkeypatch.setattr(adapters, "_studio_request", no_network)
    with pytest.raises(PermissionError):
        adapters.studio_search(ws, run["id"], "test", "http://127.0.0.1:4321", folder / "receipt.json")


def test_unknown_studio_submission_never_reposts(setup, monkeypatch):
    ws, run, folder = setup
    ws.consent(run["id"], "douyin", True, "synthetic human grant")
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise ValueError("Network observation timeout")
    monkeypatch.setattr(adapters, "_studio_request", uncertain)
    receipt = folder / "receipt.json"
    with pytest.raises(ValueError):
        adapters.studio_search(ws, run["id"], "test", "http://127.0.0.1:4321", receipt)
    with pytest.raises(ValueError, match="outcome unknown"):
        adapters.studio_search(ws, run["id"], "test", "http://127.0.0.1:4321", receipt)
    assert len(calls) == 1


def test_saved_studio_job_resumes_without_post(setup, monkeypatch):
    ws, run, folder = setup
    ws.consent(run["id"], "douyin", True, "synthetic human grant")
    receipt = folder / "receipt.json"
    atomic_json(receipt, {"run_id": run["id"], "query": "test", "origin": "http://127.0.0.1:4321", "job_id": "known"})
    def state(origin, route, body=None):
        assert route == "state" and body is None
        return {"jobs": [{"id": "known", "status": "completed", "result": {"videos": []}}]}
    monkeypatch.setattr(adapters, "_studio_request", state)
    assert adapters.studio_search(ws, run["id"], "test", "http://127.0.0.1:4321", receipt)["status"] == "completed"


def test_studio_remote_origins_rejected():
    with pytest.raises(ValueError):
        adapters.studio_origin("http://127.0.0.1.example.org:4321")
    with pytest.raises(ValueError):
        adapters.studio_origin("http://:synthetic-secret@127.0.0.1:4321")


def test_redcrack_uses_real_two_command_contract(setup, monkeypatch):
    from types import SimpleNamespace
    ws, run, folder = setup
    ws.consent(run["id"], "xiaohongshu", True, "synthetic human grant")
    repo = folder / "repo"; repo.mkdir(); (repo / "xhs.py").write_text("# synthetic fixture")
    python = folder / "python.exe"; python.write_text("synthetic fixture")
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if command[0] == "git":
            return SimpleNamespace(stdout="synthetic-pinned-sha\n", returncode=0)
        output = Path(command[command.index("--output") + 1])
        if "search" in command:
            assert "--metadata-only" not in command
            atomic_json(output / "task.json", {"status": "complete", "command": "search"})
        else:
            assert "download" in command and "--from-json" in command and "--metadata-only" in command
            atomic_json(output / "task.json", {"status": "complete", "command": "download"})
        atomic_json(output / "results.json", {"schema_version": 1, "complete": True, "count": 1,
                    "notes": [{"note_id": "a"*24, "description": "Exported detail text"}]})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(adapters.subprocess, "run", execute)
    result = adapters.redcrack_search(ws, run["id"], "query", repo, python, "synthetic-pinned-sha")
    assert result["sources"][0]["status"] == "post_text_archived"
    assert len(calls) == 3


def test_studio_http_headers_and_job_roundtrip(setup):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    ws, run, folder = setup
    ws.consent(run["id"], "douyin", True, "synthetic human grant")
    seen = []
    class Server(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["X-Studio-Client"], body))
            self.reply({"id": "http-job", "status": "queued"})
        def do_GET(self):
            self.reply({"jobs": [{"id": "http-job", "status": "completed", "result": {"videos": []}}]})
        def reply(self, data):
            raw = json.dumps(data).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = adapters.studio_search(ws, run["id"], "synthetic-topic", f"http://127.0.0.1:{server.server_port}", folder / "receipt.json")
        assert result["status"] == "completed"
        assert seen == [("/api/search", "1", {"query": "synthetic-topic", "limit": 5})]
    finally:
        server.shutdown(); thread.join(); server.server_close()
