# Test fixtures

Fixtures are committed to a public repository, so they follow a strict redaction policy:

- No real emails, org ids, org names, account ids, usernames, home-directory paths,
  hostnames, tokens, cookies or API keys — not even partially.
- Use obvious placeholders instead: `user@example.invalid`, `org-REDACTED`,
  `acct:000000000000000000000000`. Home-directory paths are written as `{HOME}`, which the
  loading test replaces with a temp-dir path.
- Provider responses captured from real CLIs are reduced to the fields the code reads and
  have every identifier replaced. Usage numbers, plan names, timestamps and version strings
  of the host system are replaced with round, made-up values, and unknown provider keys use
  neutral names such as `extra_bucket_a`.
- Fake CLIs live under `fixtures/bin/` and pick their behaviour from `QR_FAKE_SCENARIO`.
