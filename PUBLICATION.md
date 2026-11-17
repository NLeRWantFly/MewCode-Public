# Public snapshot — 2026-11-17

This repository contains a source snapshot with independent Git history. Ongoing development remains in the original development repository. Public updates should be exported and checked again, rather than merging private Git history.

## Privacy changes

- Excluded local Provider/account configuration, credentials, databases, sessions, generated outputs, screenshots, and internal task reports.
- Replaced private Provider identifiers/endpoints, personal names, and local workspace paths with example values.
- Excluded captured runtime replay data. The two tests requiring this private capture skip when it is absent; synthetic replay tests remain included.
- Replaced the embedded TLS test private key with certificates generated in a temporary directory by OpenSSL.
- Used a neutral Git commit identity for this snapshot.

Provider integrations and standard public service endpoints remain part of the application. Users must supply their own configuration and credentials. Third-party copyright and dependency attribution are preserved.

## Validation

- Gitleaks 8.30.1 filesystem scan: no findings after redaction.
- Exact matching against locally configured private values: no remaining matches.
- All included Python source files parse successfully.
- Focused workflow, Provider, network transport and replay tests: 75 passed, 2 skipped for the excluded private replay capture.
- Provider identity, bootstrap, BOM handling, registry and switching regressions after redaction: 107 passed.
- Backend undefined-name checks passed.

The existing startup regression script has four failing parameterizations in `test_web_segments_resume_latest_conversation_and_delivery_without_provider_calls`. The same failure was reproduced in the original development tree: its fake `_build_role_agent` still requires a `_verdict` argument that the current caller does not pass. This historical test mismatch is preserved and is not a secret-redaction regression. The startup CI job may therefore remain red until that legacy fixture is updated.
