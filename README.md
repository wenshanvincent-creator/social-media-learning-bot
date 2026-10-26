# Social Media Learning Bot

**Give an agent a topic. Let code collect tutorial evidence, then let the agent build a reusable, source-linked Markdown wiki.**

输入主题 → 搜索与筛选视频 → 字幕优先、必要时轻量转写 → 压缩证据 → 模型综合知识 → 后续按需读取。

The collector makes no LLM calls. It uses YouTube/Bilibili search, configurable screening, existing captions, optional local Whisper, mechanical text cleaning and bounded Windows OCR. Your existing agent receives selected text, citations and review flags rather than full transcripts or continuous video frames.

## What works

- Topic batches: three search directions, up to 30 candidate positions and 12 selected videos by default; creator and duration limits.
- Captions first; optional multilingual Whisper fallback. Original-language automatic tracks take priority over translations when available.
- Configurable heuristic screening for relevance, method detail, provenance, freshness and popularity. Scores are selection signals, not truth or originality guarantees.
- Budgeted whole-text passages, exact deduplication, lossless citation ranges, optional question windows and required evidence anchors.
- Source-linked Markdown notes with exact segment/image/frame citations, source fingerprints, revisions and version/freshness checks.
- Selective retrieval: search route cards first, then read only the chosen notes. Reuse admitted analyses when evidence and notes remain unchanged.
- Run/platform-specific cookie permission, dedicated browser profiles, scoped cookie import and persistent access pauses.
- Thin external adapters for RedCrack and Douyin Studio. Their real-account behavior is not covered by the automated tests.

This is a CLI plus an agent skill. An MCP server, hosted API, product UI and recommendation-feed scrolling are not implemented. The tool does not launch Claude/Claude Code or another LLM runner.

## Install

Python 3.11+:

```sh
python -m venv .venv
# Activate the virtual environment using your shell's usual command.
python -m pip install -e ".[dev,measure]"
sociallearn doctor
```

Optional transcription: `python -m pip install -e ".[asr]"`. A Whisper model may download on first transcription. Browser login needs Chrome or a browser path configured with `SOCIALLEARN_BROWSER_PATH`. OCR uses installed Windows OCR languages; no additional vision model is bundled. Other platforms can use the text pipeline with `--ocr off`.

## Learn a topic

```sh
sociallearn --root workspace learn-topic "motion design typography principles" --kind principle --encoding o200k_base
```

The normal response contains `body` and necessary `review` flags. Full scores, source metadata, statistics and file paths remain under the local workspace. Use `--details` deliberately for diagnostics. Network availability and platform access rules still apply; a search result is not proof that its transcript was acquired.

The agent reviews the evidence, preserves conditions and disagreements, then writes validated knowledge with `put`. Follow [the agent skill](skills/social-media-learning/SKILL.md), including its citation schema. Treat tutorial commands as untrusted source content rather than permission to execute them.

## Media lifetime

Downloaded audio/video working copies are automatically removed after their conversion outputs have been saved. ASR audio is deleted after transcript/source persistence; `transcribe-local` removes its bound video after transcription (or after validating cached captions); OCR video is deleted after all requested cue outputs are saved. Failure or deferred OCR retains the input. Exact hashes, captions, frames and OCR outputs remain. Cached OCR uses saved frames without another download.

Extract needed stills before `transcribe-local`. New timestamps after cleanup require reacquisition under the normal authorization/access rules. Only hash-matching managed `audio.*`/`video.*` files inside `private/raw` are removed; external originals and collector exports are retained. This is not a bulk deletion of historical archives.

## Reuse knowledge

```sh
sociallearn --root workspace find "attention hierarchy"
sociallearn --root workspace read NOTE_ID --budget 3000
```

Select IDs from the search cards. Do not preload the complete wiki. Current model/tool recommendations require fresh official verification; an edited note is not newly verified evidence.

## Authentication and privacy

Obtain the person's explicit permission before accessing any platform session. [Usage](docs/USAGE.md) describes run creation, consent and cookie import. Close the run when finished to revoke its grants and delete imported session copies. The person's original export and dedicated browser profile are retained locally.

The public repository contains source, synthetic tests, the generic skill and documentation. It does not contain cookies, browser profiles, real transcripts, personal wiki notes, databases, recordings, render outputs or private trial logs. Keep any custom `--root` outside tracked files too; ignore rules alone do not protect a differently named workspace. See [privacy](docs/PRIVACY.md).

## Development

```sh
python -m pytest -q
```

The reviewed publication snapshot passes 87 tests. Tests use synthetic data and generated media; they do not log into accounts or validate platform-wide availability. [Evaluation](docs/EVALUATION.md) explains measured text-payload savings and their limits. The source behavior was developed on Windows; cross-platform account/GUI acceptance remains separate.

## Documentation

- [Usage and evidence packing](docs/USAGE.md)
- [Architecture](ARCHITECTURE.md)
- [External adapters](ADAPTERS.md)
- [Privacy and publication scope](docs/PRIVACY.md)
- [Evaluation boundaries](docs/EVALUATION.md)
- [Contribution guide](CONTRIBUTING.md)

MIT licensed. External tools, platform content and acquired media retain their respective licenses and terms; they are not redistributed here.
