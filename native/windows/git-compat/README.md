# Git for Windows AppContainer compatibility

The patch targets Git for Windows v2.55.0.windows.3 at commit
`52ca1113d651127f89477a8763f86ab20f645e1d`.

Apply `git-v2.55.0.windows.3.patch` to a clean source copy, then enable the
compatibility path with `-DMEWCODE_APPCONTAINER_COMPAT` for the Git build.
With the define omitted, the original standard-handle repair and direct
`GetFinalPathNameByHandleW` calls remain in effect.

The fallback only handles normalized DOS-volume requests after the original
call fails with `ERROR_ACCESS_DENIED` in a process whose token reports
`TokenIsAppContainer`. It attempts DOS-device resolution, then uses the drive
of the process's current DOS working directory only as a candidate when that
query is denied. Every candidate must reopen with the original handle's
volume serial and file index; a guessed drive is never returned without this
identity proof. Other failures retain the original Win32 error. No volume
mapping is cached and no system/ancestor permission is added.

The shared header is also used by a Windows C harness. Test API injection is
available only when `MEWCODE_APPCONTAINER_TEST` is defined; the token and
same-file checks remain part of the shared implementation.

`compat-unit.c` is a separate host-only executable test harness; it is never
linked into Git. `compat-live.c` exercises the real APIs under the host,
AppContainer, and LPAC. The Python diagnostic in `scripts/probe_git_compat.py`
freezes build hashes, stages the PE dependency closure in a new synthetic
fixture, and compares actual outputs and cleanup evidence. Without `--run`
it does not execute a fixture.

The 2026-09-26 build used the official mingw64 SDK at commit
`4485060d9c90a94d907671e4f078bf816f9055c1`. Source, SDK, both builds, build
scripts, and manifests remain under `.tmp/git-auto-compat-20260926/`.
See `docs/auto-native-git-compat-20260926.md` for build provenance, results,
reproduction commands, and limitations. The 2026-09-27 candidate additionally
passes Chinese-path Git status on C: and E: (see
`docs/auto-native-acceptance-20260927.md`); Git write transactions remain
unaccepted. A passing diagnostic never enables production AUTO.
