"""Duration-aware parallel execution for pytest.

PYTEST_DONT_REWRITE: the CLI imports this assertion-free module before pytest.
"""

from importlib.metadata import version

__version__ = version("bistra")
