# tbmail

`tbmail` is a read-only command-line interface for email already downloaded by
Thunderbird. It discovers the active profile, maps configured aliases to IMAP
accounts, and returns JSON for use by people or agents.

It does not connect to mail servers or modify Thunderbird data.

## Setup

Create `~/.config/tbmail/config.toml`:

```toml
[accounts]
school = "ckress@ufl.edu"
personal = "ckress04@gmail.com"
personal-old = "con2coool@gmail.com"
```

Run the project with `uv`:

```console
uv run tbmail accounts
uv run tbmail count --account all
uv run tbmail count -a personal
uv run tbmail count --account-raw ckress04@gmail.com
uv run tbmail search -a personal --unread --limit 20
uv run tbmail search -a school --from example@sender.com
uv run tbmail show MESSAGE_ID
```

`--account` and `--account-raw` are mutually exclusive. Both flags can be
repeated. If neither is given, `count` and `search` use all configured accounts.

Global options such as `--config` and `--profile` go before the subcommand:

```console
uv run tbmail --config ./config.toml accounts
```

## Data sources

- Account and folder locations come from `profiles.ini` and `prefs.js`.
- `count` uses Thunderbird's `folderCache.json` values.
- `search` builds a metadata-only index in
  `~/.cache/tbmail/index.sqlite3`. The first search scans the selected mbox;
  later searches reuse the index until the mbox changes. Read state is overlaid
  from Thunderbird's global message index because IMAP mbox status headers are
  not kept current.
- `show` seeks directly to the downloaded message and decodes its body. It does
  not store message bodies or attachment contents in the tbmail index.

Search currently covers subject, sender, and recipient headers. It does not
search message bodies.
