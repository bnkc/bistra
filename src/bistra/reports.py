import builtins
import warnings
from typing import Any

import pytest

from .protocol import ReportEvent


def serialize_warning(warning: warnings.WarningMessage) -> dict[str, Any]:
    """Transport display metadata, not application warning objects."""
    category = warning.category or Warning
    family = next(base for base in category.__mro__ if vars(builtins).get(base.__name__) is base)
    source = warning.source
    if type(source) not in (type(None), bool, int, float, complex, str, bytes):
        source = repr(source)
    return {
        "text": str(warning.message),
        "category": category.__name__,
        "module": category.__module__,
        "qualname": category.__qualname__,
        "family": family.__name__,
        "filename": warning.filename,
        "lineno": warning.lineno,
        "line": warning.line,
        "source": source,
    }


def deserialize_warning(data: dict[str, Any]) -> warnings.WarningMessage:
    category = vars(builtins)[data["family"]]
    if data["module"] != "builtins" or data["qualname"] != category.__qualname__:
        category = type(
            data["category"],
            (category,),
            {"__module__": data["module"], "__qualname__": data["qualname"]},
        )
    return warnings.WarningMessage(
        category(data["text"]),
        category,
        data["filename"],
        data["lineno"],
        line=data["line"],
        source=data["source"],
    )


def replay(config: pytest.Config, event: ReportEvent, *, node: object | None = None) -> None:
    if event["type"] == "report" or event["type"] == "collectreport":
        report = config.hook.pytest_report_from_serializable(config=config, data=event["data"])
        if event["type"] == "report":
            report.node = node
            config.hook.pytest_runtest_logreport(report=report)
        else:
            config.hook.pytest_collectreport(report=report)
    elif event["type"] == "warning":
        config.hook.pytest_warning_recorded.call_historic(
            kwargs={
                "warning_message": deserialize_warning(event["data"]),
                "when": event["when"],
                "nodeid": event["nodeid"],
                "location": event["location"],
            }
        )
    elif event["type"] == "logfinish":
        config.hook.pytest_runtest_logfinish(nodeid=event["nodeid"], location=event["location"])
