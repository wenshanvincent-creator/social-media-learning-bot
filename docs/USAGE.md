# Usage

## Public topic collection

```sh
sociallearn --root workspace learn-topic "typography hierarchy explained" --kind principle --limit 6 --encoding o200k_base
```

Use the exact `RUN_ID`, `SOURCE_ID`, `COLLECTION_ID` and `NOTE_ID` returned by your commands in the following examples. They are placeholders, not included account data.

## Authorized sessions

First ask the person for permission for this task/platform. Only after their explicit approval:

```sh
sociallearn --root workspace init --topic "YOUR_TOPIC"
sociallearn --root workspace consent --run RUN_ID --platform youtube --grant --evidence "Human approved this run's YouTube session use"
sociallearn --root workspace login --run RUN_ID --platform youtube
sociallearn --root workspace learn-topic "YOUR_TOPIC" --run RUN_ID --cookies --encoding o200k_base
sociallearn --root workspace close-run --run RUN_ID
```

The person performs sign-in. If Google rejects the dedicated automated browser, the person may sign in using their normal browser, verify playback and export only that platform's Netscape cookies. Use `import-cookies --run RUN_ID --platform youtube LOCAL_FILE` within the authorized run. Do not export all browser sites, paste cookie values into chat, or commit them.

Browser playback is a recovery report, not proof of programmatic access. `resolve-pause --run RUN_ID --platform youtube --evidence 'Human reported the prior restriction resolved'` is only for an actual human recovery report. It grants no cookie permission and does not restart work. If collection is still restricted, stop.

Closing a run revokes grants and deletes imported session copies. It does not delete the person's original cookie export or log out their dedicated profile. New tasks require new authorization.

## Archive reads and compact evidence

```sh
sociallearn --root workspace source-read SOURCE_ID --offset 0 --limit 10
sociallearn --root workspace pack-text --collection COLLECTION_ID --focus "font hierarchy" --balanced --budget 8000 --encoding o200k_base
sociallearn --root workspace read-pack PACK_ID
```

`body` contains exact selected text and citations; `review` marks excerpts, review status, weak matches and deferred context. Do not load `--details` or `--include-text` into every model request. `source-read SOURCE_ID --segment 's0001..s0020'` expands actual IDs and retains pagination; follow `next_offset` until the needed range is covered. A hole in the source range is an error.

For incremental refinement:

```sh
sociallearn --root workspace pack-text --collection COLLECTION_ID --focus "motion design" --question "attention hierarchy" --question "timing spacing" --require-citations required.json --budget 5500 --encoding o200k_base
```

`required.json` is an array of exact previously reviewed text citations, for example `[{"source_id":"SOURCE_ID","segment_ids":["s0001"]}]`. Replace the example with actual selected source/segment IDs. The tool reserves their neighboring windows first and refuses to discard them to meet an insufficient budget. This supports incremental review; it is not a guaranteed first-learning compressor.

## Wiki admission and retrieval

Read [the knowledge schema](../skills/social-media-learning/references/knowledge.md) before producing a candidate JSON. Use `source_described` or `agent_synthesis` accurately; exact citations and source hashes are checked by `put`. Revisions require the current hash.

```sh
sociallearn --root workspace put candidate.json
sociallearn --root workspace find "attention"
sociallearn --root workspace read NOTE_ID --budget 3000
```

Use `--current` for a current/latest task and follow freshness deferrals. A stale or changed source cannot be made current simply by editing the note.
