# Evaluation scope

The publication snapshot passes 87 automated tests covering consent, source identity, access pauses, source changes, freshness, selective retrieval, adapter receipts, media import, text/code boundaries, citation ranges and budget handling. Fixtures are synthetic; media is generated for tests. Account login, platform availability, content quality and visual aesthetics are separate acceptance questions.

A private six-tutorial archive was replayed locally using `tiktoken/o200k_base` and the same compact JSON serialization:

| Variant | Complete compact JSON tokens | Difference |
| --- | ---: | ---: |
| Original selected evidence | 8,494 | Baseline |
| Same text and citations, lossless citation ranges | 6,592 | 22.39% lower |
| Question windows plus previously reviewed required citations | 5,771 | 32.06% lower |

The lossless case preserves every selected passage, exact citation ID and review flag. The incremental case retains all four groups of prior reviewed citation checkpoints and represents six sources. Those anchors are selector inputs, so the latter is not a blind first-learning benchmark. Unguarded smaller question packs missed checkpoints and were not accepted as preserving the prior findings.

The original material count excludes supplemental source reads used during synthesis. Counts also exclude conversation history, model reasoning, outputs and billing. The private source records and learning history are deliberately not distributed, so this live-archive measurement cannot be reproduced from the public repository alone. Public synthetic tests verify the relevant mechanics, not the quoted real-corpus percentages or universal semantic preservation.

Do not present platform weights, views, dates, creator counts or threshold scores as validated credibility, depth or originality classifiers. Do not present caption analysis as visual inspection or executed software instructions.
