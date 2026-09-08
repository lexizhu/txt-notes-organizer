# Contributing

Thank you for improving My Notes Tool.

Participation in this project is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Before You Start

- Search existing issues before opening a new one.
- Use fictional data in bug reports, tests, screenshots, and pull requests.
- Never commit notes, logs, API keys, tokens, certificate files, or machine-specific paths.
- Keep the Python runtime dependency-free unless a change has a strong, documented reason.

## Development Setup

Requirements:

- macOS
- Python 3.10 or later
- Ollama for local model integration testing

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

Most tests use fake model transports and do not require network access. Tests that use a local model should be clearly identified and must use fictional prompts.

## Pull Requests

1. Keep changes focused.
2. Add or update tests for behavior changes.
3. Preserve local-first defaults and offline review support.
4. Explain privacy, migration, and compatibility implications.
5. Confirm the repository contains no private data or machine-specific paths.
6. Run the full test suite before submitting.

By contributing, you agree that your contribution is licensed under the MIT License.
