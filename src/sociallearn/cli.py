from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

from . import adapters, auth, collection, collector_media, media, websource
from .analysis import AnalysisJobs
from .textpacks import TextPacks
from .citations import expand_ids
from .store import Workspace
from .wiki import Wiki, estimate


def parser():
    p = argparse.ArgumentParser(prog="sociallearn")
    p.add_argument("--root", default="workspace", help="Private data + Markdown workspace")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    new = sub.add_parser("init")
    new.add_argument("--topic", required=True)
    grant = sub.add_parser("consent")
    grant.add_argument("--run", required=True)
    grant.add_argument("--platform", choices=auth.PLATFORMS, required=True)
    choice = grant.add_mutually_exclusive_group(required=True)
    choice.add_argument("--grant", action="store_true")
    choice.add_argument("--deny", action="store_true")
    grant.add_argument("--evidence", required=True, help="Human authorization text, never secrets")
    resolved = sub.add_parser("resolve-pause", help="Only after the human explicitly reports this platform's restriction resolved")
    resolved.add_argument("--run", required=True)
    resolved.add_argument("--platform", choices=auth.PLATFORMS, required=True)
    resolved.add_argument("--evidence", required=True)
    log = sub.add_parser("login")
    log.add_argument("--run", required=True)
    log.add_argument("--platform", choices=auth.PLATFORMS, required=True)
    log.add_argument("--timeout", type=int, default=180)
    imported = sub.add_parser("import-cookies", help="Import a human-exported, domain-only Netscape session for this run")
    imported.add_argument("--run", required=True)
    imported.add_argument("--platform", choices=("youtube", "bilibili"), required=True)
    imported.add_argument("file", type=Path)
    search = sub.add_parser("search")
    search.add_argument("--run", required=True)
    search.add_argument("--platform", choices=("bilibili", "youtube"), default="youtube", help="Default: youtube")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--cookies", action="store_true")
    collect = sub.add_parser("collect-topic", aliases=["learn-topic"], help="Batch topic search, screening and balanced multi-video evidence for synthesis")
    collect.add_argument("topic")
    collect.add_argument("--run")
    collect.add_argument("--platform", choices=("youtube", "bilibili"), default="youtube")
    collect.add_argument("--limit", type=int, default=12)
    collect.add_argument("--search-limit", type=int, default=30)
    collect.add_argument("--max-minutes", type=int, default=20)
    collect.add_argument("--total-minutes", type=int, default=180)
    collect.add_argument("--max-per-creator", type=int, default=2)
    collect.add_argument("--budget", type=int, default=8000, help="Shared evidence-body budget across selected videos")
    collect.add_argument("--encoding", help="Optional named reference tokenizer")
    collect.add_argument("--question", action="append", default=[], help="Repeat up to eight learning questions for lexical evidence windows")
    collect.add_argument("--details", action="store_true", help="Return collection paths/statistics instead of learning text")
    collect.add_argument("--cookies", action="store_true")
    collect.add_argument("--model", choices=("tiny", "base", "small"), default="small")
    collect.add_argument("--ocr", choices=("auto", "off"), default="auto")
    collect.add_argument("--ocr-language", default="")
    collect.add_argument("--max-images", type=int, default=3)
    collect.add_argument("--kind", choices=("auto", "principle", "implementation", "model", "trend"), default="auto")
    collect.add_argument("--policy", type=Path)
    collect.add_argument("--include-text", action="store_true", help="Explicitly return all selected text instead of file pointers")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--run", required=True)
    fetch.add_argument("url")
    fetch.add_argument("--cookies", action="store_true")
    fetch.add_argument("--asr", action="store_true")
    fetch.add_argument("--model", choices=("tiny", "base", "small"), default="small")
    fetch.add_argument("--max-minutes", type=int, default=30)
    fetch.add_argument("--refresh", action="store_true", help="Explicitly refetch changed captions; keep prior revision")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--run", required=True)
    inspect.add_argument("url")
    inspect.add_argument("--cookies", action="store_true")
    video = sub.add_parser("download-video")
    video.add_argument("--run", required=True)
    video.add_argument("url")
    video.add_argument("--cookies", action="store_true")
    video.add_argument("--max-minutes", type=int, default=10)
    frame = sub.add_parser("frame")
    frame.add_argument("id")
    frame.add_argument("--at", type=float, required=True)
    review = sub.add_parser("review-frames")
    review.add_argument("id")
    review.add_argument("--at", type=float, action="append", required=True)
    review.add_argument("--observation", required=True)
    review.add_argument("--reviewer", required=True)
    web = sub.add_parser("archive-web")
    web.add_argument("--run", required=True)
    web.add_argument("url")
    source = sub.add_parser("source-read")
    source.add_argument("id")
    source.add_argument("--offset", type=int, default=0)
    source.add_argument("--limit", type=int, default=10)
    source.add_argument("--segment", action="append")
    put = sub.add_parser("put")
    put.add_argument("file", type=Path)
    put.add_argument("--expected-hash")
    find = sub.add_parser("find")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=5)
    find.add_argument("--version")
    find.add_argument("--current", action="store_true")
    read = sub.add_parser("read")
    read.add_argument("ids", nargs="+")
    read.add_argument("--budget", type=int, default=3000)
    read.add_argument("--context", help="Fresh context ID; rotate after compaction")
    read.add_argument("--current", action="store_true")
    read.add_argument("--version")
    read.add_argument("--historical", action="store_true")
    sub.add_parser("reindex")
    close = sub.add_parser("close-run")
    close.add_argument("--run", required=True)
    status = sub.add_parser("status")
    status.add_argument("--run", required=True)
    red = sub.add_parser("redcrack-search")
    red.add_argument("--run", required=True)
    red.add_argument("query")
    red.add_argument("--repo", type=Path, required=True)
    red.add_argument("--python", type=Path, required=True)
    red.add_argument("--commit", required=True)
    red.add_argument("--limit", type=int, default=5)
    imp = sub.add_parser("import-redcrack")
    imp.add_argument("--run", required=True)
    imp.add_argument("folder", type=Path)
    dou = sub.add_parser("studio-search")
    dou.add_argument("--run", required=True)
    dou.add_argument("query")
    dou.add_argument("--origin", default="http://127.0.0.1:4321")
    dou.add_argument("--receipt", type=Path, required=True)
    dou.add_argument("--limit", type=int, default=5)
    dou.add_argument("--timeout", type=int, default=60)
    dj = sub.add_parser("import-studio")
    dj.add_argument("--run", required=True)
    dj.add_argument("file", type=Path)
    dv = sub.add_parser("studio-video", help="Download a selected source through its local Studio job")
    dv.add_argument("--run", required=True)
    dv.add_argument("source")
    dv.add_argument("--origin", default="http://127.0.0.1:4321")
    dv.add_argument("--receipt", type=Path, required=True)
    dv.add_argument("--timeout", type=int, default=60)
    local = sub.add_parser("transcribe-local", help="Explicit Whisper for an already archived video")
    local.add_argument("--run", required=True)
    local.add_argument("source")
    local.add_argument("--model", choices=("tiny", "base", "small"), default="small")
    xm = sub.add_parser("import-redcrack-media")
    xm.add_argument("--run", required=True)
    xm.add_argument("folder", type=Path)
    imgs = sub.add_parser("source-images")
    imgs.add_argument("source")
    imgs.add_argument("--offset", type=int, default=0)
    imgs.add_argument("--limit", type=int, default=5)
    ri = sub.add_parser("review-images")
    ri.add_argument("source")
    ri.add_argument("--image", action="append", required=True)
    ri.add_argument("--observation", required=True)
    ri.add_argument("--reviewer", required=True)
    sc = sub.add_parser("studio-captions")
    sc.add_argument("--run", required=True)
    sc.add_argument("source")
    sc.add_argument("--receipt", type=Path, required=True)
    sc.add_argument("--timeout", type=int, default=60)
    sc.add_argument("--refresh", action="store_true")
    prep = sub.add_parser("analysis-prepare", help="Create a file packet for an isolated native agent")
    prep.add_argument("--run", required=True)
    prep.add_argument("--source", required=True)
    prep.add_argument("--focus", required=True)
    prep.add_argument("--collection", help="Only admit selected sources; pass their cleaned text packet to the worker")
    prep.add_argument("--force", action="store_true", help="Reanalyze instead of reusing unchanged, already admitted methods")
    pack = sub.add_parser("pack-text", help="Retrieve related whole passages, deduplicate and cache a bounded text packet")
    pack.add_argument("--source", action="append", default=[])
    pack.add_argument("--collection")
    pack.add_argument("--focus", required=True)
    pack.add_argument("--budget", type=int, default=2500)
    pack.add_argument("--encoding", help="Optional reference tokenizer, e.g. o200k_base; not full model billing")
    pack.add_argument("--details", action="store_true", help="Diagnostic card with local paths/statistics instead of text")
    pack.add_argument("--balanced", action="store_true", help="Cover selected sources in turn for a broad topic")
    pack.add_argument("--question", action="append", default=[], help="Repeat learning questions; select complete neighboring sentence windows")
    pack.add_argument("--require-citations", type=Path, help="JSON array of exact text citations to retain with neighboring context; requires --question")
    packed = sub.add_parser("read-pack")
    packed.add_argument("id")
    packed.add_argument("--details", action="store_true", help="Include diagnostic metadata/statistics")
    submit = sub.add_parser("analysis-submit")
    submit.add_argument("id")
    submit.add_argument("file", type=Path)
    jobs = sub.add_parser("analysis-list")
    jobs.add_argument("--run", required=True)
    jobs.add_argument("--limit", type=int, default=10)
    candidate = sub.add_parser("analysis-read")
    candidate.add_argument("id")
    candidate.add_argument("candidate")
    candidate.add_argument("--budget", type=int, default=3000)
    admit = sub.add_parser("analysis-admit")
    admit.add_argument("id")
    admit.add_argument("candidate")
    admit.add_argument("--expected-hash")
    return p


def execute(args):
    ws = Workspace(args.root)
    try:
        c = args.command
        if c == "doctor":
            packages = {}
            for name in ("yt-dlp", "playwright", "PyYAML", "imageio-ffmpeg", "faster-whisper"):
                try:
                    packages[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    packages[name] = "not installed"
            return {"python": sys.version.split()[0], "packages": packages,
                    "model_loaded": False, "claude_started": False}
        if c == "init":
            return ws.create_run(args.topic)
        if c == "consent":
            return ws.consent(args.run, args.platform, args.grant, args.evidence)
        if c == "resolve-pause":
            return ws.resolve_pause(args.run, args.platform, args.evidence)
        if c == "login":
            return auth.login(ws, args.run, args.platform, args.timeout)
        if c == "import-cookies":
            return auth.import_cookie_file(ws, args.run, args.platform, args.file)
        if c == "search":
            return media.search(ws, args.run, args.platform, args.query, args.limit, args.cookies)
        if c in ("collect-topic", "learn-topic"):
            if not 1 <= args.budget <= 100000:
                raise ValueError("Evidence budget must be in 1..100000")
            result = collection.collect_topic(ws, args.topic, run_id=args.run, platform=args.platform,
                limit=args.limit, search_limit=args.search_limit, max_minutes=args.max_minutes,
                total_minutes=args.total_minutes, cookies=args.cookies, model=args.model,
                ocr_mode=args.ocr, ocr_language=args.ocr_language, max_images=args.max_images,
                kind=args.kind, policy=json.loads(args.policy.read_text(encoding="utf-8-sig")) if args.policy else None,
                include_text=args.include_text, max_per_creator=args.max_per_creator)
            if args.details or args.include_text:
                return result
            if not result["selected"]:
                return {"body": "", "review": ["collection_" + result["status"]]}
            packs = TextPacks(ws)
            card = packs.prepare([], args.topic, budget=args.budget, encoding=args.encoding,
                                 collection_id=result["id"], balanced=True, questions=args.question)
            payload = packs.read(card["id"])
            if result["status"] != "ready":
                payload.setdefault("review", []).append("collection_" + result["status"])
            if len(result["selected"]) < min(3, args.limit):
                payload.setdefault("review", []).append("few_sources")
            return payload
        if c == "fetch":
            return media.fetch(ws, args.run, args.url, cookies=args.cookies, max_minutes=args.max_minutes,
                               asr=args.asr, model=args.model, refresh=args.refresh)
        if c == "inspect":
            return media.inspect(ws, args.run, args.url, args.cookies)
        if c == "download-video":
            return media.download_video(ws, args.run, args.url, args.cookies, args.max_minutes)
        if c == "frame":
            return media.frame(ws, args.id, args.at)
        if c == "review-frames":
            return media.review_frames(ws, args.id, args.at, args.observation, args.reviewer)
        if c == "archive-web":
            return websource.archive(ws, args.run, args.url)
        if c == "source-read":
            if not 1 <= args.limit <= 20 or args.offset < 0:
                raise ValueError("Use limit 1..20 and nonnegative offset")
            source = ws.source(args.id)
            segments = ws.read_segments(source)
            if args.segment:
                requested = set(expand_ids(args.segment, [s["id"] for s in segments]))
                segments = [s for s in segments if s["id"] in requested]
            selected = segments[args.offset:args.offset + args.limit]
            header = {k: source[k] for k in ("id", "platform", "url", "title", "published_at", "observed_at",
                       "duration", "transcript_origin", "visual_status", "transcript_review_status", "evidence_origin", "parent_source_id", "evidence_channel") if k in source}
            return {"source": header, "segments": selected, "total_segments": len(segments),
                    "next_offset": args.offset + len(selected) if args.offset + len(selected) < len(segments) else None,
                    "estimated_tokens": estimate(json.dumps(selected, ensure_ascii=False))}
        if c == "close-run":
            ws.close_run(args.run)
            return {"run": args.run, "status": "closed", "consent_revoked": True}
        if c == "status":
            return {"run": ws.run(args.run), "pauses": [dict(r) for r in ws.db.execute("SELECT * FROM pauses")],
                    "sources": [{"id": r[0], "status": json.loads(r[1])["status"]} for r in
                                ws.db.execute("SELECT id,data FROM sources WHERE run_id=?", (args.run,))]}
        if c == "redcrack-search":
            return adapters.redcrack_search(ws, args.run, args.query, args.repo, args.python, args.commit, args.limit)
        if c == "import-redcrack":
            return adapters.import_redcrack(ws, args.run, args.folder)
        if c == "studio-search":
            return adapters.studio_search(ws, args.run, args.query, args.origin, args.receipt, args.limit, args.timeout)
        if c == "import-studio":
            return adapters.import_douyin(ws, args.run, json.loads(args.file.read_text(encoding="utf-8")))
        if c == "studio-video":
            return adapters.studio_video(ws, args.run, args.source, args.origin, args.receipt, args.timeout)
        if c == "transcribe-local":
            return media.transcribe_local(ws, args.run, args.source, args.model)
        if c == "import-redcrack-media":
            return collector_media.import_redcrack_media(ws, args.run, args.folder)
        if c == "source-images":
            return collector_media.source_images(ws, args.source, args.offset, args.limit)
        if c == "review-images":
            return collector_media.review_images(ws, args.source, args.image, args.observation, args.reviewer)
        if c == "studio-captions":
            return collector_media.studio_captions(ws, args.run, args.source, args.receipt, args.timeout, args.refresh)
        if c == "analysis-prepare":
            return AnalysisJobs(ws).prepare(args.run, args.source, args.focus, args.collection, args.force)
        if c == "pack-text":
            packs = TextPacks(ws)
            card = packs.prepare(args.source, args.focus, budget=args.budget,
                                 encoding=args.encoding, collection_id=args.collection, balanced=args.balanced, questions=args.question,
                                 required_citations=json.loads(args.require_citations.read_text(encoding="utf-8")) if args.require_citations else None)
            return card if args.details else packs.read(card["id"])
        if c == "read-pack":
            return TextPacks(ws).read(args.id, details=args.details)
        if c == "analysis-submit":
            return AnalysisJobs(ws).submit(args.id, json.loads(args.file.read_text(encoding="utf-8-sig")))
        if c == "analysis-list":
            return AnalysisJobs(ws).list(args.run, args.limit)
        if c == "analysis-read":
            return AnalysisJobs(ws).read(args.id, args.candidate, args.budget)
        if c == "analysis-admit":
            return AnalysisJobs(ws).admit(args.id, args.candidate, args.expected_hash)
        wiki = Wiki(ws)
        if c == "put":
            return wiki.put(json.loads(args.file.read_text(encoding="utf-8-sig")), args.expected_hash)
        if c == "find":
            return wiki.search(args.query, args.limit, target_version=args.version, current=args.current)
        if c == "read":
            return wiki.read(args.ids, budget=args.budget, session=args.context, current=args.current,
                             target_version=args.version, historical=args.historical)
        if c == "reindex":
            return wiki.reindex()
        raise ValueError("Unknown command")
    finally:
        ws.db.close()


def main(argv=None):
    # Windows pipes can default to cp1252 even when the terminal displays Chinese.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (ValueError, PermissionError, media.PlatformError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    compact = args.command in ("pack-text", "read-pack", "collect-topic", "learn-topic") and not args.details and not getattr(args, "include_text", False)
    print(json.dumps(result, ensure_ascii=False, **({"separators": (",", ":")} if compact else {"indent": 2})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

