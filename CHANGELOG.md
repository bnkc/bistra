# Changelog

## 0.1.1 (2026-10-30)

- Allow successful workers up to ten seconds for interpreter shutdown while
  retaining the two-second deadline for cancellation.

## 0.1.0 (2026-10-22)

- Duration-aware parallel pytest execution on Linux and macOS with cached test
  timings and fresh worker interpreters.
- Opt-in execution through the `bistra` command or `pytest --bistra`.
- pytest-cov support for combined worker coverage, reports, and coverage thresholds.
- Package validation, Python/pytest compatibility checks, and tag-triggered PyPI
  publishing.
