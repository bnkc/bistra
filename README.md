# bistra

A small, experimental pytest extension that collects concurrently in fresh
interpreters, then runs tests through a shared duration-aware queue. Tests still use pytest's assertions, fixtures,
capture, and reports. Test results are never cached or skipped by bistra.

**Requirements:** macOS or Linux for parallel execution, Python 3.11+, and
pytest 8.4.2–9.x. No Rust toolchain or compiler is needed. Windows parallel
execution is not supported. One worker delegates execution to ordinary pytest.

## Install in your own project

Clone or download this repository. From the Python project whose tests you want
to run, install bistra as a development dependency. Replace the example path with
your checkout's location:

```sh
cd /path/to/your-project
uv add --dev /path/to/bistra
uv run bistra --version
uv run bistra -- -q tests/
```

Keep the checkout at that path while your uv project references it. If you use pip,
activate the project's existing virtual environment instead:

```sh
python -m pip install /path/to/bistra
python -m bistra -- -q tests/
```

Your project's test dependencies must already be installed. Installing bistra
does not enable it for ordinary pytest invocations. You can opt in through pytest:

```sh
uv run pytest --bistra -q tests/
```

If `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` is set, use the `bistra` command above, or
add `-p bistra` to the pytest command.

## Check whether it helps

Start with a small representative selection, then widen it if results agree:

```sh
time uv run pytest -q tests/
time uv run bistra -- -q tests/
```

Use the same paths, filters and pytest options on both commands. Compare pass,
failure, error, skip and xfail results, not just elapsed time. Repeat each command
three times; do not run the comparisons simultaneously. Dependency installation
is outside the timed commands. Avoid combining this first comparison with xdist,
Testmon selection, or coverage instrumentation.

Bistra automatically learns test durations. The first parallel run records
timings; subsequent runs use that history to start expensive work earlier and
distribute it across workers. Tests sharing module or class fixtures stay grouped
to preserve fixture reuse. The optional local cache stores timings, never results;
an unavailable cache does not prevent tests from running. Compare the initial
learning run and subsequent runs separately when measuring speed.

Bistra defaults to 12 workers. Override it with `-j N` or pytest's
`--bistra-workers N`.
Use `-j 1` for ordinary serial execution. Put pytest's arguments after `--`.
Run `bistra --help` for details.

## Compatibility

Each child starts a fresh Python interpreter, so native libraries initialize in
that interpreter instead of inheriting another process's runtime state. No native
routing decorators are needed. The configured worker count includes the original
process, which executes tests alongside the fresh workers.

Every interpreter collects the same suite concurrently. Before executing any tests,
Bistra checks that ordered test IDs and fixture groups agree. Non-deterministic
collection fails explicitly; collection hooks must tolerate running per interpreter.

This is an alpha prototype, not a guaranteed speedup or a universal plugin
replacement. It uses pytest internals; its dependency is bounded below pytest 10.

- Session fixtures execute once per worker; module/class fixtures can repeat
  when tests are split across workers. Tests must tolerate parallel execution.
- Python state created during collection is local to each interpreter. Shared
  databases, files, and external resources still require fixture isolation.
- Programmatic pytest plugins must be importable modules. For plugin instances
  passed directly to `pytest.main`, use `-j 1`. Python scripts calling parallel
  pytest must guard their entry point with `if __name__ == "__main__":`.
- Worker identity is available before initial conftest imports through
  `config.workerinput` and the `PYTEST_XDIST_WORKER`, `PYTEST_XDIST_WORKER_COUNT`,
  and `PYTEST_XDIST_TESTRUNUID` environment variables. The `worker_id` and
  `testrun_uid` fixtures work without installing xdist. Bistra does not implement
  xdist's complete worker-startup hook lifecycle.
- Do not combine bistra with xdist worker options. Coverage, Testmon and plugins
  that manage their own execution lifecycle have not been generally certified.
- Async tests continue to require your normal async plugin, such as pytest-asyncio.
  Interactive debugging requires `-j 1`. Worker crashes are reported as failures.
- Reports from children are delivered on the original process's main thread.
  Progress updates and parent-plugin stop decisions can wait while that process
  executes a long test; in-flight tests finish before an early stop takes effect.

## Source and development

The `src/bistra/` directory contains the complete implementation; `tests/`
contains focused runner regression tests. To develop from this checkout:

```sh
uv sync --locked
uv run tox -e py,lint,package
```

This tests the installed wheel with the current interpreter, checks style, and
validates package metadata. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full
compatibility matrix and project setup.

To build a source distribution and wheel in `dist/`:

```sh
uv build
```
