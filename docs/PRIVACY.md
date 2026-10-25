# Privacy and publication scope

This repository is distributed from an allowlist of original source files, synthetic tests, generic skills and public documentation. Private runtime folders and the development history are not part of the initial Git history.

Excluded material includes:

- Browser profiles, cookies, session exports and account grants.
- Credentials, environment files, personal paths and contact details.
- Raw videos/audio, real transcripts, screenshots and OCR captures.
- Personal wiki contents, databases, caches, runtime receipts and trial logs.
- Vendored external collectors, demo assets and render outputs.

The tests include clearly synthetic tokens/URLs to check redaction and signed-URL handling. These are not live credentials. Cookie names and environment-variable names in the implementation identify protocol fields; they contain no session values.

Keep your runtime `--root` in an ignored or external directory. A custom directory name is not automatically ignored. Never attach a live cookie jar, browser profile or unredacted account log to an issue. Inspect staged files before publishing changes; `.gitignore` is not a secret scanner.

Each platform session needs explicit permission for the current run. Permission records stay in the local workspace. The agent/collector must stop on authentication challenges or platform restrictions rather than trying another adapter to evade them.

The optional Whisper path can download and run its model locally. Windows OCR uses installed system components. No cloud LLM caller or telemetry service is implemented in the collector. Network requests to platforms and external collectors remain part of the requested acquisition workflow.
