"""task.report snapshot reference 的薄适配透传测试。"""

from unittest.mock import patch

from callwarden.server.tools import tools_task


def _registered_tools():
    registrations = {}

    class FakeMcp:
        def tool(self):
            def decorator(fn):
                registrations[fn.__name__] = fn
                return fn

            return decorator

    tools_task.register(FakeMcp())
    return registrations


def test_task_report_step_forwards_snapshot_id_without_inventing_one():
    report_step = _registered_tools()["task_report_step"]
    with patch.object(tools_task, "_route", return_value={"status": "review"}) as route:
        result = report_step(
            task_id="T-REPORT-SNAPSHOT",
            step_id="S-REPORT-SNAPSHOT",
            result="done",
            snapshot_id="snapshot-from-publish",
        )

    assert result == {"status": "review"}
    method, params, op_class = route.call_args.args
    assert method == "task.report"
    assert op_class == "PROTECTED_MUTATION"
    assert params["snapshot_id"] == "snapshot-from-publish"


def test_task_report_step_keeps_missing_snapshot_empty():
    report_step = _registered_tools()["task_report_step"]
    with patch.object(tools_task, "_route", return_value={"status": "review"}) as route:
        report_step(task_id="T-REPORT-SNAPSHOT", step_id="S-REPORT-SNAPSHOT")

    assert route.call_args.args[1]["snapshot_id"] == ""
