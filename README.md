# bistra

Run pytest in parallel, using past test timings to schedule slower groups first.
Bistra uses pytest's assertions, fixtures, capture, and reports.

Still in alpha. Parallel runs require **Linux or macOS, Python 3.11+, and
pytest 8.4.2–9.x**.

## Install

Clone this repository, then install it in the project you want to test:

```sh
cd /path/to/your-project
uv add --dev /path/to/bistra
uv run bistra -- -q tests/
```

Replace `/path/to/bistra` with your checkout and keep it there while uv references
it. With pip, use your project's virtual environment:

```sh
python -m pip install /path/to/bistra
python -m bistra -- -q tests/
```

Install your project's test dependencies as usual. Bistra defaults to 12 workers;
use `-j N` to change that or `-j 1` for ordinary serial pytest. Put pytest arguments
after `--`.

You can also enable Bistra through pytest:

```sh
uv run pytest --bistra -q tests/
```

Plain `pytest` runs are unchanged. Use `--bistra-workers N` to set the worker count.
If `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` is set, add `-p bistra` or use the `bistra`
command.

## Check whether it helps

Compare a small, representative selection using the same pytest options:

```sh
time uv run pytest -q tests/
time uv run bistra -- -q tests/
```

Run each command three times, one at a time, and check that test outcomes agree.
Leave xdist, Testmon, and coverage out of this first comparison.

The first parallel run records timings; later runs use them to schedule tests.
Measure the first run separately. The cache stores durations only, and tests
still run if it is unavailable.

## Compatibility

Each child collects tests in a fresh interpreter, where native libraries initialize
independently. The original process also runs tests and counts toward the worker
limit. Bistra uses pytest internals, so pytest 10 is excluded until supported.

- Collection must produce the same ordered test IDs and fixture groups in every
  interpreter. Bistra checks this before running tests. Collection hooks run in
  each interpreter.
- Tests sharing package, module, or class fixtures stay grouped. Session fixtures
  are local to each worker. Dynamically requested fixtures cannot guide grouping
  and may repeat across workers.
- Collection state is local to each interpreter. Tests must isolate shared
  databases, files, and other external resources.
- Plugins passed to `pytest.main` must be importable modules. Use one worker for
  plugin instances. Scripts starting parallel pytest need an
  `if __name__ == "__main__":` guard.
- `worker_id` and `testrun_uid` fixtures work without xdist. Before conftest imports,
  worker identity is available through `config.workerinput` and
  `PYTEST_XDIST_WORKER`, `PYTEST_XDIST_WORKER_COUNT`, and `PYTEST_XDIST_TESTRUNUID`.
  Bistra does not implement all xdist worker-startup hooks.
- Do not combine Bistra with xdist worker options. Compatibility with coverage,
  Testmon, and other plugins that control test execution is not yet verified.
- Async tests still need an async plugin, such as pytest-asyncio. Use one worker
  for interactive debugging. Worker crashes count as failures.
- The original process delivers child reports on its main thread. A long test
  there can delay progress updates and stop requests; running tests finish before
  an early stop takes effect.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and builds.
