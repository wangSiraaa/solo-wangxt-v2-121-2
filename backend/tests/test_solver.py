"""按目标体积产率反推终馏切点（solve_end_temp）的核心规则与 API 测试。"""
from __future__ import annotations

import pytest

from app.curve import prepare_curve
from app.planning import evaluate_plan
from app.solver import solve_end_temp


def pc(ts, rs):
    return prepare_curve([{"temp_c": t, "recovered_pct": r} for t, r in zip(ts, rs)])


# ---------- 普通单调段：可核对的温度 ----------
def test_monotone_solution_hits_knot_exactly():
    c = pc([10, 50, 100, 150, 200], [0, 25, 50, 75, 100])
    r = solve_end_temp(c, 10.0, 25.0)  # 目标回收 25% 正好在结点 50 ℃
    assert r["feasible"] and r["reason"] is None
    assert not r["multiple_solutions"]
    assert r["latest_end_temp_c"] is None
    assert r["suggested_end_temp_c"] == pytest.approx(50.0, abs=1e-6)
    assert r["actual_yield_pct"] == pytest.approx(25.0, abs=1e-6)
    assert r["achievable_yield_pct"] == [0.0, 100.0]


def test_monotone_interior_solution_is_verifiable():
    c = pc([10, 50, 100, 150, 200], [0, 25, 50, 75, 100])
    r = solve_end_temp(c, 50.0, 10.0)
    t = r["suggested_end_temp_c"]
    assert r["feasible"] and 50 < t < 100
    # 可核对①：用曲线复算 建议温度处回收 − 起点回收 ≈ 目标
    assert float(c.pchip(t)) - float(c.pchip(50.0)) == pytest.approx(10.0, abs=1e-3)
    # 可核对②：与方案计算 evaluate_plan 的产率一致
    res = evaluate_plan(c, [{"name": "x", "start_temp_c": 50, "end_temp_c": t}], 0.0)
    assert res["cuts"][0]["volume_yield_pct"] == pytest.approx(
        r["actual_yield_pct"], abs=1e-3
    )


def test_solution_never_leaves_measured_range():
    c = pc([30, 100, 200], [5, 40, 90])  # 缺两端点
    r = solve_end_temp(c, 30.0, 85.0)  # 目标回收 90% = R_max
    assert r["feasible"]
    assert r["suggested_end_temp_c"] <= 200.0 + 1e-9
    assert r["suggested_end_temp_c"] == pytest.approx(200.0, abs=1e-6)
    assert r["applicable_range"]["extrapolation"] == "none"


# ---------- 平台段：多解取最早可用温度，规则稳定 ----------
def test_plateau_picks_earliest_temperature():
    c = pc([0, 50, 100, 150, 200], [0, 10, 40, 40, 100])  # 平台 100~150 ℃ @40%
    r = solve_end_temp(c, 0.0, 40.0)
    assert r["feasible"] and r["multiple_solutions"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-3)
    assert r["latest_end_temp_c"] == pytest.approx(150.0, abs=1e-3)
    assert r["actual_yield_pct"] == pytest.approx(40.0, abs=1e-6)
    assert any("平台" in m and "最早" in m for m in r["messages"])


def test_plateau_earliest_despite_float_noise():
    # 起点取非结点温度，使 起点回收+目标 带浮点噪声后仍压在平台 40% 上
    c = pc([0, 50, 100, 150, 200], [0, 10, 40, 40, 100])
    r_start = float(c.pchip(10.0))
    r = solve_end_temp(c, 10.0, 40.0 - r_start)
    assert r["feasible"] and r["multiple_solutions"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-3)


def test_plateau_rule_is_deterministic():
    c = pc([0, 50, 100, 150, 200], [0, 10, 40, 40, 100])
    assert solve_end_temp(c, 0.0, 40.0) == solve_end_temp(c, 0.0, 40.0)


def test_plateau_at_range_end_still_earliest():
    c = pc([0, 50, 100, 150], [0, 20, 60, 60])  # 平台一直延伸到实测终点
    r = solve_end_temp(c, 0.0, 60.0)
    assert r["feasible"] and r["multiple_solutions"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-3)
    assert r["latest_end_temp_c"] == pytest.approx(150.0, abs=1e-3)


def test_unique_solution_on_rising_segment_not_flagged_multiple():
    c = pc([0, 50, 100, 150, 200], [0, 10, 40, 40, 100])
    r = solve_end_temp(c, 0.0, 25.0)  # 落在 50~100 ℃ 上升段，唯一解
    assert r["feasible"] and not r["multiple_solutions"]
    assert 50 < r["suggested_end_temp_c"] < 100


# ---------- 拒绝路径：目标不可达 / 起点越界 / 曲线硬错误 ----------
def test_target_beyond_achievable_rejected_with_range():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 10.0, 95.0)  # 最多 90%
    assert not r["feasible"]
    assert r["reason"] == "target_unreachable"
    assert r["suggested_end_temp_c"] is None
    assert r["actual_yield_pct"] is None
    assert r["achievable_yield_pct"] == [0.0, 90.0]
    assert any("不可达" in m and "方案未改动" in m for m in r["messages"])


def test_target_exactly_at_max_is_feasible():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 10.0, 90.0)
    assert r["feasible"]
    assert r["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-6)
    assert r["actual_yield_pct"] == pytest.approx(90.0, abs=1e-6)


def test_start_out_of_range_rejected_both_sides():
    c = pc([10, 50, 100], [0, 40, 90])
    for s in (5.0, 120.0):
        r = solve_end_temp(c, s, 10.0)
        assert not r["feasible"] and r["reason"] == "start_out_of_range"
        assert r["suggested_end_temp_c"] is None
        assert any(i["code"] == "out_of_range" for i in r["issues"])


def test_start_at_range_end_only_zero_achievable():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 100.0, 1.0)
    assert not r["feasible"] and r["reason"] == "target_unreachable"
    assert r["achievable_yield_pct"] == [0.0, 0.0]
    r0 = solve_end_temp(c, 100.0, 0.0)
    assert r0["feasible"] and r0["suggested_end_temp_c"] == pytest.approx(100.0)


def test_zero_target_returns_start_temperature():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 50.0, 0.0)
    assert r["feasible"]
    assert r["suggested_end_temp_c"] == pytest.approx(50.0, abs=1e-6)
    assert r["actual_yield_pct"] == pytest.approx(0.0, abs=1e-9)


def test_invalid_curve_refused():
    c = pc([10, 20, 30], [0, 30, 20])  # 曲线下降 => 硬错误
    r = solve_end_temp(c, 10.0, 5.0)
    assert not r["feasible"] and r["reason"] == "curve_invalid"
    assert any(i["code"] == "curve_decreases" for i in r["issues"])


def test_volume_basis_declared_mass_not_silently_used():
    c = pc([10, 50, 100], [0, 40, 90])
    r = solve_end_temp(c, 10.0, 20.0)
    assert r["basis"] == "volume"
    assert any("体积" in m and "质量" in m for m in r["messages"])


# ---------- API：求解只读，拒绝时原方案不变 ----------
def _experiments(client):
    exps = client.get("/api/experiments").json()
    if not exps:
        client.post("/api/seed")
        exps = client.get("/api/experiments").json()
    return exps


def test_solve_end_api_monotone_hit(client):
    exp = _experiments(client)[0]  # 示例A：55~340 ℃，5~90%
    r = client.post(
        f"/api/experiments/{exp['id']}/solve-end",
        json={"start_temp_c": 185, "target_yield_pct": 13},  # 49% → 62% 在结点 225 ℃
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] and body["basis"] == "volume"
    assert body["suggested_end_temp_c"] == pytest.approx(225.0, abs=1e-6)
    assert body["actual_yield_pct"] == pytest.approx(13.0, abs=1e-6)
    assert body["achievable_yield_pct"] == [0.0, 41.0]  # 90 − 49
    assert body["applicable_range"]["temp_c"] == [55, 340]


def test_solve_end_api_plateau_via_created_experiment(client):
    payload = {
        "name": "plateau",
        "feed_density_g_cm3": 0.8,
        "points": [
            {"temp_c": 0, "recovered_pct": 0},
            {"temp_c": 50, "recovered_pct": 10},
            {"temp_c": 100, "recovered_pct": 40},
            {"temp_c": 150, "recovered_pct": 40},
            {"temp_c": 200, "recovered_pct": 100},
        ],
    }
    created = client.post("/api/experiments", json=payload)
    assert created.status_code == 201, created.text
    eid = created.json()["id"]
    body = client.post(
        f"/api/experiments/{eid}/solve-end",
        json={"start_temp_c": 0, "target_yield_pct": 40},
    ).json()
    assert body["feasible"] and body["multiple_solutions"]
    assert body["suggested_end_temp_c"] == pytest.approx(100.0, abs=1e-3)
    assert body["latest_end_temp_c"] == pytest.approx(150.0, abs=1e-3)


def test_solve_end_api_unreachable_rejects_and_plan_untouched(client):
    exp = _experiments(client)[0]
    plan = {
        "name": "untouched", "basis": "volume", "loss_pct": 1.5,
        "cuts": [{"name": "f1", "start_temp_c": 55, "end_temp_c": 185}],
    }
    before_eval = client.post(f"/api/experiments/{exp['id']}/evaluate", json=plan).json()
    before_plans = client.get(f"/api/experiments/{exp['id']}").json()["plans"]

    r = client.post(
        f"/api/experiments/{exp['id']}/solve-end",
        json={"start_temp_c": 55, "target_yield_pct": 95},  # 最多 85%
    )
    assert r.status_code == 200
    body = r.json()
    assert not body["feasible"] and body["reason"] == "target_unreachable"
    assert body["suggested_end_temp_c"] is None
    assert body["achievable_yield_pct"] == [0.0, 85.0]

    # 求解（含拒绝）不改动任何已存方案与计算结果
    after_eval = client.post(f"/api/experiments/{exp['id']}/evaluate", json=plan).json()
    after_plans = client.get(f"/api/experiments/{exp['id']}").json()["plans"]
    assert after_eval == before_eval
    assert after_plans == before_plans


def test_solve_end_api_validation_and_404(client):
    exp = _experiments(client)[0]
    # 负目标 / 超 100% 目标 => 422 明确拒绝
    assert client.post(
        f"/api/experiments/{exp['id']}/solve-end",
        json={"start_temp_c": 55, "target_yield_pct": -1},
    ).status_code == 422
    assert client.post(
        f"/api/experiments/{exp['id']}/solve-end",
        json={"start_temp_c": 55, "target_yield_pct": 101},
    ).status_code == 422
    assert client.post(
        "/api/experiments/99999/solve-end",
        json={"start_temp_c": 55, "target_yield_pct": 10},
    ).status_code == 404


def test_solve_end_api_curve_with_hard_error_refused(client):
    exps = _experiments(client)
    bad = next(e for e in exps if e["name"].startswith("示例C"))  # 曲线下降
    body = client.post(
        f"/api/experiments/{bad['id']}/solve-end",
        json={"start_temp_c": 40, "target_yield_pct": 10},
    ).json()
    assert not body["feasible"] and body["reason"] == "curve_invalid"
    assert any(i["code"] == "curve_decreases" for i in body["issues"])
