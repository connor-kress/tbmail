# tbmail agent instructions

Read `LOCAL.md` when it exists. It contains optional machine-specific setup
details and is intentionally excluded from Git. Do not assume those details
apply on another machine.

After changing Python code, run:

```bash
uvx ruff format .
uvx ruff check .
uv run python -W error::ResourceWarning -m unittest discover -v
```
