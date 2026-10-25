from __future__ import annotations

import os
import json
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .store import Workspace

PLATFORMS = {
    "bilibili": {"home": "https://www.bilibili.com", "domain": "bilibili.com", "login_cookie": "SESSDATA"},
    "youtube": {"home": "https://www.youtube.com", "domain": "youtube.com", "login_cookie": "SAPISID"},
    "xiaohongshu": {"delegated": True},
    "douyin": {"delegated": True},
}


def _context(ws: Workspace, platform: str, *, visible: bool):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        candidates = [Path(os.environ.get("SOCIALLEARN_BROWSER_PATH", "")),
                      Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
                      Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe"]
        executable = next((str(p) for p in candidates if p.is_file()), None)
        location = {"executable_path": executable} if executable else {"channel": "chrome"}
        context = pw.chromium.launch_persistent_context(
            str(ws.root / "private/profiles" / platform),
            **location, headless=not visible,
        )
    except Exception:
        pw.stop()
        raise
    return pw, context


def login(ws: Workspace, run_id: str, platform: str, timeout: int = 180) -> dict:
    ws.require_consent(run_id, platform)
    # Manual login is how the human can inspect/resolve a pause. It never clears
    # that pause; acquisition and cookie export still check_access independently.
    cfg = PLATFORMS[platform]
    if cfg.get("delegated"):
        raise ValueError("This platform delegates login/profile ownership to its configured collector")
    pw, context = _context(ws, platform, visible=True)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg["home"], wait_until="domcontentloaded", timeout=30000)
        page.bring_to_front()
        print(json.dumps({"event": "login_browser_ready", "platform": platform,
                          "message": "Log in in the dedicated browser; no password/cookie enters chat"}),
              file=sys.stderr, flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cookies = context.cookies(cfg["home"])
            if any(c["name"] == cfg["login_cookie"] and c["value"] for c in cookies):
                return {"platform": platform, "status": "session_present", "capability_verified": False}
            page.wait_for_timeout(1500)
        return {"platform": platform, "status": "awaiting_login", "capability_verified": False}
    finally:
        context.close()
        pw.stop()


def netscape(cookies: list[dict], domain: str) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        host = c["domain"].lstrip(".")
        if host != domain and not host.endswith("." + domain):
            continue
        if c.get("partitionKey"):
            raise ValueError("Partitioned cookies cannot be exported as Netscape cookies")
        name, value = c["name"], c["value"]
        if any(ch in name + value for ch in "\t\r\n"):
            raise ValueError("Invalid cookie field")
        exported_domain = ("#HttpOnly_" if c.get("httpOnly") else "") + c["domain"]
        lines.append("\t".join([
            exported_domain, "TRUE" if c["domain"].startswith(".") else "FALSE",
            c.get("path", "/"), "TRUE" if c.get("secure") else "FALSE",
            str(max(0, int(c.get("expires", 0)))), name, value,
        ]))
    return "\n".join(lines) + "\n"


def import_cookie_file(ws: Workspace, run_id: str, platform: str, source: Path) -> dict:
    """Import a human-exported, single-platform session without opening a profile."""
    ws.require_consent(run_id, platform)
    cfg = PLATFORMS[platform]
    if cfg.get("delegated"):
        raise ValueError("Use the delegated collector for this platform")
    if source.stat().st_size > 1_000_000:
        raise ValueError("Cookie export is unexpectedly large; export only this platform")
    lines, logged_in = ["# Netscape HTTP Cookie File"], False
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError("Use a Netscape-format cookie export; contents are not logged")
        host = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        if host != cfg["domain"] and not host.endswith("." + cfg["domain"]):
            raise ValueError("Export only the current platform, not all websites")
        try:
            expires = int(fields[4])
        except ValueError:
            raise ValueError("Invalid cookie expiry; contents are not logged") from None
        if expires > 0 and expires <= time.time():
            continue
        if fields[5] == cfg["login_cookie"] and fields[6]:
            logged_in = True
        lines.append(line)
    if not logged_in:
        raise ValueError("No usable platform login cookie in this export")
    folder = ws.root / "private/sessions" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (platform + ".cookies")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as file:
        temporary = Path(file.name)
        os.chmod(temporary, 0o600)
        file.write("\n".join(lines) + "\n")
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"run_id": run_id, "platform": platform, "status": "session_imported", "capability_verified": False}


@contextmanager
def cookie_file(ws: Workspace, run_id: str, platform: str, enabled: bool):
    ws.check_access(run_id, platform)
    if not enabled:
        yield None
        return
    # Check before opening the profile, including when it already exists.
    ws.require_consent(run_id, platform)
    cfg = PLATFORMS[platform]
    imported = ws.root / "private/sessions" / run_id / (platform + ".cookies")
    if imported.is_file():
        content = imported.read_text(encoding="utf-8")
    else:
        pw, context = _context(ws, platform, visible=False)
        try:
            cookies = context.cookies(cfg["home"])
            if not any(c["name"] == cfg["login_cookie"] and c["value"] for c in cookies):
                raise PermissionError("Session missing/expired; use login or import-cookies after consent")
            content = netscape(cookies, cfg["domain"])
        finally:
            # Do not hold an exclusive browser profile during a long media download.
            context.close()
            pw.stop()
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".cookies",
                                         dir=ws.root / "private", delete=False) as f:
            path = Path(f.name)
            os.chmod(path, 0o600)
            f.write(content)
        # yt-dlp consumes only this task's domain-scoped file, not a browser DB.
        yield str(path)
    finally:
        if path:
            path.unlink(missing_ok=True)

