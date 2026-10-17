# Local MCP agent controls (0.2.3)

Refresh correction: [REFRESH.md](REFRESH.md) explains current versus historical
membership, additive schema migration and exclusive cutover. `rescan` now gates
old descendants until their parents rediscover them. Counts/pending are current
observations; `all`/`downloaded` retain history with a `presence` field. Unknown
branches stay incomplete, and absence is established only by a complete parent
listing. Do not run old binaries on an upgraded active queue.

The stdio server is an adapter to the **already open GUI**, not a second
downloader. It writes session-bound commands into the selected queue's SQLite
database; the GUI applies them to its existing single worker. No HTTP listener,
cloud relay, token argument, cookie extraction or token-returning tool exists.

## Human setup

1. Open `YandexDiskRescue.exe --state C:/BackupState/disk` (or `python main.py
   --state ...`). Use a new state for a different destination/account. Do not
   attach an old 0.1.0 GUI and a new GUI to the same queue simultaneously.
2. Enter the OAuth token in the window. Optional DPAPI saving remains a human
   GUI choice. The MCP process never reads the vault, clipboard or GUI memory.
3. Set destination, cloud roots, interval and retry options. Select **Разрешить
   агенту MCP управление этим профилем**. These scope/options fields then freeze.
4. Configure your MCP client to launch `YandexDiskRescueMCP.exe --mcp --state`
   with that **same absolute state path**. For source use `python main.py --mcp
   --state ...`. Explicit `--state` is mandatory in MCP mode.

Example client configuration (replace sample paths with your actual paths;
no credentials belong here):

```json
{
  "mcpServers": {
    "yandex_disk_rescue": {
      "command": "C:/Tools/YandexDiskRescue/YandexDiskRescueMCP.exe",
      "args": ["--mcp", "--state", "C:/BackupState/disk"]
    }
  }
}
```

This release does not edit Codex/global MCP configuration automatically. Client
reload/tool attachment behavior depends on the client. Do not claim connection
until your client lists the tools and a harmless `get_status` call succeeds.

Unchecking consent cancels unclaimed commands and prevents new agent commands;
it does not stop an already running download. Use the GUI Stop button for that.
The checkbox now saves consent for the exact validated profile and queue account
binding. After restart the GUI opens a new session (old queued commands become
stale; executing commands become ambiguous) and restores permission only if the
profile/binding still match and the saved DPAPI token loads. This does not start
downloads. Existing queues without saved consent remain off until checked once.
Unchecking it, or using Forget while idle, revokes saved permission. A changed
profile/account or unavailable token leaves permission off. If consent preceded
the first account binding, reapprove after that binding is established.

## Tools

| Tool | Arguments/behavior |
|---|---|
| `get_status` | Live phase, credential-present boolean, approved profile and scoped known-inventory progress; no token/account ID |
| `list_entries` | `view`: all/pending/downloaded/failed/folders; `offset` >=0, `limit` 1–200; only approved roots |
| `get_command` | `request_id`; durable command outcome |
| `start` / `scan` | Download or metadata-only run of the exact approved profile; already-active never starts a second worker |
| `pause` / `resume` / `stop` | Control existing worker; resume never silently starts a new run |
| `retry` / `rescan` | Reset unresolved retry cycle or pagination **within approved roots**, preserving history; does not start network calls |
| `export` | Scoped CSVs in a fresh directory under state/agent-exports; agent cannot specify an arbitrary output directory |

Every mutation requires a canonical UUID `request_id`. Reuse the same ID to
recover an uncertain tool reply; a different ID represents a new command.
Maximum 32 outstanding commands. **Queued ACK is not execution.** Poll
`get_command`; `completed` means the GUI applied a command, not that backup
finished successfully. Then inspect status and unresolved/incomplete rows.

`pause_requested` differs from `paused`: network calls can take up to the
configured transport timeout before the worker reaches a checkpoint.
Exports/retry/rescan require an idle worker. Stop retains partials and history.

The GUI publishes a heartbeat. A missing/closed/stale GUI rejects commands;
there is no automatic headless worker. New GUI sessions invalidate unclaimed
old commands; a claimed command interrupted by restart becomes `ambiguous`
and is never automatically replayed. Keep the queue on a private local disk.
SQLite/local-file access shares the user's OS trust boundary; this is not an
isolation sandbox against other same-user processes with arbitrary file access.

The same queue may retain inventory from older roots. Agent lists, retries,
rescans and exports are filtered to the newly approved scope. GUI's own manual
all-inventory view remains available to its human user.

## Check without a live account

MCP initializes/lists tools while GUI is closed; `get_status` then returns
`app_not_running`. An open GUI without opt-in returns `enabled: false`; queue
reads and mutations remain denied. Synthetic review uses isolated state/profile
and a fake API, never real credentials or another active recovery queue.

The local command channel itself does not prove that the cloud is available or
that full recursive backup has completed. Prior single-file live validation of
0.1.0 remains a separate bounded result.

Protocol references checked 2026-10-15:
[stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and [lifecycle/version negotiation](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle).
The server negotiates the common 2024-11-05/2025-03-26/2025-06-18 versions;
protocol output is newline-delimited UTF-8 JSON-RPC only.
