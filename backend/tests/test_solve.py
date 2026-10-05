"""按目标体积产率反推终止切点的核心规则测试（不依赖数据库/网络）。"""
from __future__ import annotations

import pytest

from app.curve import prepare_curve
from app.solve import solve_end_temp


def pc(ts, rs):
    return prepare_curve([{"temp_c": t, "recovered_pct": r} for t, r in zip(ts, rs)])


# ---------- 普通单调段：可核对的温度 ----------
def test_linear_curve_exact_checkable_temperature():
    # R(T) = T/2（严格单调）：start=50（R=25%），目标 25% => T_end = 100 ℃
    c = pc([0, 100, 200], [0, 50, 100])
    r = solve_end_temp(c, 50.0, 25.0)
    assert r["ok"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-6)
    assert r["actual_yield_pct"] == pytest.approx(25.0, abs=1e-6)
    # 可核对：R(100) − R(50) = 50 − 25 = 25
    assert r["recovery_at_end_pct"] - r["recovery_at_start_pct"] == pytest.approx(25.0)
    assert r["plateau"] is None
    assert r["basis"] == "volume"


def test_start_between_knots_uses_pchip_and_hits_target():
    # 起点落在结点之间：起点回收由 PCHIP 插值，实际产率仍须命中目标
    c = pc([30, 65, 100, 145, 185], [0, 6, 15, 28, 41])
    r = solve_end_temp(c, 80.0, 10.0)
    assert r["ok"]
    t = r["suggested_end_temp_c"]
    assert 80.0 < t <= 185.0
    # 用曲线复核：R(T_end) − R(start) == 目标
    # （建议温度保留 4 位小数，温度舍入带来的产率偏差 ≪ 1e-3 个百分点）
    assert float(c.pchip(t)) - float(c.pchip(80.0)) == pytest.approx(10.0, abs=1e-3)
    assert r["actual_yield_pct"] == pytest.approx(10.0, abs=1e-3)


def test_achievable_range_reported_from_start():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 50.0, 10.0)
    # 可达范围 = [0, R_max − R(start)] = [0, 50]
    assert r["achievable_yield_pct"] == [0.0, pytest.approx(50.0)]
    assert r["recovery_at_start_pct"] == pytest.approx(40.0)


def test_zero_target_gives_start_temperature():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 50.0, 0.0)
    assert r["ok"]
    assert r["suggested_end_temp_c"] == pytest.approx(50.0, abs=1e-9)
    assert r["actual_yield_pct"] == pytest.approx(0.0, abs=1e-9)


def test_target_equal_to_max_uses_earliest_reaching_temp():
    # 末端平台：R 在 150 ℃ 就达到 100%，目标=可达上限 => 最早达到点 150 ℃
    c = pc([0, 100, 150, 200], [0, 60, 100, 100])
    r = solve_end_temp(c, 0.0, 100.0)
    assert r["ok"]
    assert r["suggested_end_temp_c"] == pytest.approx(150.0, abs=1e-6)
    assert r["actual_yield_pct"] == pytest.approx(100.0, abs=1e-6)


# ---------- 平台段：多解取最早可用温度，规则稳定 ----------
def test_plateau_picks_earliest_temperature_and_reports_interval():
    # 100~200 ℃ 平台（R 恒为 50%）：目标 50% 的解集是整个平台
    c = pc([0, 100, 200, 300], [0, 50, 50, 100])
    r = solve_end_temp(c, 0.0, 50.0)
    assert r["ok"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-6)
    assert r["actual_yield_pct"] == pytest.approx(50.0, abs=1e-6)
    assert r["plateau"] is not None
    assert r["plateau"]["from_temp_c"] == pytest.approx(100.0, abs=1e-6)
    assert r["plateau"]["to_temp_c"] == pytest.approx(200.0, abs=1e-6)
    assert "最早" in r["plateau"]["note"]
    assert any(i["code"] == "plateau_multiple_solutions" for i in r["issues"])


def test_plateau_choice_is_deterministic_across_calls():
    c = pc([0, 100, 200, 300], [0, 50, 50, 100])
    temps = {solve_end_temp(c, 0.0, 50.0)["suggested_end_temp_c"] for _ in range(5)}
    assert len(temps) == 1  # 同一输入永远给出同一最早温度


def test_plateau_after_start_with_partial_target():
    # 起点在平台之前，目标落在平台内：解仍是平台左缘
    c = pc([0, 50, 100, 200, 300], [0, 20, 50, 50, 90])
    r = solve_end_temp(c, 0.0, 50.0)
    assert r["ok"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-6)
    assert r["plateau"]["to_temp_c"] == pytest.approx(200.0, abs=1e-6)


# ---------- 超出可达范围：明确拒绝 ----------
def test_target_beyond_reach_rejected_with_range():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 50.0, 60.0)  # 可达上限 = 90 − 40 = 50
    assert not r["ok"]
    assert r["suggested_end_temp_c"] is None
    assert r["actual_yield_pct"] is None
    assert r["achievable_yield_pct"] == [0.0, pytest.approx(50.0)]
    err = [i for i in r["issues"] if i["code"] == "target_out_of_reach"]
    assert err and err[0]["severity"] == "error"
    assert "不外推" in err[0]["message"]


def test_target_above_max_recovery_rejected():
    c = pc([10, 50, 100], [5, 40, 85])  # 缺端点曲线，R_max=85
    r = solve_end_temp(c, 10.0, 90.0)
    assert not r["ok"]
    assert r["achievable_yield_pct"] == [0.0, pytest.approx(80.0)]


def test_start_outside_measured_range_rejected():
    c = pc([10, 50, 100], [0, 40, 90])
    for bad_start in (5.0, 120.0):
        r = solve_end_temp(c, bad_start, 10.0)
        assert not r["ok"]
        assert r["achievable_yield_pct"] is None
        assert any(i["code"] == "solve_start_out_of_range" for i in r["issues"])


def test_negative_target_rejected():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 50.0, -1.0)
    assert not r["ok"]
    assert any(i["code"] == "solve_target_invalid" for i in r["issues"])


def test_blocked_curve_refuses_to_solve():
    c = pc([10, 20, 30], [0, 30, 20])  # 曲线下降 => 硬错误
    assert c.hard_errors
    r = solve_end_temp(c, 10.0, 10.0)
    assert not r["ok"]
    assert any(i["code"] == "solve_curve_blocked" for i in r["issues"])


# ---------- 基准声明 ----------
def test_result_declares_volume_basis_not_mass():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 10.0, 20.0)
    assert r["basis"] == "volume"
    assert "质量" in r["basis_note"]  # 明确不把质量% 当体积%
