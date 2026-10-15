# 🐇 Bistra

> **Pre-alpha.** Bistra is a work in progress. Expect bugs, missing features, and
> breaking changes. It's ready to experiment with, but not to rely on yet.

**Less waiting for pytest.**

Bistra runs your tests in parallel and uses timings from previous runs to start
the slowest groups first. You get pytest's assertions, fixtures, and reports,
with pytest as the only runtime dependency.

## Try it

Install your local Bistra checkout in the project you want to test:

```sh
uv add --dev /path/to/bistra
uv run bistra
```

## Compatibility

Each worker collects tests independently and runs its own session fixtures.
Collection must be consistent across workers, and tests need to isolate shared
databases and files. The `worker_id` and `testrun_uid` fixtures can help with that.

Don't combine Bistra with xdist worker options. Compatibility with coverage,
Testmon, and other plugins that control test execution hasn't been verified yet.

## Contributing

Trying Bistra on a real test suite is a big help.
[Report what breaks](https://github.com/bnkc/bistra/issues), or see the
[contributing guide](CONTRIBUTING.md) to work on the code.
