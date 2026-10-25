# Working on this project

- Keep collection deterministic. Do not add an LLM caller or launch another model CLI as part of collection.
- Preserve the transcript-first workflow. Read available captions before ASR; use a few targeted OCR images only for essential missing screen text. No routine dense frame sampling or demo rendering.
- Before any cookie/profile access, require explicit permission for the current run and platform. Restricted platforms stay paused until the person reports recovery. Do not switch adapters to bypass a restriction.
- Keep credentials, browser profiles, raw transcripts, private knowledge and runtime receipts out of Git. Use synthetic fixtures in tests.
- Search wiki cards first, read selected notes second, and expand exact source segments only when necessary. Do not confuse disk caching with context-token savings.
- Preserve exact source identities, citation boundaries, conditions, contradictory examples and uncertainty. Filter scores do not prove depth, originality or correctness.
- Keep source freshness separate from publication/edit time. Current model claims require dated official evidence and an exact model ID.
- Make changes in the module responsible for them; avoid unrelated refactoring. Run tests appropriate to the risk. Consent, source identity, retrieval or freshness changes require the full test suite.
- Report local checks, live account acceptance and visual inspection separately. Do not claim execution or observation without evidence.
