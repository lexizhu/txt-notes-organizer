# Release Checklist

## Code and Tests

- [ ] Run `python3 -m unittest discover -s tests -v`.
- [ ] Confirm the default model remains local Ollama.
- [ ] Confirm the optional cloud UI exposes only the standard OpenAI-compatible protocol.
- [ ] Verify the offline HTML opens without a server.
- [ ] Verify the local service binds only to `127.0.0.1`.

## Privacy

- [ ] Use only fictional TXT notes and generated demo records.
- [ ] Remove all personal notes, capture history, user actions, logs, lock files, cancellation markers, purge transaction directories, and watcher snapshots.
- [ ] Remove API keys, tokens, `.env` files, certificate files, and external model settings.
- [ ] Confirm `~/Library/Application Support/My Notes Tool Public/` is absent from the release artifact; inspect or remove the local directory separately when appropriate.
- [ ] Search for names, email addresses, private domains, organization-specific providers, and machine-specific paths.
- [ ] Inspect generated HTML because it embeds data directly.
- [ ] Inspect screenshots and terminal output before publishing.

## Repository Hygiene

- [ ] Remove `__pycache__`, `.pyc`, `.DS_Store`, coverage output, and temporary files.
- [ ] Check `.gitignore`, `.gitattributes`, LICENSE, SECURITY, and contribution files.
- [ ] Review `docs/SYSTEM_DESIGN.md` for accuracy and public-only architecture boundaries.
- [ ] Review every URL in source, tests, and documentation.
- [ ] Confirm `data/.organize-cancel-*` and `data/.purge-transaction-*` are absent.
- [ ] Create the release archive from this clean directory, never from a real-data working copy.
- [ ] Extract the archive to a temporary directory and repeat the smoke test.

## GitHub Release

- [ ] Update `CHANGELOG.md` and the version number.
- [ ] Tag the commit using Semantic Versioning, for example `v0.1.0`.
- [ ] Attach the verified source archive.
- [ ] Publish release notes with setup steps, known limitations, and privacy boundaries.
