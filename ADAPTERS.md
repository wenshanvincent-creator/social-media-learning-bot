# External collector adapters

Upstream source is not vendored. Configure each tool separately, follow its license and obtain explicit run/platform permission before its browser/profile is accessed.

## RedCrack

External project: [CyberMemoir/RedCrack](https://github.com/CyberMemoir/RedCrack). Its upstream proprietary license is separate from this package's MIT license; this repository does not grant rights to upstream code.

```sh
sociallearn --root workspace redcrack-search --run RUN_ID "QUERY" --repo ABS_REPO --python ABS_PYTHON --commit EXPECTED_COMMIT --limit 2
sociallearn --root workspace import-redcrack --run RUN_ID ABS_EXPORT_FOLDER
sociallearn --root workspace import-redcrack-media --run RUN_ID ABS_NOTE_FOLDER
```

The wrapper checks the checkout and completed export/download receipts. Signed URLs are excluded from wiki records. Canonical post text and its video child channel remain one source, not independent corroboration. Imports check asset identity, bounds, size and hashes. Unknown or partial outcomes remain explicit.

## Douyin Studio

External project: [CyberMemoir/douyin-studio](https://github.com/CyberMemoir/douyin-studio). Configure the separate local server and its private data/profile directories. This adapter only allows a local HTTP origin and preserves upstream task IDs and pause behavior.

Use `studio-search --help`, `studio-captions --help` and `studio-video --help` for resolve/download receipt inputs. Page `<track>` captions cover only tracks actually exposed by that page; absent tracks do not prove that captions are unavailable everywhere. Failed or blocked tracks must not silently trigger ASR. `transcribe-local` explicitly transcribes archived media and preserves existing captions.

Synthetic/local HTTP integration tests cover the wrappers. They do not establish real-account acceptance or cross-version upstream compatibility. Pin and check the upstream revision you use; a repository update can change its CLI/API contract.
