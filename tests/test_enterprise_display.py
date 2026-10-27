"""Tests for the 3-scale enterprise display surfaces (SP-0011 B07).

Covers the display-only additions that surface the separate 0–3 enterprise
rubric everywhere the 0–2 final score is already shown:

  * per-trial Scenario Results table (Ent column)
  * cross-trial Per-Scenario Results table (Ent per trial)
  * compare-reports parsers + HTML (tool-eval and summary)
  * CLI leaderboard (table + CSV/JSON export)
  * CLI history (table + compare summary)
  * live display scenario lines (ent/3)

The scoring logic is never changed here: ``final_score`` stays 0–2 and the
tier rule lives in ``domain.scenarios.enterprise_tier``, which is cross-checked
against ``evals.helpers.enterprise_score_earned``.
"""

from __future__ import annotations

import io
import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console

from tool_eval_bench.cli.display import BenchmarkDisplay
from tool_eval_bench.cli.history import compare_runs, print_history
from tool_eval_bench.application.leaderboard import (
    extract_leaderboard_rows as _extract_leaderboard_rows,
)
from tool_eval_bench.cli.leaderboard import (
    export_runs,
    print_leaderboard,
)
from tool_eval_bench.compare_reports import summary as cmp_summary
from tool_eval_bench.compare_reports import tool_eval as cmp_tool_eval
from tool_eval_bench.domain.scenarios import (
    CATEGORY_LABELS,
    Category,
    FailureKind,
    ScenarioDefinition,
    ScenarioResult,
    ScenarioStatus,
    enterprise_tier,
    scenario_report_metadata,
)
from tool_eval_bench.evals.helpers import (
    enterprise_score_earned,
    pass_eval,
)
from tool_eval_bench.runner.orchestrator import score_results
from tool_eval_bench.storage.reports import MarkdownReporter


def _scenario(
    scenario_id: str,
    enterprise_points: int | None = None,
    category: Category = Category.P,
) -> ScenarioDefinition:
    return ScenarioDefinition(
        id=scenario_id,
        title=f"{scenario_id} title",
        category=category,
        user_message="test",
        description="test",
        handle_tool_call=lambda state, call: {},
        evaluate=pass_eval("ok"),
        enterprise_points=enterprise_points,
    )


def _result(scenario_id: str, status: ScenarioStatus, points: int) -> ScenarioResult:
    return ScenarioResult(scenario_id=scenario_id, status=status, points=points, summary="s")


def _graded_pair() -> tuple[list[ScenarioDefinition], list[ScenarioResult]]:
    """One enterprise-graded scenario (top tier) and one plain scenario."""
    scenarios = [
        _scenario("TC-70", enterprise_points=3),
        _scenario("TC-71"),
    ]
    results = [
        _result("TC-70", ScenarioStatus.PASS, 2),  # enterprise tier 3
        _result("TC-71", ScenarioStatus.PASS, 2),  # no rubric
    ]
    return scenarios, results


# ---------------------------------------------------------------------------
# (a) the display-side tier rule agrees with the scorer
# ---------------------------------------------------------------------------


def test_enterprise_tier_matches_score_earned() -> None:
    for status in ScenarioStatus:
        for points in (0, 1, 2):
            for ep in (None, 0, 1, 2, 3):
                scenario = _scenario("TC-X", enterprise_points=ep)
                result = _result("TC-X", status, points)
                # Result-based scoring uses the real points.
                assert enterprise_tier(status, points, ep) == enterprise_score_earned(
                    scenario, result
                ), (status, points, ep)
                # A bare status carries no points evidence: the scorer uses a
                # sub-full-marks placeholder (points=1) so PASS never reaches 3.
                assert enterprise_tier(status, 1, ep) == enterprise_score_earned(
                    scenario, status
                ), (status, points, ep)


def test_enterprise_tier_none_without_rubric() -> None:
    assert enterprise_tier(ScenarioStatus.PASS, 2, None) is None
    assert enterprise_tier(ScenarioStatus.FAIL, 0, None) is None


# ---------------------------------------------------------------------------
# (b) per-trial report — Scenario Results table Ent column
# ---------------------------------------------------------------------------


def _write_trial_report(
    tmp_path: Path, summaries, scenarios, run_id: str = "run-a", model: str = "model-a"
) -> Path:
    reporter = MarkdownReporter(root=str(tmp_path))
    return reporter.write_scenario_report(
        run_id,
        model,
        summaries,
        scenario_metadata=scenario_report_metadata(scenarios),
    )


def test_scenario_report_has_enterprise_column_when_graded(tmp_path: Path) -> None:
    scenarios, results = _graded_pair()
    txt = _write_trial_report(tmp_path, score_results(results, scenarios), scenarios).read_text(
        encoding="utf-8"
    )

    assert "| Ent |" in txt
    header_row = [line for line in txt.splitlines() if line.startswith("| ID |")][0]
    assert header_row == "| ID | Title | Diff | Status | Points | Ent | Failure | Summary |"
    # Graded scenario: tier 3; plain scenario: en dash.
    row_70 = [line for line in txt.splitlines() if line.startswith("| TC-70 |")][0]
    assert "| 3 |" in row_70
    row_71 = [line for line in txt.splitlines() if line.startswith("| TC-71 |")][0]
    assert "| — |" in row_71


def test_scenario_report_omits_enterprise_column_without_rubric(tmp_path: Path) -> None:
    scenarios = [_scenario("TC-01")]
    results = [_result("TC-01", ScenarioStatus.PASS, 2)]
    txt = _write_trial_report(tmp_path, score_results(results, scenarios), scenarios).read_text(
        encoding="utf-8"
    )
    assert "| Ent |" not in txt
    header_row = [line for line in txt.splitlines() if line.startswith("| ID |")][0]
    assert "| Failure |" in header_row


def test_scenario_report_enterprise_dash_for_infrastructure_failure(tmp_path: Path) -> None:
    scenario = _scenario("TC-70", enterprise_points=3)
    result = ScenarioResult(
        scenario_id="TC-70",
        status=ScenarioStatus.FAIL,
        points=0,
        summary="endpoint down",
        failure_kind=FailureKind.TIMEOUT,
    )
    txt = _write_trial_report(tmp_path, score_results([result], [scenario]), [scenario]).read_text(
        encoding="utf-8"
    )
    row = [line for line in txt.splitlines() if line.startswith("| TC-70 |")][0]
    # Infrastructure failures leave the enterprise denominator too → en dash,
    # never tier 0.
    assert "| — |" in row


def test_scenario_report_enterprise_tiers_for_all_outcomes(tmp_path: Path) -> None:
    scenarios = [_scenario(s, enterprise_points=3) for s in ("TC-70", "TC-71", "TC-72")]
    results = [
        _result("TC-70", ScenarioStatus.PASS, 2),  # 3
        _result("TC-71", ScenarioStatus.PARTIAL, 1),  # 1
        _result("TC-72", ScenarioStatus.FAIL, 0),  # 0
    ]
    txt = _write_trial_report(tmp_path, score_results(results, scenarios), scenarios).read_text(
        encoding="utf-8"
    )
    assert "| 3 |" in [line for line in txt.splitlines() if line.startswith("| TC-70 |")][0]
    assert "| 1 |" in [line for line in txt.splitlines() if line.startswith("| TC-71 |")][0]
    assert "| 0 |" in [line for line in txt.splitlines() if line.startswith("| TC-72 |")][0]


# ---------------------------------------------------------------------------
# (c) cross-trial summary — Per-Scenario Results Ent columns
# ---------------------------------------------------------------------------


def _agg() -> dict:
    return {
        "trials": 2,
        "final_score_mean": 100.0,
        "final_score_stddev": 0.0,
        "total_points_mean": 2.0,
        "total_points_stddev": 0.0,
        "pass_at_k": 100.0,
        "pass_hat_k": 100.0,
        "reliability_gap": 0.0,
        "final_score_ci95": (100.0, 100.0),
        "per_scenario": {},
        "per_category": {},
    }


def _write_summary_report(
    tmp_path: Path, summaries, scenarios, run_id: str = "run-sum", model: str = "model-a"
) -> Path:
    reporter = MarkdownReporter(root=str(tmp_path))
    return reporter.write_summary_report(
        run_id,
        model,
        summaries,
        _agg(),
        scenario_metadata=scenario_report_metadata(scenarios),
    )


def test_summary_report_has_enterprise_columns_when_graded(tmp_path: Path) -> None:
    scenarios = [_scenario("TC-70", enterprise_points=3), _scenario("TC-71")]
    trial_1 = score_results(
        [
            _result("TC-70", ScenarioStatus.PASS, 2),
            _result("TC-71", ScenarioStatus.PASS, 2),
        ],
        scenarios,
    )
    trial_2 = score_results(
        [
            _result("TC-70", ScenarioStatus.PARTIAL, 1),
            _result("TC-71", ScenarioStatus.PASS, 2),
        ],
        scenarios,
    )
    txt = _write_summary_report(tmp_path, [trial_1, trial_2], scenarios).read_text(encoding="utf-8")

    assert "| Ent T1 |" in txt and "| Ent T2 |" in txt
    row_70 = [line for line in txt.splitlines() if line.startswith("| TC-70 |")][0]
    assert "| 3 |" in row_70 and "| 1 |" in row_70
    row_71 = [line for line in txt.splitlines() if line.startswith("| TC-71 |")][0]
    assert "| — |" in row_71


def test_summary_report_omits_enterprise_columns_without_rubric(tmp_path: Path) -> None:
    scenarios = [_scenario("TC-01")]
    summary = score_results([_result("TC-01", ScenarioStatus.PASS, 2)], scenarios)
    txt = _write_summary_report(tmp_path, [summary], scenarios).read_text(encoding="utf-8")
    assert "Ent T1" not in txt


# ---------------------------------------------------------------------------
# (d) compare-reports — tool-eval parser + HTML
# ---------------------------------------------------------------------------


def test_compare_tool_eval_parses_enterprise_fields(tmp_path: Path) -> None:
    scenarios, results = _graded_pair()
    report = _write_trial_report(tmp_path, score_results(results, scenarios), scenarios)
    parsed = cmp_tool_eval.parse_md(str(report))

    assert parsed["enterprise_score"] == 100
    assert parsed["enterprise_categories"] == [
        {
            "name": CATEGORY_LABELS[Category.P],
            "scenarios": 1,
            "earned": 3,
            "max": 3,
            "percent": 100.0,
        }
    ]
    by_id = {s["id"]: s for s in parsed["scenarios"]}
    assert by_id["TC-70"]["enterprise_earned"] == 3
    assert by_id["TC-71"]["enterprise_earned"] is None
    # Existing fields keep their indices despite the extra column.
    assert by_id["TC-70"]["points_earned"] == 2
    assert by_id["TC-70"]["points_max"] == 2


def test_compare_tool_eval_html_renders_enterprise(tmp_path: Path) -> None:
    scenarios, results = _graded_pair()
    report_a = _write_trial_report(tmp_path, score_results(results, scenarios), scenarios)
    # A legacy run with no enterprise rubric at all.
    plain_scenarios = [_scenario("TC-01")]
    report_b = _write_trial_report(
        tmp_path,
        score_results([_result("TC-01", ScenarioStatus.PASS, 2)], plain_scenarios),
        plain_scenarios,
        run_id="run-b",
        model="model-b",
    )

    out = tmp_path / "cmp.html"
    cmp_tool_eval.generate_html(
        cmp_tool_eval.parse_md(str(report_a)),
        cmp_tool_eval.parse_md(str(report_b)),
        str(out),
    )
    html = out.read_text(encoding="utf-8")

    assert "Enterprise Score (0–3 rubric)" in html
    assert "100 / 100" in html  # graded side
    assert "Enterprise by Category (0–3 rubric)" in html
    assert "100%" in html and "(3/3)" in html
    assert "Separate 0–3 rubric" in html


def test_compare_tool_eval_html_without_enterprise_has_no_enterprise_section(
    tmp_path: Path,
) -> None:
    scenarios = [_scenario("TC-01")]
    report_a = _write_trial_report(
        tmp_path, score_results([_result("TC-01", ScenarioStatus.PASS, 2)], scenarios), scenarios
    )
    report_b = _write_trial_report(
        tmp_path,
        score_results([_result("TC-01", ScenarioStatus.PASS, 2)], scenarios),
        scenarios,
        run_id="run-b",
        model="model-b",
    )
    out = tmp_path / "cmp.html"
    cmp_tool_eval.generate_html(
        cmp_tool_eval.parse_md(str(report_a)),
        cmp_tool_eval.parse_md(str(report_b)),
        str(out),
    )
    html = out.read_text(encoding="utf-8")
    assert "Enterprise Score" not in html
    assert "Enterprise by Category" not in html


# ---------------------------------------------------------------------------
# (e) compare-reports — summary parser + HTML
# ---------------------------------------------------------------------------


def test_compare_summary_parses_enterprise_fields(tmp_path: Path) -> None:
    scenarios = [_scenario("TC-70", enterprise_points=3), _scenario("TC-71")]
    trial_1 = score_results(
        [_result("TC-70", ScenarioStatus.PASS, 2), _result("TC-71", ScenarioStatus.PASS, 2)],
        scenarios,
    )
    trial_2 = score_results(
        [_result("TC-70", ScenarioStatus.PARTIAL, 1), _result("TC-71", ScenarioStatus.PASS, 2)],
        scenarios,
    )
    report = _write_summary_report(tmp_path, [trial_1, trial_2], scenarios)

    parsed = cmp_summary.parse_summary(str(report))
    # Per-trial enterprise: 100 (3/3) and 33 (1/3) → mean 66.5, sample σ 47.4.
    assert parsed["enterprise_mean"] == 66.5
    assert parsed["enterprise_std"] == 47.4
    # The category table is sourced from the first graded trial: 3/3 = 100%.
    assert parsed["enterprise_categories"] == [
        {
            "name": CATEGORY_LABELS[Category.P],
            "scenarios": 1,
            "earned": 3,
            "max": 3,
            "percent": 100.0,
        }
    ]


def test_compare_summary_html_renders_enterprise(tmp_path: Path) -> None:
    scenarios = [_scenario("TC-70", enterprise_points=3), _scenario("TC-71")]
    trial_1 = score_results(
        [_result("TC-70", ScenarioStatus.PASS, 2), _result("TC-71", ScenarioStatus.PASS, 2)],
        scenarios,
    )
    trial_2 = score_results(
        [_result("TC-70", ScenarioStatus.PARTIAL, 1), _result("TC-71", ScenarioStatus.PASS, 2)],
        scenarios,
    )
    report_a = _write_summary_report(tmp_path, [trial_1, trial_2], scenarios)

    # A summary with no enterprise rubric.
    plain_scenarios = [_scenario("TC-01")]
    plain = score_results([_result("TC-01", ScenarioStatus.PASS, 2)], plain_scenarios)
    report_b = _write_summary_report(
        tmp_path, [plain], plain_scenarios, run_id="run-sum-b", model="model-b"
    )

    out = tmp_path / "cmp.html"
    cmp_summary.generate_html(
        cmp_summary.parse_summary(str(report_a)),
        cmp_summary.parse_summary(str(report_b)),
        str(out),
    )
    html = out.read_text(encoding="utf-8")
    assert "Enterprise Score (0–3 rubric)" in html
    assert "66 / 100" in html
    assert "Enterprise by Category (0–3 rubric)" in html
    assert "(3/3)" in html


# ---------------------------------------------------------------------------
# (f) CLI leaderboard
# ---------------------------------------------------------------------------


def _leaderboard_run(**overrides) -> dict:
    run = {
        "model": "test-model",
        "run_id": "run_1",
        "created_at": "2026-10-10T13:07:43",
        "status": "completed",
        "config": {"scenario_count": 2, "backend": "vllm"},
        "scores": {
            "final_score": 90,
            "rating": "★★★★★ Excellent",
            "total_points": 180,
            "max_points": 200,
            "category_scores": [{"category": "P", "percent": 90}],
            "scenario_results": [
                {"status": "pass", "scenario_id": "TC-70"},
                {"status": "pass", "scenario_id": "TC-71"},
            ],
            "total_tokens": 5000,
            "safety_warnings": [],
            "enterprise_score": 81,
        },
        "metadata": {},
    }
    run.update(overrides)
    return run


def test_leaderboard_extract_includes_enterprise_score() -> None:
    rows = _extract_leaderboard_rows([_leaderboard_run()])
    assert rows[0]["enterprise_score"] == 81


def test_leaderboard_extract_dash_without_enterprise() -> None:
    run = _leaderboard_run()
    del run["scores"]["enterprise_score"]
    rows = _extract_leaderboard_rows([run])
    assert rows[0]["enterprise_score"] is None


def _capture_export(fmt: str) -> str:
    """Run export_runs with the repository mocked, capturing stdout."""
    import sys

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        with patch("tool_eval_bench.storage.db.RunRepository") as MockRepo:
            repo = MagicMock()
            repo.list.return_value = [_leaderboard_run()]
            MockRepo.return_value = repo
            export_runs(Console(file=StringIO(), width=200, no_color=True), fmt=fmt)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def test_leaderboard_prints_enterprise_column() -> None:
    console = Console(file=StringIO(), width=200, no_color=True)
    with patch("tool_eval_bench.storage.db.RunRepository") as MockRepo:
        repo = MagicMock()
        repo.list.return_value = [_leaderboard_run()]
        MockRepo.return_value = repo
        print_leaderboard(console)
    output = console.file.getvalue()
    assert "Ent" in output
    assert "81" in output


def test_leaderboard_export_json_includes_enterprise_score() -> None:
    payload = json.loads(_capture_export("json"))
    assert payload[0]["enterprise_score"] == 81


def test_leaderboard_export_csv_includes_enterprise_score() -> None:
    output = _capture_export("csv")
    assert "enterprise_score" in output.splitlines()[0]
    assert "81" in output.splitlines()[1]


# ---------------------------------------------------------------------------
# (g) CLI history
# ---------------------------------------------------------------------------


def test_history_shows_enterprise_column() -> None:
    runs = [
        {
            "run_id": "run_a",
            "model": "model-a",
            "scores": {"final_score": 93, "enterprise_score": 81},
            "created_at": "2026-10-10T13:07:43",
        },
        {
            "run_id": "run_b",
            "model": "model-b",
            "scores": {"final_score": 90},
            "created_at": "2026-10-10T15:14:37",
        },
    ]
    with patch("tool_eval_bench.storage.db.RunRepository") as MockRepo:
        repo = MagicMock()
        repo.list.return_value = runs
        MockRepo.return_value = repo
        console = Console(file=StringIO(), width=200, no_color=True)
        print_history(console)
    output = console.file.getvalue()
    assert "81" in output
    assert "—" in output  # the ungraded run still shows the column


def test_compare_runs_shows_enterprise_line() -> None:
    run_a = {
        "run_id": "run_a_id",
        "config": {"model": "model-a"},
        "scores": {
            "final_score": 70,
            "enterprise_score": 60,
            "scenario_results": [
                {"scenario_id": "TC-70", "points": 2, "status": "pass"},
                {"scenario_id": "TC-71", "points": 0, "status": "fail"},
            ],
        },
    }
    run_b = {
        "run_id": "run_b_id",
        "config": {"model": "model-b"},
        "scores": {
            "final_score": 85,
            "enterprise_score": 80,
            "scenario_results": [
                {"scenario_id": "TC-70", "points": 2, "status": "pass"},
                {"scenario_id": "TC-71", "points": 2, "status": "pass"},
            ],
        },
    }
    with patch("tool_eval_bench.storage.db.RunRepository") as MockRepo:
        repo = MagicMock()
        repo.get.side_effect = lambda rid: run_a if rid == "run_a_id" else run_b
        repo.get_latest.return_value = None
        MockRepo.return_value = repo
        console = Console(file=StringIO(), width=200, no_color=True)
        compare_runs(console, "run_a_id", "run_b_id")
    output = console.file.getvalue()
    assert "Enterprise (0–3 rubric):  60 → 80" in output
    assert "0–2 final score is unchanged" in output


# ---------------------------------------------------------------------------
# (h) live display scenario lines
# ---------------------------------------------------------------------------


def _display(scenarios) -> BenchmarkDisplay:
    return BenchmarkDisplay(
        model="test-model",
        backend="vllm",
        base_url="http://localhost:8080",
        scenarios=scenarios,
    )


def _plain(line: str) -> str:
    """Strip Rich markup tags so a formatted live line is readable."""
    import re

    return re.sub(r"\[[^\]]*\]", "", line)


def test_live_line_shows_enterprise_tier_when_graded() -> None:
    scenario = _scenario("TC-70", enterprise_points=3)
    display = _display([scenario])
    line = _plain(display._format_result_line(scenario, _result("TC-70", ScenarioStatus.PASS, 2)))
    assert "3/3" in line
    line_partial = _plain(
        display._format_result_line(scenario, _result("TC-70", ScenarioStatus.PARTIAL, 1))
    )
    assert "1/3" in line_partial


def test_live_line_shows_dash_for_infrastructure_failure() -> None:
    scenario = _scenario("TC-70", enterprise_points=3)
    result = ScenarioResult(
        scenario_id="TC-70",
        status=ScenarioStatus.FAIL,
        points=0,
        summary="endpoint down",
        failure_kind=FailureKind.TIMEOUT,
    )
    display = _display([scenario])
    line = _plain(display._format_result_line(scenario, result))
    assert "–/3" in line


def test_live_line_omits_enterprise_without_rubric() -> None:
    scenario = _scenario("TC-01")
    display = _display([scenario])
    line = _plain(display._format_result_line(scenario, _result("TC-01", ScenarioStatus.PASS, 2)))
    assert "/3" not in line
