# Pre-release verification: 0.2.0

Local MCP follow-up: independent dedicated control suite **14 passed, 1
skipped**; full offline app suite **28 passed, 0 failed, 2 skipped**. Both skips
require unavailable Windows symlink privileges. Actual subprocess protocol,
initialization/version negotiation, 11-tool discovery, closed/disabled GUI
denial, scoped inventory/reset/exports, idempotency/concurrent producers,
revocation/restart/heartbeat, error non-disclosure and pause checkpoint passed.
Single-worker command behavior was checked using a synthetic handler.

Rebuilt 0.2.0 console MCP passed isolated initialize/tools/list/get_status
(GUI closed, app_not_running), exit 0/empty stderr. Rebuilt GUI --ui-smoke
passed. No real GUI consent/worker/account or live MCP download was exercised.
The prior one-file authenticated result below applies to 0.1.0, not a new live
acceptance claim for 0.2.0. MCP is not automatically installed into any client.

## Prior engine and live validation

2026-10-15. Independent synthetic review: **14 passed, 0 failed, 1 skipped**.
The skipped Windows reparse-point test required unavailable symlink privileges;
that runtime path remains unverified on this test host.

Verified offline scope: recursive pages and empty directories, incomplete scan
evidence, partial download interruption/restart, cloud hash verification,
existing-copy adoption/no-overwrite conflicts, lock contention, account and
destination binding, read-only API/host restrictions, auth/429 mapping,
retry history, CSV completeness/formula escaping, case collisions and synthetic
Windows DPAPI roundtrip.

An independent probe found that changed cloud versions inherited exhausted
retry budgets. The fix resets only the new version's current retry/evidence
fields, retaining lifetime attempts/history and existing files. Independent
recheck passed; unchanged metadata preserves its current budget.

The freshly packaged Windows EXE passed `--ui-smoke --state` in a new isolated
directory: exit 0, only an empty queue database created, no network/cloud access.
This is startup verification, not visual/DPI acceptance.

**Live follow-up:** the public 0.1.0 EXE successfully downloaded one real file
(4,042,624 bytes) through authenticated REST. Independent local evidence review
confirmed cloud SHA256, MD5 and recorded local SHA256 agreement, one completed
attempt and no error. Credentials were entered by the human and never read by
the reviewers. No private filename, account ID or payload is published.

**Not validated:** live full-tree pagination/backup, HTTP 410 resolution across
accounts/files, any complete real Disk backup, or media playability. Users obtain
their own read-only OAuth credentials through Yandex and enter them locally.
An error, exhausted retries, or partial inventory must not be treated as proof
of permanent file loss or backup completeness.

The release is an explicitly experimental prerelease. No credentials, personal
files, recovery inventories, private project history, or live-account logs are
included. The original application/tests are MIT; bundled runtime licenses
remain separate and are included in the portable archive.
