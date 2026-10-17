# Yandex Disk Rescue

A small Russian-language Windows desktop app for read-only Yandex Disk backup.
MIT licensed. It enumerates selected cloud roots recursively, keeps a durable
SQLite queue, verifies size and a cloud-provided SHA256/MD5 hash, and exports
unresolved files and incomplete folder scans. It never deletes cloud or local
files and never silently overwrites conflicting files.

**Status: pre-release. Independently tested with synthetic data and one real
authenticated file download (4,042,624 bytes, cloud SHA256/MD5 verified).** Full
live recursive backup and every-file availability remain unverified. An HTTP 410 or exhausted retries is not evidence that a
file is permanently lost. This project is not affiliated with Yandex.

## Run

Windows with Python 3.11+ and Tkinter, no third-party Python runtime packages:

```powershell
python main.py
# A separate queue/account/destination:
python main.py --state 'D:\BackupQueue'
```

The packaged `YandexDiskRescue.exe` needs no Python installation. Keep the
package's license/notices. Queue state defaults to `state` beside the script or
executable. Put it on a private local disk, not a synced/shared directory.
One state directory binds permanently to one account and destination; use a
different `--state` directory to change either. Only one worker may use a queue.

## Agent control

The release also includes `YandexDiskRescueMCP.exe`: a local stdio MCP adapter
to the open GUI's existing worker. [MCP.md](MCP.md) provides the exact client
configuration, tool contract and setup. Enter credentials in the GUI and enable
agent control for the approved destination/roots/options; agents never receive
the token or a tool to change that scope. The checkbox remembers exact-profile
approval across restarts; unchecking it revokes that saved permission.
The GUI includes an explicit **Вставить** token button; Shift+Insert also works.

MCP can read scoped queue/progress, request start/scan/pause/resume/stop,
reset retries/rescan, and export reports. Commands are durable and idempotent;
acknowledgement is not backup completion. The server creates no second worker
and exposes no HTTP listener. This package does not auto-install global client
configuration or replace an existing running app.

## Authentication

The app uses the official **read-only REST API**, not browser scraping. A
browser login does not grant this app API access. The user obtains their own
OAuth token and enters it in the masked field; never paste credentials in a
chat, issue, CSV, or command line.

1. Register an application **for API access or debugging** in Yandex OAuth.
2. Request only **read all Disk** and **Disk information** permissions
   (`cloud_api:disk.read`, `cloud_api:disk.info`). Do not request write access.
3. Open this address in your browser, replacing `YOUR_CLIENT_ID` with the
   ClientID from your application's properties:

   ```text
   https://oauth.yandex.ru/authorize?response_type=token&client_id=YOUR_CLIENT_ID
   ```

   Login and consent are performed by you in Yandex's page. For the API/debug
   app type, the redirect is the official `https://oauth.yandex.ru/verification_code`.
   This app does not collect passwords, browser cookies, or Client Secrets.
4. Enter the token. Optional saving uses Windows user-scoped DPAPI; otherwise it
   remains in this process's memory. “Забыть” deletes the app's saved token.

API access may be refused independently of the website. A current rclone issue
reports HTTP 410 with its Yandex backend. Reports of a user-owned OAuth app
working are unverified for your account; this app does not promise a workaround.

## Use

Interval, attempts per cycle, backoff and the queue view filter are saved
automatically in the selected queue's SQLite settings, without starting a
transfer. Valid numeric edits survive restart; invalid/nonfinite/out-of-range
text keeps the last valid saved value. Older queues use defaults (3/3/5).
The MCP checkbox remembers approval for the exact profile/account binding.
It restores into a fresh session only if the profile still matches and the
saved Windows-protected token loads. Uncheck it or use Forget to revoke saved
approval. Legacy queues without saved approval stay off. Approval restoration
does not start a transfer or replay old commands. Destination,
cloud roots, queue progress and optional Windows-protected token retain their
existing persistence rules; no token is saved merely by editing preferences.

**Refreshing after cloud changes:** read [REFRESH.md](REFRESH.md) before upgrading
an existing recovery queue. Version 0.2.1 fixes old deleted/moved folders being
scheduled again from historical inventory. Rows/local copies are preserved;
presence distinguishes observed, unverified and not_current. A failed request
does not establish deletion. Use a coordinated metadata-only refresh first.

- Choose the destination (initial default `I:\YandexBackup\Disk`). Select the
  whole Disk or enter cloud roots, one per line, e.g. `disk:/Photos`.
  Full paths from `disk:/` are retained when selecting a nested root.
- “Только опись” enumerates metadata without writing backup files. It still
  updates the queue. Pagination and empty directories are tracked explicitly.
- “Начать / продолжить” continues the selected roots. Previous inventory and
  history remain in the queue; changing selection never removes old rows.
- Pause/stop is cooperative between requests/chunks; an outstanding network
  request may take up to its 30-second timeout. Restart resumes at durable page
  and file boundaries. A partial file is retained but retried from byte zero;
  HTTP byte-range continuation is not claimed.
- One download at a time. Interval, attempts per cycle and exponential backoff
  are bounded. Retry-After seconds are respected up to one hour. Authentication
  failures stop the run. “Повторить ошибки” starts a new explicit retry cycle
  while retaining attempt history; security exclusions stay excluded.
- “Пересканировать” restarts pagination, retaining all historical observations
  and downloaded files. It does not mirror deletions. A changing cloud tree is
  not an atomic snapshot; errors and changed totals remain visible.

Before downloading, existing files under the destination are indexed read-only
by name/size, then compared with cloud hashes. Earlier backup layouts can be
reused without downloading or moving them. Name/size or browser-event success
alone never proves integrity. Missing cloud hashes remain unresolved. Cloud
hash verification proves byte agreement, not media playability.

Invalid Windows filename characters are escaped, case collisions get stable
suffixes, and reparse points are refused. Active attachments, including `.lnk`
and executables/scripts, are excluded and reported; the app never restores
quarantined files, executes downloads, or changes Defender/browser protections.
Files that cannot be published atomically remain unresolved. Partials are kept
in `.YandexBackupParts` under the destination, without automatic deletion.

## CSV exports

UTF-8 with BOM; spreadsheet-formula prefixes are escaped. Exports contain local
paths/cloud filenames and should be treated as private recovery data.

| File | Contents |
|---|---|
| `inventory.csv` | All observed files/directories, stable cloud path/ID, expected hashes/size, latest queue state |
| `downloaded.csv` | Verified completed/reconciled files, actual path/SHA256 and verification basis |
| `unresolved.csv` | **Every** unresolved file, including never attempted, retries, auth/HTTP/rate/transport/integrity/conflicts/security exclusions |
| `inventory_errors.csv` | Every known folder not fully enumerated, offset/total/error/attempt count |
| `not_current.csv` | Historical entries absent from a completed parent listing; not a deletion verdict |
| `unverified.csv` | Historical entries awaiting rediscovery, not scheduled independently |
| `attempt_history.csv` | Recorded attempts with time, reason/HTTP, retained partial path/bytes |
| `run_status.csv` | Requested roots and latest run/error, including failures before enumeration |

No pending row disappears until verified publication or adoption. A timeout,
`retry_exhausted`, `service_unavailable` or HTTP 410 is an observation, not a
permanent-loss verdict. Directory coverage must be assessed together with
`run_status.csv`; zero enumerated folders does not mean an empty Disk.

## Build and checks

```powershell
python -m py_compile api.py engine.py main.py vault.py
python smoke_check.py
python main.py --ui-smoke --state '.\review_artifacts\ui-state'
python -m venv .buildenv
.\.buildenv\Scripts\python -m pip install pyinstaller==6.22.3
.\.buildenv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name YandexDiskRescue main.py
.\.buildenv\Scripts\python -m PyInstaller --noconfirm --onefile --console --name YandexDiskRescueMCP main.py
.\.buildenv\Scripts\python package_release.py
```

PyInstaller is a build-only dependency; its bootloader exception permits
distribution of the bundled app under MIT. Bundled Python/Tcl/Tk retain their
own licenses; MIT applies to this project's original source code.
Both builds clear PyInstaller caches. Packaging additionally compares the six
bundled application code objects against the final source before making a ZIP;
a stale/mismatched executable is rejected.

## Sources checked 2026-10-15

- [Official Disk REST overview](https://yandex.com/dev/disk/rest/)
- [Official OAuth registration](https://www.yandex.com/dev/id/doc/en/register-api)
- [Official manual token flow](https://www.yandex.ru/dev/id/doc/ru/tokens/debug-token)
- [Metadata/pagination reference](https://yandex.ru/dev/disk-api/doc/ru/reference/meta)
- [Download-link reference](https://yandex.ru/dev/disk-api/doc/ru/reference/content)
- [rclone Yandex documentation](https://rclone.org/yandex/)
- [Current rclone HTTP 410 report](https://github.com/rclone/rclone/issues/10067)

The overview/OAuth/rclone pages were accessible during implementation. The
detailed Yandex reference pages could not be fetched by the research tool;
specific runtime behavior still requires a coordinated authenticated smoke.
