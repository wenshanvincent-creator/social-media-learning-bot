"""Deterministic admission heuristics, not truth/originality probabilities."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone


DEFAULT_POLICY = {
    "weights": {"platform": .10, "freshness": .20, "method": .30,
                "relevance": .20, "provenance": .15, "popularity": .05},
    "platform_priors": {"youtube": 1.0, "bilibili": .85, "xiaohongshu": .55, "douyin": .45},
    "accept_at": 65, "reject_below": 40, "min_text_chars": 300, "min_method_families": 2,
    "max_age_days": {"principle": None, "implementation": 180, "model": 30, "trend": 90},
    "duplicate_similarity": .82,
}
METHODS = {
    "steps": r"\b(step|first|then|rule|next)\b|步骤|首先|然后|规则|接下来",
    "reasons": r"\b(because|why|therefore|reason|means)\b|因为|原因|原理|因此|为了",
    "specifics": r"\b(example|parameter|prompt|code|setting|contrast)\b|比如|例如|参数|代码|提示词|设置|字号",
    "limits": r"\b(avoid|unless|when|if|instead|don't)\b|避免|不要|如果|限制|适用|注意",
}
PROMO = r"\b(subscribe|buy now|earn money|limited offer|join my course)\b|私信|加微信|购买课程|月入|领取资料|关注点赞"
REPOST = r"\b(reupload(?:ed)?|reposted|original video by)\b|视频搬运|本视频.{0,8}搬运|转载自|转自"


def policy_config(overrides=None):
    p = {**DEFAULT_POLICY, **(overrides or {})}
    for key in ("weights", "platform_priors", "max_age_days"):
        p[key] = {**DEFAULT_POLICY[key], **(overrides or {}).get(key, {})}
    if set(p["weights"]) != set(DEFAULT_POLICY["weights"]):
        raise ValueError("Unknown screening dimension")
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in p["weights"].values()) or not sum(p["weights"].values()):
        raise ValueError("Screening weights must be finite, nonnegative and nonzero in total")
    if not 0 <= p["reject_below"] <= p["accept_at"] <= 100:
        raise ValueError("Use ordered screening thresholds in 0..100")
    if not 0 < p["duplicate_similarity"] <= 1 or p["min_text_chars"] < 1 or not 1 <= p["min_method_families"] <= len(METHODS):
        raise ValueError("Invalid content screening limits")
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in p["platform_priors"].values()):
        raise ValueError("Platform priors must be finite values in 0..1")
    if any(v is not None and (not isinstance(v, int) or v <= 0) for v in p["max_age_days"].values()):
        raise ValueError("Age windows must be positive days or null")
    return p


def infer_kind(topic):
    if re.search(r"模型|\b(claude|gpt|gemini|llm|opus|model)\b", topic, re.I):
        return "model"
    if re.search(r"趋势|流行|\b(trend|trending)\b", topic, re.I):
        return "trend"
    if re.search(r"\b(api|sdk|remotion|manim|version|framework)\b|版本|框架", topic, re.I):
        return "implementation"
    return "principle"


def age_days(value, today=None):
    if not value:
        return None
    try:
        date = datetime.strptime(value[:8], "%Y%m%d").date() if re.fullmatch(r"\d{8}", value) else datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None
    return ((today or datetime.now(timezone.utc).date()) - date).days


def metadata_gate(source, kind, policy, today=None):
    words = (source.get("title") or "") + "\n" + (source.get("description") or "")[:5000]
    if re.search(REPOST, words, re.I):
        return {"decision": "rejected", "reasons": ["declared_repost_or_credit; not a copyright finding"]}
    age = age_days(source.get("published_at") or source.get("published"), today)
    maximum = policy["max_age_days"][kind]
    if age is not None and age < 0:
        return {"decision": "review", "reasons": ["future_publication_date"]}
    if maximum is not None and (age is None or age > maximum):
        return {"decision": "review", "reasons": ["date_unknown" if age is None else "outside_discovery_freshness_window"]}
    return None


def popularity(source):
    values = []
    for key, cap in (("view_count", 100_000_000), ("channel_follower_count", 10_000_000)):
        value = source.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            values.append(min(1, math.log1p(value) / math.log1p(cap)))
    return sum(values) / len(values) if values else None


def relevance(topic, text):
    terms = set(re.findall(r"[a-z][a-z0-9-]{2,}|[\u4e00-\u9fff]{2,}", topic.lower()))
    aliases = {"审美": "design typography layout color motion", "设计": "design typography composition layout",
               "字体": "typography font type", "配色": "color colour palette", "视频": "video animation motion"}
    for key, words in aliases.items():
        if key in topic:
            terms.update(words.split())
    hits = [term for term in terms if term in text.lower()]
    return min(1, len(hits) / min(3, len(terms))) if terms else None


def fingerprint(text):
    normalized = "".join(re.findall(r"\w", text.lower()))
    return {normalized[i:i+7] for i in range(max(1, len(normalized)-6))}


def screen(source, segments, topic, kind, policy, seen=None, today=None):
    gate = metadata_gate(source, kind, policy, today)
    if gate:
        return gate
    text = "\n".join(s["text"] for s in segments)
    tokens = fingerprint(text)
    for source_id, previous in (seen or {}).items():
        similarity = len(tokens & previous) / max(1, len(tokens | previous))
        if similarity >= policy["duplicate_similarity"]:
            return {"decision": "rejected", "reasons": ["near_duplicate_text"],
                    "duplicate_of": source_id, "similarity": round(similarity, 3),
                    "originality": "not_determined"}
    method_hits = [key for key, pattern in METHODS.items() if re.search(pattern, text, re.I)]
    promo_ratio = sum(bool(re.search(PROMO, s["text"], re.I)) for s in segments) / max(1, len(segments))
    age = age_days(source.get("published_at") or source.get("published"), today)
    maximum = policy["max_age_days"][kind]
    dimensions = {
        "platform": policy["platform_priors"].get(source.get("platform")),
        "freshness": 1 if maximum is None else max(0, 1-age/maximum) if age is not None else None,
        "method": len(method_hits) / len(METHODS),
        "relevance": relevance(topic, (source.get("title") or "") + "\n" + text),
        "provenance": .8 if re.search(r"https://(?:github\.com|[\w.-]+/docs/)", source.get("description") or "") else .5,
        "popularity": popularity(source),
    }
    active = {k: v for k, v in dimensions.items() if v is not None}
    denominator = sum(policy["weights"][k] for k in active)
    score = round(100 * sum(policy["weights"][k] * v for k, v in active.items()) / denominator, 1) if denominator else 0
    reasons = []
    if promo_ratio >= .35:
        decision, reasons = "rejected", ["high_call_to_action_segment_ratio"]
    elif len(re.sub(r"\s", "", text)) < policy["min_text_chars"] or len(method_hits) < policy["min_method_families"]:
        decision, reasons = "review", ["insufficient_explanatory_signals_for_auto_admission"]
    else:
        decision = "selected" if score >= policy["accept_at"] else "rejected" if score < policy["reject_below"] else "review"
        reasons = ["heuristic_score_threshold"]
    return {"decision": decision, "score": score, "dimensions": dimensions,
            "missing_dimensions": [k for k, v in dimensions.items() if v is None],
            "method_families": method_hits, "promo_segment_ratio": round(promo_ratio, 3), "reasons": reasons,
            "interpretation": "heuristic priority; not truth, depth, originality or accuracy probability"}
