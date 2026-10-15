# Pre-release verification: 0.2.1

2026-10-15. Independent offline app suite: **38 run, 36 passed, 0 failures,
0 errors, 2 skipped**. Skips require unavailable Windows symlink privileges;
reparse-point rejection is not runtime-confirmed on this test host.

The new membership refresh module passed **10/10** synthetic tests. It covers
complete multipage absence, incomplete/failed/changed/duplicate pages, resumed
offsets, legacy and partially upgraded schemas, concurrent openers, reappearance,
retry cycles, verified local-copy retention, exact-root 404, sibling scoping,
MCP/reporting and directory-to-file conversion without other metadata changes.

Review found a concurrent WAL setup error and a type-only metadata change edge;
both were corrected and rechecked. An initial full run had two failures from
old exact six-CSV expectations. The new contract explicitly adds two reports;
only those two expected filename sets were updated to exactly eight, keeping
the content/privacy assertions. The final full run above passed; initial
negative observations remain in the private review record.

Clean-built GUI smoke returned normally on empty synthetic state. Console MCP
accepted initialize(2025-06-18), reported version 0.2.1 and app_not_running for
the closed-GUI status check, exit 0. No real queue, token or network was used.
Packaging rejects stale binaries by comparing six embedded application modules
with compiled final source. Both final archives passed that owner check.

Historical inventory, observations, attempts, local files and partials remain
preserved. not_current means absence from a completed parent listing, not proof
of deletion. Unknown/unavailable/incomplete branches cannot be called deleted
or a complete backup. Existing queues require the quiescent cutover and backup
procedure in REFRESH.md; no production migration was performed by this review.

## Prior live validation

The public 0.1.0 EXE downloaded one real file (4,042,624 bytes) through
authenticated REST. Independent saved-evidence review confirmed cloud SHA256,
MD5 and recorded local SHA256 agreement with one completed attempt. Human
credentials were entered locally and never read by reviewers.

This is not live acceptance of a complete Disk, all files/accounts, full-tree
pagination, HTTP 410 recovery, media decoding, or the new 0.2.1 refresh migration.
The release remains experimental. Source/tests are MIT; bundled runtime
licenses are separate. No private recovery data or credentials are distributed.
