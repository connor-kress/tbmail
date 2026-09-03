# tbmail

`tbmail` is a read-only command-line interface for email already downloaded by Thunderbird. It discovers the active profile, maps configured aliases to IMAP accounts, and returns JSON for use by people or agents.

It does not connect to mail servers or modify Thunderbird data.

## Setup

Create `~/.config/tbmail/config.toml` with aliases for the accounts in your Thunderbird profile:

```toml
[accounts]
personal = "me@example.com"
work = "me@work.example"
```

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
```

The CLI respects `XDG_CONFIG_HOME`. Use `--config` before the subcommand to load another file:

`--account` and `--account-raw` are mutually exclusive. Both flags can be repeated. If neither is given, `status` and `search` use all configured accounts. Search returns every match unless `--limit` is set. `--offset` skips that many results in the deterministic search order, so `--limit` and `--offset` can split a search into index-based batches. Use `search --count` to return the number of exact matches without message summaries. It cannot be combined with `--limit` or `--offset`.

```console
uv run tbmail --config /path/to/config.toml accounts
```

## Data sources

- Account and folder locations come from `profiles.ini` and `prefs.js`.
- `status` uses Thunderbird's `folderCache.json` values to report folder and server totals without building the message index.
- `search` builds a metadata-only index in `~/.cache/tbmail/index.sqlite3`. The first search scans the selected mbox; later searches reuse the index until the mbox changes. Read state is overlaid from Thunderbird's global message index because IMAP mbox status headers are not kept current.
- `show` seeks directly to the downloaded message and decodes its body. It does not store message bodies or attachment contents in the tbmail index.

Search currently covers subject, sender, and recipient headers. It does not search message bodies.
