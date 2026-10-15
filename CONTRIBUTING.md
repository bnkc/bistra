# Contributing

Bug reports, fixes, and ideas are welcome. Include a small example when reporting
a bug, and open an issue before starting a larger change.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run --locked pre-commit install
```

Run all checks:

```sh
uv lock --check
uv run --locked tox -e py,lint,type,package
```

Pre-commit runs Ruff, ty, and the full test suite on every commit.

## Builds

Build the wheel and source archive in `dist/`:

```sh
uv build
```

## Releases

From `main`, bump the version with `uv version --bump alpha` (or
`uv version VERSION`). Update `CHANGELOG.md` and run the checks above.
Commit the changes, then tag and push using the version from `pyproject.toml`:

```sh
git tag -a vVERSION -m "Release vVERSION"
git push origin main
git push origin vVERSION
```

A matching tag publishes to PyPI once all checks pass.
