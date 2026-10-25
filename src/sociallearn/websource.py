from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .store import Workspace, atomic_json, digest, now


class TextPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []
        self.title_parts = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag in ("p", "h1", "h2", "h3", "li", "pre", "br"):
            self.parts.append("\n")
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip -= 1
        if tag in ("p", "h1", "h2", "h3", "li", "pre"):
            self.parts.append("\n")
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.skip:
            self.parts.append(data)


def archive(ws: Workspace, run_id: str, url: str) -> dict:
    if ws.run(run_id)["status"] != "active":
        raise ValueError("Run is closed")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Archive requires a public HTTPS source URL")
    # Archive only explicit URLs chosen by the calling agent. Never follow page instructions.
    with urlopen(Request(url, headers={"User-Agent": "SocialLearn/0.1 personal research"}), timeout=25) as response:
        raw = response.read(4_000_001)
        final = response.url
    if len(raw) > 4_000_000:
        raise ValueError("Page exceeds text archive limit")
    page = TextPage()
    page.feed(raw.decode("utf-8", errors="replace"))
    paragraphs = [re.sub(r"\s+", " ", line).strip() for line in "".join(page.parts).splitlines()]
    paragraphs = [p for p in paragraphs if p]
    if sum(map(len, paragraphs)) < 100:
        raise ValueError("No sufficient text; a browser may be required")
    pieces = [p[i:i+1200] for p in paragraphs for i in range(0, len(p), 1200)]
    segments = [{"id": f"p{i:04}", "text": p} for i, p in enumerate(pieces, 1)]
    source_id = "web-" + digest(url.encode())[:16]
    folder = ws.root / "private/raw" / source_id
    folder.mkdir(parents=True, exist_ok=True)
    if ws.db.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
        ws.snapshot_source(source_id)
    (folder / "original.html").write_bytes(raw)
    atomic_json(folder / "segments.json", segments)
    source = {"id": source_id, "run_id": run_id, "platform": "web", "url": url, "resolved_url": final,
              "title": "".join(page.title_parts).strip(), "observed_at": now(), "published_at": None,
              "status": "text_archived", "visual_status": "not_inspected", "transcript_origin": "web_text",
              "content_hash": digest(str(segments).encode()),
              "segments_sha256": digest((folder / "segments.json").read_bytes()),
              "segments_path": str((folder / "segments.json").relative_to(ws.root))}
    return ws.save_source(source)

