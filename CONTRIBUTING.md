# Contributing

## Development checks

From the repository root:

```sh
uv sync --locked
uv run tox -e py,lint,package
```

The `py` environment builds and installs a wheel, then runs the regression tests
with the current interpreter. To run selected tests:

```sh
uv run tox -e py -- tests/test_bistra_cli.py
```

Run `uv run tox` for the full configured matrix: Python 3.11 with pytest 8.4.2,
Python 3.11–3.15 with the latest permitted pytest, plus lint and package checks.
Those Python interpreters must be available locally. The GitHub Actions workflow
configures the Python matrix on Linux and an additional macOS test job.

- `uv run tox -e lint` runs Flake8 over `src/` and `tests/`.
- `uv run tox -e package` builds a source distribution and a wheel from it, then
  checks their metadata with `twine check --strict`.
- `uv build` writes a source distribution and wheel to `dist/`.

## Project setup

The packaging scaffold comes from
[pytest-dev/cookiecutter-pytest-plugin](https://github.com/pytest-dev/cookiecutter-pytest-plugin/tree/8f38e95b344f07071c1adada1fe209137c7effab),
at commit `8f38e95b344f07071c1adada1fe209137c7effab` (2024-09-18).
The existing implementation, regression tests, and README were retained.

Deliberate adaptations:

- Keep `bistra` as the distribution, import package, pytest plugin, and CLI name.
- Require Python 3.11+ and `pytest>=8.4.2,<10`; parallel execution supports Linux
  and macOS.
- Keep implementation in `src/bistra/` and tests in `tests/`, with pytest's
  `pytester` helper available for plugin tests.
- Use `pyproject.toml` as the version's source of truth; runtime `__version__`
  reads the installed distribution metadata.
- Keep the README in Markdown and omit a separate documentation site.

## Pending follow-ups

The scaffold includes test, lint, and package-check jobs. GitHub repository
and PyPI project wiring, a release/versioning policy, and automated publishing
are still pending. Formatter enforcement, type checking, and a changelog workflow
also remain follow-ups.
