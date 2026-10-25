import json
from pathlib import Path

import pytest

from sociallearn.cleaning import compact_segments
from sociallearn.store import Workspace, atomic_json, digest
from sociallearn.textpacks import TextPacks
from sociallearn.cli import main
from sociallearn.citations import compact_ids, expand_ids


def save(ws, source_id, paragraphs):
    path = ws.root / "private/raw" / source_id / "segments.json"
    segments = [{"id": f"s{i}", "start": i*10, "end": i*10+9, "text": text} for i, text in enumerate(paragraphs)]
    atomic_json(path, segments)
    source = {"id": source_id, "run_id": "synthetic", "url": "https://example.org/"+source_id,
              "title": source_id, "transcript_origin": "manual", "published_at": "20261024",
              "segments_path": str(path.relative_to(ws.root)), "segments_sha256": digest(path.read_bytes()),
              "content_hash": digest(path.read_bytes())}
    ws.save_source(source)
    return source, segments


@pytest.fixture
def ws(tmp_path):
    value = Workspace(tmp_path)
    yield value
    value.db.close()


def paragraph(prefix):
    return prefix + " A concrete explanation keeps the complete example and its assumptions together. " * 3


def test_focused_whole_windows_keep_conditions_but_skip_unrelated_passages(ws):
    texts = [paragraph("Unrelated astronomy " + str(i)) for i in range(10)]
    texts[4] = paragraph("Prerequisite: use this only for a two-line title and caption.")
    texts[5] = paragraph("Font weight establishes title hierarchy with a visible difference.")
    texts[6] = paragraph("Exception: do not apply these sizes to Chinese formulas; retain 24 versus 48 as distinct examples.")
    save(ws, "alpha", texts)
    packs = TextPacks(ws)
    card = packs.prepare(["alpha"], "font weight", budget=1500)
    body = packs.read(card["id"])["body"]
    assert "Prerequisite" in body and "Exception" in body and "24 versus 48" in body
    assert "Unrelated astronomy 0" not in body
    assert card["metrics"]["returned_units"] <= 1500
    assert card["metrics"]["returned_units"] < card["metrics"]["full_clean_units"]


def test_cross_source_exact_dedup_keeps_both_citations_and_conflicting_values(ws):
    shared = paragraph("Font weight hierarchy demonstration.")
    save(ws, "alpha", [shared, paragraph("Use parameter 24; do not use the other value.")])
    save(ws, "beta", [shared, paragraph("Use parameter 48; do not use the other value.")])
    packs = TextPacks(ws)
    card = packs.prepare(["alpha", "beta"], "font weight", budget=3000)
    payload = packs.read(card["id"])
    assert payload["body"].count(shared) == 1
    assert "alpha:s0" in payload["body"] and "beta:s0" in payload["body"]
    assert "parameter 24" in payload["body"] and "parameter 48" in payload["body"]
    assert card["metrics"]["merged_exact_passages"] == 1


def test_cache_invalidates_on_source_focus_or_budget_change(ws):
    save(ws, "alpha", [paragraph("Font weight hierarchy")])
    packs = TextPacks(ws)
    first = packs.prepare(["alpha"], "font", budget=1000)
    assert packs.prepare(["alpha"], "font", budget=1000)["cached"]
    assert packs.prepare(["alpha"], "weight", budget=1000)["id"] != first["id"]
    assert packs.prepare(["alpha"], "font", budget=900)["id"] != first["id"]
    source = ws.source("alpha")
    ws.save_source({**source, "transcript_origin": "automatic"})
    with pytest.raises(ValueError, match="Source changed"):
        packs.read(first["id"])
    assert packs.prepare(["alpha"], "font", budget=1000)["id"] != first["id"]
    save(ws, "alpha", [paragraph("Changed font weight hierarchy")])
    with pytest.raises(ValueError, match="Source changed"):
        packs.read(first["id"])
    assert not packs.prepare(["alpha"], "font", budget=1000)["cached"]


def test_no_match_and_tiny_budget_do_not_cut_code_or_fabricate_text(ws):
    code = "```python\n    prompt = '{keep [symbols]}'\n" + "    print(prompt)\n" * 100 + "```"
    save(ws, "alpha", [code])
    packs = TextPacks(ws)
    no_match = packs.prepare(["alpha"], "astronomy", budget=1000)
    assert no_match["status"] == "no_match" and not packs.read(no_match["id"])["body"]
    assert packs.read(no_match["id"])["review"] == ["no_match"]
    tiny = packs.prepare(["alpha"], "prompt", budget=1)
    assert tiny["status"] == "budget_deferred" and not packs.read(tiny["id"])["body"]
    assert packs.read(tiny["id"])["review"] == ["budget_deferred"]
    full = packs.prepare(["alpha"], "prompt", budget=5000)
    assert code in packs.read(full["id"])["body"]


def test_archived_web_paragraphs_keep_ids_without_invented_timestamps(ws):
    path = ws.root / "private/raw/web/segments.json"
    atomic_json(path, [{"id": "p0001", "text": paragraph("Frame animation uses current frame.")},
                       {"id": "p0002", "text": paragraph("The duration controls the interpolation boundary.")}])
    ws.save_source({"id": "web", "run_id": "synthetic", "status": "archived", "platform": "web", "title": "Owned web fixture",
                    "segments_path": str(path.relative_to(ws.root)), "segments_sha256": digest(path.read_bytes())})
    card = TextPacks(ws).prepare(["web"], "frame animation", budget=1000)
    packet = json.loads(Path(card["packet_path"]).read_text(encoding="utf-8"))
    cited = [c for p in packet["passages"] for c in p["citations"]]
    assert cited and cited[0]["segment_ids"] == ["p0001"]
    assert all("start" not in c and "end" not in c for c in cited)
    assert "p0001" in TextPacks(ws).read(card["id"])["body"]


def test_fenced_code_spanning_segments_is_whole_or_deferred(ws):
    lines = ["```python", '    prompt = "{keep [symbols]}"'] + [f"    values.append({i})" for i in range(150)] + ["```"]
    save(ws, "code", lines)
    packs = TextPacks(ws)
    small = packs.prepare(["code"], "prompt", budget=300)
    assert small["status"] == "budget_deferred" and not packs.read(small["id"])["body"]
    large = packs.prepare(["code"], "prompt", budget=5000)
    assert "\n".join(lines) in packs.read(large["id"])["body"]


def test_collection_ocr_prompt_is_preserved_and_source_snapshot_is_bound(ws):
    source, segments = save(ws, "alpha", [paragraph("Here is the full prompt shown on screen.")])
    cleaned, _ = compact_segments(segments, "manual")
    packet = ws.root / "state/fixture-text.json"
    prompt = '    Keep {braces}, [brackets], and "quotes" exactly.'
    atomic_json(packet, {"source_snapshot": {k: source.get(k) for k in ("content_hash", "segments_sha256", "video_hash")},
                        "segments": cleaned, "screen_text": {"status": "extracted_needs_review", "items": [{"frame_times": [5], "text": prompt, "needs_review": True}]}})
    collection = "a"*12
    atomic_json(ws.root / "state/collections" / collection / "manifest.json",
                {"selected": [{"id": "alpha", "text_path": str(packet), "sha256": digest(packet.read_bytes())}]})
    packs = TextPacks(ws)
    card = packs.prepare([], "prompt", budget=2000, collection_id=collection)
    body = packs.read(card["id"])["body"]
    assert prompt in body and "frames=5" in body and "needs_review" in body
    assert "alpha:needs_review" in packs.read(card["id"])["review"]
    save(ws, "alpha", [paragraph("Source revision has different prompt details")])
    with pytest.raises(ValueError, match="Collection source changed"):
        packs.read(card["id"])


def test_default_cli_keeps_text_citations_and_review_but_hides_diagnostics(ws, capsys):
    source, _ = save(ws, "alpha", [paragraph("Font hierarchy uses clear contrast.")])
    ws.save_source({**source, "transcript_origin": "local_whisper", "transcript_review_status": "needs_review"})
    args = ["--root", str(ws.root), "pack-text", "--source", "alpha", "--focus", "font", "--budget", "1500"]
    assert main(args) == 0
    stdout = capsys.readouterr().out
    compact = json.loads(stdout)
    assert set(compact) == {"body", "review"}
    assert "alpha:s0" in compact["body"] and "alpha:needs_review" in compact["review"]
    assert stdout == json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n"
    assert str(ws.root) not in stdout
    assert main(args + ["--details"]) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["cached"] and card["metrics"]["returned_units"] > 0
    packet = json.loads(Path(card["packet_path"]).read_text(encoding="utf-8"))
    assert packet["body"] == compact["body"] and "metrics" in packet and "sources" in packet
    assert main(["--root", str(ws.root), "read-pack", card["id"]]) == 0
    assert json.loads(capsys.readouterr().out) == compact
    assert main(["--root", str(ws.root), "read-pack", card["id"], "--details"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["body"] == compact["body"] and full["sources"][0]["review_status"] == "needs_review"
    assert "measurement_scope" in full


def test_topic_balance_represents_each_source_before_loading_more_from_one(ws):
    save(ws, "alpha", [paragraph(f"Font contrast hierarchy typography example {i}.") for i in range(12)])
    save(ws, "beta", [paragraph("Font contrast uses parameter 24 only for short captions.")])
    save(ws, "gamma", [paragraph("Font contrast uses parameter 48 only for long captions.")])
    packs = TextPacks(ws)
    card = packs.prepare(["alpha", "beta", "gamma"], "font contrast", budget=1700, balanced=True)
    body = packs.read(card["id"])["body"]
    assert all(s + ":" in body for s in ("alpha", "beta", "gamma"))
    assert "parameter 24" in body and "parameter 48" in body
    packet = json.loads(Path(card["packet_path"]).read_text(encoding="utf-8"))
    assert packet["metrics"]["represented_sources"] == 3 and not packet["unrepresented_sources"]
    assert packs.prepare(["alpha", "beta", "gamma"], "font contrast", budget=1700)["id"] != card["id"]
    small = packs.prepare(["alpha", "beta", "gamma"], "font contrast", budget=1, balanced=True)
    review = packs.read(small["id"])["review"]
    assert "budget_deferred" in review and "unrepresented_sources:alpha,beta,gamma" in review


def test_topic_reserves_all_sources_before_expanding_large_neighbor_windows(ws):
    ids = ["lesson-" + str(i) for i in range(6)]
    for sid in ids:
        save(ws, sid, [paragraph(sid + " prerequisites"), paragraph(sid + " font contrast method"), paragraph(sid + " exceptions")])
    packs = TextPacks(ws)
    seed_cost = sum(len(paragraph(sid + " font contrast method")) for sid in ids)
    # Enough for six single passages and citations; not six three-passage windows.
    budget = seed_cost // 3 + 250
    card = packs.prepare(ids, "font contrast", budget=budget, balanced=True)
    packet = json.loads(Path(card["packet_path"]).read_text(encoding="utf-8"))
    assert packet["metrics"]["represented_sources"] == 6
    assert not packet["unrepresented_sources"]
    assert packet["context_deferred_sources"]
    assert any(f.startswith("context_deferred:") for f in packs.read(card["id"])["review"])


def test_display_ranges_roundtrip_gaps_padding_duplicates_and_cli_reads(ws, capsys):
    ids = ["s0001", "s0002", "s0003", "s0005", "s0006", "p01", "p02", "p03", "p03"]
    compact = compact_ids(ids)
    assert compact == "s0001..s0003,s0005,s0006,p01..p03,p03"
    assert expand_ids([compact], ids) == ids
    with pytest.raises(ValueError, match="missing"):
        expand_ids(["s0001..s0006"], ids)
    with pytest.raises(ValueError, match="invalid"):
        expand_ids(["s0001..p03"], ids)
    save(ws, "range-source", ["One", "Two", "Three", "Four"])
    assert main(["--root", str(ws.root), "source-read", "range-source", "--segment", "s0..s2", "--limit", "2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in result["segments"]] == ["s0", "s1"]
    assert result["next_offset"] == 2 and result["total_segments"] == 3


def test_question_refinement_keeps_required_opposing_conditions_or_refuses_budget(ws):
    for sid, value in (("alpha", 24), ("beta", 48)):
        save(ws, sid, [paragraph("Font animation typography overview " + str(i)) for i in range(3)] +
             [paragraph(f"Only for short titles: use parameter {value}; the other setting is a different style."),
              paragraph("Exception: long narration requires a different duration.")])
    anchors = [{"source_id": sid, "segment_ids": ["s3", "s4"]} for sid in ("alpha", "beta")]
    packs = TextPacks(ws)
    card = packs.prepare(["alpha", "beta"], "font", budget=5000, questions=["font animation"], required_citations=anchors)
    payload = packs.read(card["id"])
    assert "parameter 24" in payload["body"] and "parameter 48" in payload["body"]
    assert payload["body"].count("long narration") == 1  # exact duplicate text, both citations retained
    assert "question_selection_needs_review" in payload["review"]
    with pytest.raises(ValueError, match="increase budget"):
        packs.prepare(["alpha", "beta"], "font", budget=1, questions=["font"], required_citations=anchors)
    with pytest.raises(ValueError, match="exact segment IDs"):
        packs.prepare(["alpha"], "font", questions=["font"], required_citations=[{"source_id": "alpha", "segment_ids": ["s999"]}])
    with pytest.raises(ValueError, match="exact segment IDs"):
        packs.prepare(["alpha"], "font", questions=["font"], required_citations={"source_id": "alpha", "segment_ids": ["s3"]})
    changed = packs.prepare(["alpha", "beta"], "font", budget=5000, questions=["font animation"], required_citations=anchors[:1])
    assert changed["id"] != card["id"]


def test_question_windows_preserve_fenced_code_and_report_unmatched_question(ws):
    lines = ["```python", 'prompt = "{font [symbols]}"'] + [f"    values.append({i})" for i in range(60)] + ["```"]
    save(ws, "code", lines)
    packs = TextPacks(ws)
    card = packs.prepare(["code"], "font", budget=5000, questions=["font prompt", "unrelated astronomy"])
    payload = packs.read(card["id"])
    assert "\n".join(lines) in payload["body"]
    assert "unmatched_questions:unrelated astronomy" in payload["review"]
    with pytest.raises(ValueError, match="increase budget"):
        packs.prepare(["code"], "font", budget=10, questions=["font"], required_citations=[{"source_id": "code", "segment_ids": ["s1"]}])
