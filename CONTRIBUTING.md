# Contributing

## Development

From the repository root:

```sh
uv sync --locked
uv run pre-commit install
uv run tox -e py,lint,type,package
```

Pre-commit runs Ruff fixes and formatting, then tox for types and the full test
suite on every commit. Tests use the installed wheel on your current Python.
To check all tracked files, run `uv run pre-commit run --all-files`. Review and
stage any fixes, then commit again.

`py` tests the installed wheel with the current interpreter and latest allowed pytest.
`lint` checks Ruff rules and formatting, `type` checks `src/` with ty targeting
Python 3.11, and `package` builds source and wheel distributions and checks their
metadata with Twine.

To run selected tests:

```sh
uv run tox -e py -- tests/test_bistra_cli.py
```

Run `uv run tox` for the full matrix: Python 3.11 with pytest 8.4.2,
Python 3.11–3.15 with the latest permitted pytest, plus the other checks above.
These interpreters must be installed locally. GitHub Actions runs this test
matrix on Linux and also tests Python 3.15 on macOS.

To apply lint fixes and format the code:

```sh
uv run ruff check --fix src tests
uv run ruff format src tests
```

Run `uv build` to write a source distribution and wheel to `dist/`.

The local environment uses Python 3.13 and dependencies locked in `uv.lock`.
Tox resolves dependencies separately; its environments do not use the uv lock.
Tool configuration and dependency groups live in `pyproject.toml`.

## Project notes

Code lives in `src/bistra/` and tests in `tests/`. Set the package version in
`pyproject.toml`; `bistra.__version__` reads the installed package metadata.

PyPI setup, a release/versioning policy, automated publishing, and a changelog
workflow are still pending.
