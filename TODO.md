# TODO

- Consider body-text indexing if header-only search is too limited. Keep body content out of the cache unless there is a clear need for it.
- Guarantee that `sync` downloads bodies exceeding Thunderbird's configured maximum offline message size; ESR 140's `downloadAllForOffline()` still applies `limitOfflineMessageSize`.
- Allow `mark-read` and `mark-unread` to update multiple message IDs in one command.
- Add a Thunderbird-backed `send` command.
- Investigate recovery when Thunderbird never reports completion of an active IMAP operation. Managed stop and the fixed safety deadline drain without force-killing, so such an operation can still prevent shutdown.
- Extend coverage for stale-file cleanup, log rotation, and setup rollback.
- Run controlled live checks for mailbox compaction and duplicate RFC `Message-ID` rejection.
- Verify that setup can persistently re-enable an add-on previously disabled in Thunderbird and strengthen installed-build verification beyond the manifest version.
- Revalidate the Experiment's internal Thunderbird APIs when moving beyond ESR `140.*`.
