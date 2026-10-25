from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse


def as_date(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


OFFICIAL_MODEL_HOSTS = {
    "openai": ("openai.com",),
    "anthropic": ("anthropic.com", "claude.com"),
    "google": ("ai.google.dev", "deepmind.google", "cloud.google.com"),
    "deepseek": ("deepseek.com",),
    "qwen": ("qwen.ai", "qwenlm.github.io"),
}


def assess(meta: dict, *, today: date | None = None, target_version: str | None = None,
           current: bool = False) -> dict:
    today = today or datetime.now(timezone.utc).date()
    kind = meta.get("kind", "principle")
    checked = as_date(meta.get("verified_at"))
    if checked and checked > today:
        return {"status": "invalid", "reason": "verification date is in the future"}
    if target_version and meta.get("version") and meta["version"] != target_version:
        return {"status": "incompatible", "reason": "target version differs from this knowledge block"}
    if kind == "model":
        host = urlparse(meta.get("official_url", "")).hostname or ""
        trusted = any(host == domain or host.endswith("." + domain)
                      for domain in OFFICIAL_MODEL_HOSTS.get(meta.get("provider", ""), ()))
        if not meta.get("model_id") or not trusted or not checked:
            return {"status": "unverified", "reason": "exact model ID and dated official verification required"}
        if current:
            return {"status": "live_check_required", "reason": "model selection/latest claims require a new official check"}
        if checked < today - timedelta(days=7):
            return {"status": "review_due", "reason": "model verification older than 7 days; recheck before advice"}
    deadline = as_date(meta.get("review_after"))
    if deadline and deadline <= today:
        return {"status": "review_due", "reason": "scheduled review is due"}
    if kind in ("model", "implementation", "trend") and not checked:
        return {"status": "unverified", "reason": "verification date is missing"}
    if kind == "implementation" and not meta.get("version"):
        return {"status": "unverified", "reason": "implementation version or documentation snapshot is missing"}
    if kind == "trend" and not deadline:
        return {"status": "unverified", "reason": "trend requires an observation window/review date"}
    return {"status": "usable", "reason": "no known freshness or version issue; evidence limits still apply"}

