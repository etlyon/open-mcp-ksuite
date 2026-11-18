# Security review

This page records the security evidence for the application source in commit
`96d5266cd6c92e49b4a8d33a6702e762c0b239af`, reviewed on 14 November 2026.
It is evidence, not a certification of every operator deployment.

## Result

Two independent source reviews and a final independent low-risk recheck found
no open critical, high or medium issue in the reviewed source. The final
low-risk change adds the same pre-authentication rate limit to revocation as to
token exchange.

Automated evidence on that exact source:

- Offline suite: **67/67 passed**; three configured live-service tests skipped.
- `pip-audit` against the exact hash-locked dependencies: **No known vulnerabilities**.
- Semgrep Python and secrets rules: **0 findings**.
- gitleaks across the complete three-commit history: **0 leaks**.
- The dependency lock contains hashes; the container installs with
  `--require-hashes --no-deps` and runs as uid 10001.

## Boundaries checked

- OAuth 2.1 authorization, dynamic client metadata and redirect addresses are
  validated; Infomaniak tokens are never passed through to an assistant.
- The connector calls only the fixed Infomaniak API/Mail origins; account and
  recipient-domain policies are checked again on each relevant operation.
- Read/write/send scopes, per-account policy, request bounds, timeouts and rate
  limits are enforced by the connector, not by assistant instructions.
- Tokens are encrypted at rest with a connection key wrapped by the person's
  live authorization. Logs, receipts and error pages do not contain tokens or
  message text.
- PDF, office and OpenDocument extraction runs in a bounded child process with
  input, expansion, output, CPU, memory and time limits.
- Sending, invitations and destructive calendar operations retain explicit
  confirmation; with no recipient-domain policy, mail remains a draft.

## What operators still own

Terminate TLS at a maintained reverse proxy; expose only the public HTTPS
address; keep the container and host patched; back up and protect the state
volume; set mailbox and recipient-domain policies deliberately; and inspect
updates before rebuilding. The review did not certify a particular proxy,
firewall, host or assistant subscription.

Report a suspected problem privately as described in [SECURITY.md](SECURITY.md).
