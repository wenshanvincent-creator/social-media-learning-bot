# Architecture

The project has a deterministic collection/retrieval core and an agent-authored knowledge layer. It does not run a model server.

| Layer | Responsibility |
| --- | --- |
| `cli.py` | Structured command interface, topic orchestration and bounded outputs |
| `collection.py`, `screening.py` | Topic queries, candidate deduplication, thresholds, creator/time limits and manifests |
| `media.py`, `auth.py` | Captions/optional Whisper, dedicated profiles, run-scoped consent and access pauses |
| `cleaning.py`, `ocr.py`, `collector_media.py` | Mechanical text cleaning, bounded Windows OCR and verified media imports |
| `textpacks.py`, `citations.py` | Whole passage/window selection, exact deduplication, citation ranges and reference-token budgets |
| `wiki.py`, `freshness.py` | Validated Markdown notes, source hashes, routing, revisions and freshness/version gates |
| `analysis.py` | Optional native-agent file handoffs, reviewed admission and result reuse |
| `adapters.py` | External collector integration without vendored upstream source |

## Topic flow

1. Search bounded candidate positions, deduplicate canonical IDs, inspect metadata.
2. Screen using configurable heuristics. Popularity and platform weights remain preferences, not credibility verdicts.
3. Fetch captions before optional lightweight ASR. OCR only transcript-cued missing prompts/code with installed Windows OCR.
4. Keep raw evidence and full diagnostics local. Return selected text, citations and review markers.
5. Let the existing agent synthesize complete methods, conditions, disagreements, uncertainty and provenance. Validate before writing notes.
6. Reuse through route-card search and selected note reads. Expand original evidence only when needed.

HTTP restrictions and verification challenges pause the platform. Individual transport failures can be recorded and skipped without retry; this does not bypass an access restriction. Metadata/caption extraction can proceed without a downloadable video format, while actual audio downloads still require one.

## Text budgets

Default balanced packing reserves one whole passage per source before neighboring context and preserves omitted-source/context flags. Citation ranges reduce wire overhead without changing the selected text or refilling the freed space. The existing selection budget is conservative because it still accounts for verbose citations.

Optional question windows use lexical relevance and whole neighboring units. They need review and can omit conditions. Incremental refinement may require explicit reviewed citations; their full windows are reserved first, or the tool rejects an insufficient budget. No lexical selector establishes semantic agreement or completeness.

Stored citation arrays and source fingerprints remain exact. Current evidence changes invalidate dependent packs/notes. Optional `o200k_base` counting measures reference text, not complete requests or billing.

## Interfaces

The CLI and agent skill are implemented. A generic MCP adapter, hosted API, UI and recommendation-feed browsing are future work. Native workers are optional and need explicit authorization; parallelism can reduce parent context without reducing total model tokens.
