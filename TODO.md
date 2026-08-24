# TODO

- Add an executable `~/Scripts/tbmail` wrapper that runs this project through
  `uv`.
- Add a small agent skill that documents the CLI and tells agents not to parse
  or modify Thunderbird files directly.
- Choose an IMAP or Thunderbird MailExtension backend before implementing
  `mark-read`, `mark-unread`, or `sync`.
- Consider body-text indexing if header-only search is too limited. Keep body
  content out of the cache unless there is a clear need for it.
