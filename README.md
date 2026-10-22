<p align="center">
  <img src="https://raw.githubusercontent.com/bnkc/bistra/main/.github/assets/bistra.svg" alt="Bistra" width="100%">
</p>

[![PyPI](https://img.shields.io/pypi/v/bistra.svg)](https://pypi.org/project/bistra/)
[![CI](https://github.com/bnkc/bistra/actions/workflows/main.yml/badge.svg?branch=main)](https://github.com/bnkc/bistra/actions/workflows/main.yml)

An extremely fast, highly opinionated test scheduler for pytest.

The name comes from the Russian **быстро** (*bystro*), meaning “fast” or “quickly.”

Bistra is built for real test suites with uneven runtimes. It uses timing history
to balance work across workers and reduce the wait for the last worker.

Run your tests with `bistra`. Keep your pytest fixtures, plugins, and options.

## Benchmarks

Benchmarked on nine open-source projects, including pytest, Polars, SQLAlchemy,
and Textual. Charts show one project.

- **Machine:** Apple M4 Max, macOS.
- **Workers:** 12 for Bistra and xdist; 1 for pytest.
- **Timing history:** none for cold runs, reused for warm runs.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/bnkc/bistra/main/.github/assets/benchmark-cold-dark.svg">
    <img src="https://raw.githubusercontent.com/bnkc/bistra/main/.github/assets/benchmark-cold-light.svg" alt="Selected urllib3 cold run: Bistra 13.72 seconds, pytest-xdist 23.52 seconds, pytest 83.00 seconds." width="720">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/bnkc/bistra/main/.github/assets/benchmark-warm-dark.svg">
    <img src="https://raw.githubusercontent.com/bnkc/bistra/main/.github/assets/benchmark-warm-light.svg" alt="Selected urllib3 warm run: Bistra 11.62 seconds, pytest-xdist 23.52 seconds, pytest 83.00 seconds." width="720">
  </picture>
</p>

## Installation

```sh
uv add --dev bistra
uv run bistra
```

Use your usual pytest options with `bistra`.

Workers default to the available CPU count. Override with `-j` or `--workers`:

```sh
uv run bistra -j 8
```

Try a few worker counts; the fastest setting depends on your machine and test suite.

## For coding agents

Paste this into your agent:

```text
Install Bistra and switch our local and CI test commands to `uv run bistra`.
Keep our existing plugins and test settings, remove xdist scheduling options,
and check that the tests still pass.
```

## Plugins

Works with **pytest-cov** and **pytest-testmon** using their usual options:

```sh
uv run bistra --cov=my_package --cov-report=xml
uv run bistra --testmon
```

Bistra replaces xdist scheduling. Remove xdist options (`-n`, `--dist`,
`--looponfail`) from your commands and pytest config; xdist can stay installed.

## Optimizations

- **Timing history.** Balances work using past test runtimes.
- **Fixture locality.** Keeps tests with shared fixtures together unless splitting
  them is predicted to be faster.
- **Dynamic rebalancing.** Redistributes queued work as workers become available.
- **Less collection work.** Reuses worker collection results in the controller.

## Contributing

Bistra is early-stage software: expect bugs and breaking changes.
[Issues](https://github.com/bnkc/bistra/issues) and PRs are welcome.
See [CONTRIBUTING.md](https://github.com/bnkc/bistra/blob/main/CONTRIBUTING.md) to get started.
