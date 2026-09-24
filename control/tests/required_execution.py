"""Opt-in acceptance gate: selected tests must execute without skips or xfails."""

import pytest


class RequiredExecution:
    def __init__(self) -> None:
        self.completed = 0
        self.unproven: set[str] = set()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped or hasattr(report, "wasxfail"):
            self.unproven.add(report.nodeid)
        if report.when == "call" and report.passed:
            self.completed += 1

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.unproven.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if exitstatus == pytest.ExitCode.OK and (self.unproven or self.completed == 0):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: object) -> None:
        # The public reporter type is defined in pytest's implementation module.
        from _pytest.terminal import TerminalReporter

        assert isinstance(terminalreporter, TerminalReporter)
        if self.unproven or self.completed == 0:
            terminalreporter.write_sep("=", "Acceptance evidence incomplete")
            for nodeid in sorted(self.unproven):
                terminalreporter.write_line(
                    f"Skipped or expected-failure test: {nodeid}"
                )
            if self.completed == 0:
                terminalreporter.write_line("No selected test completed successfully.")


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(RequiredExecution(), "required-execution-gate")
