"""Run cancellation terminal-state invariants."""

from app.agents.run_control import RunController, RunState


def test_cancelled_run_cannot_be_overwritten_by_late_completion_or_failure():
    controller = RunController("run-cancel-terminal")
    controller.start()

    controller.request_cancel()
    controller.mark_completed()
    controller.mark_failed()

    assert controller.state is RunState.CANCELLED
