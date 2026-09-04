# tbmail agent instructions

Read `LOCAL.md` when it exists. It contains optional machine-specific setup details and is intentionally excluded from Git. Do not assume those details apply on another machine.

When changing an API or CLI, update every affected document, example, and locally configured integration described by `LOCAL.md`, including agent skills and their descriptions.

Prefer typed dataclasses for fixed data structures. Use dictionaries only for genuinely dynamic mappings or serialization boundaries. Keep the design simple; do not add abstractions solely for typing.

After changing Python code, run:

```bash
uvx ruff format .
uvx ruff check .
uv run python -W error::ResourceWarning -m unittest discover -v
```
