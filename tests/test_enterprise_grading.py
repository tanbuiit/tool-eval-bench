"""Unit tests for the 3-scale enterprise grading skeleton (branch tavn/3scale).

Covers the B01 scope:
  * ``ScenarioDefinition.enterprise_points`` (0–3, optional)
  * ``evals.helpers.enterprise_score_earned`` tier mapping
  * ``runner.orchestrator.score_results`` enterprise aggregate (with the
    0–2 ``final_score`` formula pinned as unchanged)
  * sqlite schema v5 (nullable ``enterprise_score`` column, migration-safe)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tool_eval_bench.domain.scenarios import (
    Category,
    FailureKind,
    ScenarioDefinition,
    ScenarioResult,
    ScenarioStatus,
)
from tool_eval_bench.evals.helpers import (
    enterprise_score_earned,
    fail_eval,
    partial_eval,
    pass_eval,
)
from tool_eval_bench.runner.orchestrator import score_results
from tool_eval_bench.storage.db import _SCHEMA_VERSION, RunRepository


def _scenario(scenario_id: str, enterprise_points: int | None = None) -> ScenarioDefinition:
    return ScenarioDefinition(
        id=scenario_id,
        title=scenario_id,
        category=Category.A,
        user_message="test",
        description="test",
        handle_tool_call=lambda state, call: {},
        evaluate=pass_eval("ok"),
        enterprise_points=enterprise_points,
    )


def _result(scenario_id: str, status: ScenarioStatus, points: int) -> ScenarioResult:
    return ScenarioResult(scenario_id=scenario_id, status=status, points=points, summary="s")


# ---------------------------------------------------------------------------
# (a) domain field
# ---------------------------------------------------------------------------


def test_enterprise_points_defaults_to_none() -> None:
    scenario = _scenario("TC-E1")
    assert scenario.enterprise_points is None


def test_enterprise_points_accepts_0_to_3() -> None:
    for value in (0, 1, 2, 3):
        assert _scenario("TC-E1", enterprise_points=value).enterprise_points == value


# ---------------------------------------------------------------------------
# (b) tier mapping
# ---------------------------------------------------------------------------


def test_no_rubric_is_excluded() -> None:
    scenario = _scenario("TC-E1", enterprise_points=None)
    assert enterprise_score_earned(scenario, _result("TC-E1", ScenarioStatus.PASS, 2)) is None
    assert enterprise_score_earned(scenario, ScenarioStatus.PASS) is None
    assert enterprise_score_earned(scenario, "pass") is None


def test_fail_maps_to_0_partial_to_1() -> None:
    scenario = _scenario("TC-E1", enterprise_points=3)
    assert enterprise_score_earned(scenario, _result("TC-E1", ScenarioStatus.FAIL, 0)) == 0
    assert enterprise_score_earned(scenario, _result("TC-E1", ScenarioStatus.PARTIAL, 1)) == 1
    assert enterprise_score_earned(scenario, ScenarioStatus.FAIL) == 0
    assert enterprise_score_earned(scenario, "partial") == 1


def test_pass_maps_to_2_and_full_marks_on_top_tier_maps_to_3() -> None:
    top = _scenario("TC-E1", enterprise_points=3)
    assert enterprise_score_earned(top, _result("TC-E1", ScenarioStatus.PASS, 2)) == 3
    # A pass that is not full marks stays at tier 2.
    assert enterprise_score_earned(top, ScenarioStatus.PASS) == 2
    # A rubric that tops out below 3 can never award the excellent tier.
    capped = _scenario("TC-E2", enterprise_points=2)
    assert enterprise_score_earned(capped, _result("TC-E2", ScenarioStatus.PASS, 2)) == 2
    assert enterprise_score_earned(capped, "pass") == 2


# ---------------------------------------------------------------------------
# (c) aggregate in score_results — final_score untouched
# ---------------------------------------------------------------------------


def test_no_enterprise_scenarios_yields_null_enterprise_score() -> None:
    scenarios = [_scenario("TC-E1"), _scenario("TC-E2")]
    results = [_result("TC-E1", ScenarioStatus.PASS, 2), _result("TC-E2", ScenarioStatus.PASS, 2)]
    summary = score_results(results, scenarios)
    assert summary.enterprise_score is None
    assert summary.final_score == 100
    assert "enterprise_score" not in summary.to_dict()


def test_enterprise_aggregate_uses_earned_over_n_times_3() -> None:
    ent_top = _scenario("TC-E1", enterprise_points=3)
    ent_capped = _scenario("TC-E2", enterprise_points=2)
    plain = _scenario("TC-E3")
    scenarios = [ent_top, ent_capped, plain]
    results = [
        _result("TC-E1", ScenarioStatus.PASS, 2),  # enterprise tier 3
        _result("TC-E2", ScenarioStatus.PARTIAL, 1),  # enterprise tier 1
        _result("TC-E3", ScenarioStatus.PASS, 2),  # not part of the aggregate
    ]
    summary = score_results(results, scenarios)
    # (3 + 1) / (2 × 3) × 100 = 66.67 → 67
    assert summary.enterprise_score == 67
    # The 0–2 final_score formula is unchanged: (2 + 1 + 2) / (3 × 2) × 100 = 83
    assert summary.final_score == round((5 / 6) * 100)
    assert summary.to_dict()["enterprise_score"] == 67


def test_enterprise_scoring_does_not_change_final_score() -> None:
    base = [_scenario("TC-E1"), _scenario("TC-E2")]
    with_rubric = [_scenario("TC-E1", enterprise_points=3), _scenario("TC-E2", enterprise_points=0)]
    results = [_result("TC-E1", ScenarioStatus.PASS, 2), _result("TC-E2", ScenarioStatus.FAIL, 0)]
    assert (
        score_results(results, base).final_score == score_results(results, with_rubric).final_score
    )
    # The rubric cannot inflate or deflate the 0–2 score, whatever the tiers.
    assert score_results(results, base).final_score == 50


def test_infrastructure_failure_excluded_from_enterprise_aggregate() -> None:
    ent = _scenario("TC-E1", enterprise_points=3)
    scenarios = [ent]
    results = [
        ScenarioResult(
            scenario_id="TC-E1",
            status=ScenarioStatus.FAIL,
            points=0,
            summary="endpoint down",
            failure_kind=FailureKind.TIMEOUT,
        )
    ]
    summary = score_results(results, scenarios)
    # Excluded from numerator and denominator like the quality score → no
    # gradable enterprise scenarios → null.
    assert summary.enterprise_score is None


# ---------------------------------------------------------------------------
# (d) sqlite schema v5
# ---------------------------------------------------------------------------


def _make_v4_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE scenario_runs (
              run_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              status TEXT NOT NULL,
              model TEXT NOT NULL,
              config_json TEXT NOT NULL,
              scores_json TEXT,
              metadata_json TEXT,
              run_type TEXT NOT NULL DEFAULT 'tool_eval',
              report_path TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE run_checkpoints ("
            "run_id TEXT NOT NULL, scenario_id TEXT NOT NULL, created_at TEXT NOT NULL, "
            "result_json TEXT NOT NULL, PRIMARY KEY (run_id, scenario_id))"
        )
        conn.execute(
            "CREATE TABLE scenario_traces ("
            "run_id TEXT NOT NULL, scenario_id TEXT NOT NULL, raw_log TEXT NOT NULL, "
            "PRIMARY KEY (run_id, scenario_id))"
        )
        conn.execute("PRAGMA user_version = 4")
    conn.close()


def test_schema_version_is_5() -> None:
    assert _SCHEMA_VERSION == 5


def test_v4_database_migrates_to_v5_and_keeps_rows_writable(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _make_v4_database(db_path)
    with RunRepository(db_path=str(db_path)) as repo:
        conn = sqlite3.connect(db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(scenario_runs)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "enterprise_score" in columns
        assert version == 5
        # The migrated store stays writable and readable.
        repo.upsert_scenario_run(
            {
                "run_id": "migrated",
                "status": "completed",
                "config": {"model": "test"},
                "scores": {},
            }
        )
        assert repo.get("migrated") is not None


def test_fresh_database_is_created_at_v5(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.sqlite"
    with RunRepository(db_path=str(db_path)):
        pass
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scenario_runs)")}
    conn.close()
    assert version == _SCHEMA_VERSION
    assert "enterprise_score" in columns


# ---------------------------------------------------------------------------
# helpers referenced by the tier mapping stay coherent
# ---------------------------------------------------------------------------


def test_tier_helpers_match_status_points() -> None:
    assert pass_eval("ok").points == 2
    assert partial_eval("ok").points == 1
    assert fail_eval("ok").points == 0
