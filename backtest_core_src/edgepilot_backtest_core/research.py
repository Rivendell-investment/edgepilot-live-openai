"""Stable Research-facing facade over the local-only backtest runner."""

from edgepilot_backtest_core.models import BacktestRequest
from edgepilot_backtest_core.runner import ReportExporter, execute_local_backtest


def run_backtest(
    request: BacktestRequest,
    *,
    run_id: str | None = None,
    report_exporter: ReportExporter | None = None,
) -> tuple[str, dict]:
    return execute_local_backtest(
        request,
        run_id=run_id,
        report_exporter=report_exporter,
    )
