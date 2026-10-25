# Contributing

Install the development dependencies, make focused changes and run `python -m pytest -q`. Explain the behavior changed and what was actually verified. Use synthetic fixtures or media you own; do not submit platform transcripts or user account data as fixtures.

Preserve the deterministic collector/agent synthesis boundary. Changes to consent, source identity, freshness and retrieval need regression checks for the concrete risk. Avoid adding local models, an external LLM client, dense frame scanning or platform-access workarounds without an explicit design discussion.

Bug reports should include the command shape, tool versions and redacted error category. Exclude cookies, signed media URLs, private paths, personal notes and raw account logs. Keep upstream licensing separate from this project's license.
