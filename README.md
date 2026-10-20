<p align="center">
  <img src=".github/assets/bistra.svg" alt="Bistra" width="100%">
</p>

> **Pre-alpha.** Bistra is a work in progress. Expect bugs, missing features, and
> breaking changes. It's ready to experiment with, but not to rely on yet.

**Less waiting for pytest.**

Bistra runs your tests in parallel and uses timings from previous runs to balance
work across workers. You get pytest's assertions, fixtures, and reports,
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
The controller schedules the workers' agreed inventory without collecting the
suite again.

Duration history balances work across workers. A fixture group that limits
parallelism may be split when the predicted gain outweighs repeated setup and
teardown. Cold runs retain fixture locality; `xdist_group` marks always keep
tests together. Each worker runs its assigned tests in collection order.
Unstarted groups can move to a worker with less work remaining, provided that
preserves its collection order. Once dispatched, a group stays on the same worker.

The controller runs project setup before starting workers. Existing
`pytest_configure_node` hooks can populate `node.workerinput`; workers return
`config.workeroutput` to `pytest_testnodedown` for cleanup. Shared resources stay
available until workers finish. This supports local resource hooks, not xdist
gateways or custom xdist schedulers.

With pytest-cov installed, use its normal options, for example
`bistra --cov=my_package --cov-report=xml`. Bistra combines worker coverage before
generating reports and checking coverage thresholds. Versions 5–7 are tested.

Don't combine Bistra with xdist worker options. Compatibility with Testmon and
other plugins that control test execution hasn't been verified yet.

## Contributing

Trying Bistra on a real test suite is a big help.
[Report what breaks](https://github.com/bnkc/bistra/issues), or see the
[contributing guide](CONTRIBUTING.md) to work on the code.
