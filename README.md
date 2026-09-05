# tbmail

`tbmail` reads email already downloaded by Thunderbird and delegates explicit write operations to a small Thunderbird Experiment. It discovers the active profile, maps configured aliases to IMAP accounts, and returns JSON for people or agents.

Read commands remain local and never launch Thunderbird. `sync`, `mark-read`, and `mark-unread` launch Thunderbird headlessly when needed so Thunderbird remains responsible for authentication, IMAP, and its local mail store.

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

Run the project through `uv`:

```console
uv run tbmail accounts
uv run tbmail status --account all
uv run tbmail status -a personal
uv run tbmail status --account-raw me@example.com
uv run tbmail search -a personal --unread
uv run tbmail search -a personal --subject "invoice" --count
uv run tbmail search -a personal --unread --limit 100 --offset 200
uv run tbmail search -a work --from example@sender.com
uv run tbmail show MESSAGE_ID
uv run tbmail mark-read MESSAGE_ID
uv run tbmail mark-unread MESSAGE_ID
uv run tbmail sync -a personal
uv run tbmail sync --timeout 1800
```

The CLI respects `XDG_CONFIG_HOME`. Use `--config` before the subcommand to load another file:

`--account` and `--account-raw` are mutually exclusive. Both flags can be repeated. If neither is given, `status`, `search`, and `sync` use all configured accounts. Search returns every match unless `--limit` is set. `--offset` skips that many results in deterministic search order. Use `search --count` to return the number of exact matches without message summaries; it cannot be combined with `--limit` or `--offset`.

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

`status`, `search`, and `show` always return local data. If a relevant account has never completed `tbmail sync`, or the last successful sync is more than five minutes old, the command also writes a JSON warning to stderr and still exits successfully. `accounts` does not warn.

Write failures use nonzero exit status and JSON on stderr. Timeout errors include the request ID and diagnostic paths. Managed Thunderbird output is written to `$XDG_STATE_HOME/tbmail/thunderbird.log`, defaulting to `~/.local/state/tbmail/thunderbird.log`; repeated headless GTK warnings stay in that process log rather than entering CLI output. Bridge events are written to `<profile>/tbmail-ipc/bridge.log`.

The profile-local bridge queue uses private file permissions and atomic request/response writes. A headless Thunderbird process stays available for five minutes after a write, then exits cleanly. The add-on never applies that timer to a normal GUI Thunderbird process.
