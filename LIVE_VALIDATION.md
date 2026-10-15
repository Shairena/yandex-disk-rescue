# Bounded live validation — 2026-10-15

Public Windows EXE 0.1.0, SHA256
`1e83e95d0b4655fc3098a32ffa6ee7715c10e02d6ce834e3e7687ab77e30aafd`.

A human entered their own read-only OAuth token locally and started a preselected
single-file run in a separate scratch directory. The official REST identity,
exact-file metadata and download route succeeded. One file, **4,042,624 bytes**,
one completed attempt, no error or incomplete payload.

Independent saved-evidence verification hashed the actual downloaded file:
expected cloud SHA256, expected cloud MD5, recorded local SHA256 and size all
matched. No additional network requests or access to credentials were needed.

This confirms one exact-file download for one account at the test time. Live
recursive pagination, complete account coverage, every-file availability,
resolution of other accounts' HTTP 410 errors and media decoding are not implied.
The release remains experimental.

The ZIP and its manifest were built before this live follow-up; their original
`live_api_validated: false` is historical packaging-time metadata. The checked
EXE/ZIP bytes have not been silently replaced. This document records the later
verification. No token, account identifier, personal filename, digest of private
payloads, or payload data is included.
