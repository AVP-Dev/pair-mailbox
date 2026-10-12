## Summary

<!-- What does this PR change, and what problem does it solve? -->

## Protocol & Verification Checklist

- [ ] **Single Writer Invariant**: Maintained (no bypass of sealed patch workflow).
- [ ] **Comment Lint**: Ran `bash scripts/comment-lint.sh` (clean — no comments describing past history).
- [ ] **File Size Ratchet**: Ran `./scripts/file-size-gate.sh` (clean).
- [ ] **Security Scan**: Ran `./scripts/security-scan.sh` (clean).
- [ ] **Test Coverage**: Ran `./scripts/verify-project.sh --jobs 1` (clean, all tests pass).
- [ ] **Doc Parity**: Updated both English and Russian documentation where relevant (`docs/*.md` / `docs/*.ru.md`).

## Verification Output

<!-- Paste relevant output from ./scripts/verify-changed.sh or ./scripts/verify-project.sh -->
