# Contributing

Bug reports, fixes, and ideas are welcome. A small example helps us reproduce
bugs. For larger changes, open an issue first so we can discuss the approach.

## Development

On Linux or macOS, install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and run these commands from the repository root:

```sh
uv sync --locked
uv run pre-commit install
```

Run all checks:

```sh
uv run tox -e py,lint,type,package
```

Or run a single test file:

```sh
uv run tox -e py -- tests/test_bistra_cli.py
```

Pre-commit runs Ruff, ty, and the full test suite on every commit.
