# tbmail

`tbmail` reads email already downloaded by Thunderbird and delegates explicit write operations to a small Thunderbird Experiment. It discovers the active profile, maps configured aliases to IMAP accounts, and returns JSON for people or agents.

Read commands remain local and never launch Thunderbird. Use `start` before server-backed work and `stop` when finished. Neither `sync` nor read-state changes start Thunderbird automatically. Thunderbird remains responsible for authentication, IMAP, and its local mail store.

## Setup

Create `~/.config/tbmail/config.toml` with aliases for the accounts in your Thunderbird profile:

```toml
[accounts]
personal = "me@example.com"
work = "me@work.example"

[thunderbird]
command = ["thunderbird", "--headless"]
```

The shown native Thunderbird command is the default and may be omitted. Set an argument array for another installation method. For example, the local Flatpak configuration uses:

```toml
[thunderbird]
command = ["flatpak", "run", "org.mozilla.thunderbird_esr", "--headless"]
```

Install and verify the Thunderbird bridge before using write commands:

```console
./scripts/setup-thunderbird-addon
```

The installer supports Thunderbird ESR `140.*` through the `org.mozilla.thunderbird_esr` Flatpak. Thunderbird must be closed while the XPI is installed. The script packages the repository add-on under ignored `tmp/`, backs up an existing XPI, launches Thunderbird headlessly, checks the bridge heartbeat, and requests a clean shutdown. Use `--profile PATH` only when the automatic profile selection is wrong.

If verification fails, setup attempts managed cleanup without killing Thunderbird. It rolls back an unchanged XPI only after profile release, preserving external replacements and preferences saved during verification. The two sideload preference changes remain in place.

Run the project through `uv`:

```console
uv run tbmail accounts
uv run tbmail sync --check -a personal
uv run tbmail start
uv run tbmail sync -a personal
uv run tbmail status -a personal
uv run tbmail search -a personal --subject "invoice" --count
uv run tbmail search -a personal --subject "invoice"
uv run tbmail show MESSAGE_ID
uv run tbmail mark-read MESSAGE_ID
uv run tbmail stop
```

These commands illustrate the interface, not an instruction to change arbitrary messages. Only apply read-state changes the user authorized. For an explicit request for the latest mail, run `start`, `sync --force` for the selected accounts, the requested reads or writes, and `stop`. Use `sync --timeout 1800` for a large initial download.

The CLI respects `XDG_CONFIG_HOME`. Use `--config` before the subcommand to load another file:

`--account` and `--account-raw` are mutually exclusive. Both flags can be repeated. If neither is given, `status`, `search`, and `sync` use all configured accounts. `status` and `search` cover all downloaded folders by default; use `--folder inbox` or another folder only for an explicit folder request. Search returns every match unless `--limit` is set. `--offset` skips that many results in deterministic search order. Use `search --count` to return the number of exact matches without message summaries; it cannot be combined with `--limit` or `--offset`.

```console
uv run tbmail --config /path/to/config.toml accounts
```

## Data sources

- Account and folder locations come from `profiles.ini` and `prefs.js`.
- `status` uses Thunderbird's `folderCache.json` values to report folder and server totals without building the message index.
- `search` builds a metadata-only index in `~/.cache/tbmail/index.sqlite3`. The first search scans the selected mbox; later searches reuse the index until the mbox changes. Read state is overlaid from Thunderbird's global message index because IMAP mbox status headers are not kept current.
- `show` seeks directly to the downloaded message and decodes its body. It does not store message bodies or attachment contents in the tbmail index.
- `mark-read` and `mark-unread` identify a Thunderbird message by account, folder, mbox offset, and RFC `Message-ID`. A unique folder-local Message-ID fallback handles mailbox compaction without changing every duplicate.
- `sync` refreshes every selectable folder twice, then downloads every missing body for offline use. The default total timeout is 300 seconds; use `--timeout SECONDS` for a large initial download.

Search currently covers subject, sender, and recipient headers. It does not search message bodies.

## Freshness and errors

`sync --check [-a ACCOUNT]` reads only local freshness metadata. Its JSON contains `needs_sync` and per-account results. Fresh and stale results both exit zero; configuration, account-selection, and I/O errors exit nonzero. A successful sync less than five minutes old is fresh. Missing, invalid, timezone-less, future, or expired timestamps are stale.

Ordinary `sync` skips fresh accounts and reports that clearly in JSON. Mixed selections synchronize only stale accounts and report the others as `skipped_fresh`. `--force` synchronizes every selected account. `--check` and `--force` are mutually exclusive. Only successful account synchronization advances freshness, not a start, a local read, or a read-state write.

`status`, `search`, and `show` always return local data. Stale accounts also produce JSON warnings on stderr without failing the read. `accounts` does not warn.

Write failures use nonzero exit status and JSON on stderr. Timeout errors include the request ID and diagnostic paths. Managed Thunderbird output is written to `$XDG_STATE_HOME/tbmail/thunderbird.log`, defaulting to `~/.local/state/tbmail/thunderbird.log`; repeated headless GTK warnings stay in that process log rather than entering CLI output. Bridge events are written to `<profile>/tbmail-ipc/bridge.log`.

An absent bridge produces an actionable JSON error with status `absent_instance`: `Run 'tbmail start' and retry.` A fresh no-op sync and `sync --check` need no running instance.

## Lifecycle

`start` leaves an existing GUI, owned headless, or unknown instance unchanged. It claims ownership only when the add-on returns the randomly generated startup token passed to the launched headless process. Flatpak receives that token through explicit `--env` arguments, not assumptions about launcher PIDs or sandbox PID namespaces.

`stop` closes only an owned headless instance. Repeated stops succeed without launching anything; GUI and unowned instances return `not_owned` and remain running. Stop signals the add-on before waiting for the write lock, so an active sync stops scheduling new folder work, waits for active operations to finish, and quits normally. The CLI then waits for Thunderbird's kernel profile lock to release. It never force-kills a process. A bounded timeout returns an error and retains ownership and the drain signal for a later `stop` retry.

Managed instances have a fixed 30-minute safety deadline established at startup. Requests never extend it. Expiry starts the same drain process, so an IMAP operation that never completes can delay shutdown beyond the deadline. This is a safety fallback, not a substitute for explicit cleanup. GUI and unowned headless instances have no tbmail shutdown timer.

Conflicting lifecycle and write operations use a per-profile advisory lock under `$XDG_STATE_HOME/tbmail`, defaulting to `~/.local/state/tbmail`. Lock metadata records PID, Linux process start time, and boot ID. The kernel lock is authoritative; stale metadata is replaced only after acquiring it, and the live lock inode is never unlinked. Local reads and `sync --check` do not acquire that lock. The profile-local bridge queue uses private permissions and atomic JSON writes.

## Agent workflow

Reassess freshness on **each user message**, including follow-ups and confirmations. Do not reuse the previous turn's freshness decision.

- Ordinary reads: run `sync --check` for the relevant accounts. If fresh, use local reads only and do not call `stop`. If stale, run `start`, `sync`, the local reads, then `stop`.
- Authorized writes: run `start`, `sync`, the authorized write, then `stop`.
- Explicit latest-mail requests: run `start`, `sync --force`, the requested reads or authorized writes, then `stop`.
- If a command reports `absent_instance`, run `start` and retry that command once. Do not create a retry loop.
- After entering a server-backed workflow, always attempt `stop` before responding or waiting for the user, including after errors. Treat cleanup failures as errors to report, not as successful shutdown.

Use `status` and matching `search --count` before review. Do not add a folder filter unless the user requested one. A shell cleanup trap can protect noninteractive sequences; agents must also clean up before asking for confirmation.
