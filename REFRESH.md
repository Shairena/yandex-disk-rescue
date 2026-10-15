# Refresh current membership without erasing recovery history

0.2.1 corrects an older rescan defect: retained folder rows could be scheduled
independently of whether a fresh parent listing still contained them.

Historical metadata, attempts, observations, verified records, local files and
partials are retained. Cloud membership is a separate `presence` field:

| Presence | Meaning |
|---|---|
| `observed` | Positively observed in the current/resumed inventory; eligible for normal scoped queue rules |
| `unverified` | Awaiting rediscovery after refresh/upgrade; not scheduled independently |
| `not_current` | Absent from a successfully completed parent's listing (or ancestor is now a file); **not proof of permanent deletion** |

Rescan gates historical descendants behind newly discovered parent membership.
Only complete pages satisfying total/offset/duplicate checks establish absence.
An auth failure, 404/410, timeout, changed total or interrupted scan does not
prove absence. Unknown descendants remain visible and incomplete. A positive
reappearance activates the branch again; unchanged verified local copies are
retained and revalidated by the normal engine.

`pending`/progress counts refer to observed current entries. `all` and
`downloaded` retain history and include presence; they are not counts of current
cloud contents. MCP `membership_counts` separates the three states. Exports now
include `not_current.csv` and `unverified.csv`, and all eight reports keep their
scope. Unverified folders count as incomplete even if their historical status
was complete. No local/cloud deletion or mirror operation is performed.

## Coordinated upgrade of an existing queue

1. Stop the worker and wait for idle; close the old GUI and stop any MCP client
   processes attached to that queue. Do not run old/new versions concurrently.
2. With the queue quiescent, make a consistent SQLite backup of the queue using
   SQLite's backup API (or preserve the complete state directory, including WAL
   files). Do not copy only the main DB while transactions/WAL writes are active.
   Keep credential files private; no credential transfer through agents is needed.
3. Extract the new package separately. Open the SAME state path only after the
   old processes are closed. Additive migration introduces membership columns;
   legacy rows initially become unverified, not deleted. Original payloads and
   verification metadata are retained. Do not change the bound destination/account.
4. Have the human check roots/destination, provide credentials locally if needed,
   and approve the MCP profile again. Use `rescan` then **metadata-only `scan`**
   to inspect current membership before starting payload downloads. Inspect
   incomplete/unverified folders as well as not_current records.
5. Coordinate the next download run with the recovery owner. A complete parent
   listing is not an atomic snapshot of a changing account.

Old 0.2.0 binaries ignore membership fields and may schedule historical branches
again. Do not use them on the upgraded active queue. Rollback, if needed, uses
the consistent pre-upgrade state backup with the old version while all new
processes are closed; local downloaded payloads still must not be deleted.
