# Security Policy

## Supported Versions

Security fixes are applied to the latest release on the default branch.

## Reporting a Vulnerability

Please do not open a public issue for a suspected vulnerability or include private notes, API keys, tokens, local paths, or logs in an issue.

Use GitHub Private Vulnerability Reporting for this repository. Include:

- A concise description of the issue
- Reproduction steps using fictional data
- The affected version or commit
- Expected and observed behavior
- Any suggested mitigation

Remove secrets and personal information from screenshots, traces, and attachments. You should receive an initial response within seven days.

## Security Boundaries

The local HTTP service is intended for loopback access only. Cloud model use sends selected note content to the configured provider. Users are responsible for reviewing provider privacy terms, retention policies, access controls, and charges before enabling cloud processing.
