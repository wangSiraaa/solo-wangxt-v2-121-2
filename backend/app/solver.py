"""按目标体积产率反推终馏切点（“由产率求终点”）。

设计要点
--------
1. 已知馏分起点温度 T_s 与目标 **体积** 产率 y（进料体积基准，%），求终点
   T_e 使 R(T_e) − R(T_s) = y。R 是 ``curve.py`` 校验过的 PCHIP 单调曲线。
2. **只在实测温度范围内求解**：T_e ∈ [T_s, T_max]，绝不外推。目标超出可达
   范围（y > R(T_max) − R(T_s)）时明确拒绝、给出可达产率区间，不改动任何
   已有方案；起点越界同样拒绝。
3. **平台段多解规则**：R 在平台段为常数时，同一目标产率对应一段温度区间
   [T_first, T_last]。规则固定为取 **最早可用温度** T_first（同样产率下尽早
   终馏，不把更重馏分混入本馏分），同时返回 T_last 供参考。固定次数的二分
   求解是确定性的：同一输入永远得到同一答案。
4. 目标产率只按 **体积 %** 解释。质量 % 是另一套基准（见 ``density.py``），
   不能把质量百分比当作体积百分比代入求解。
"""
from __future__ import annotations

from .curve import Issue, PreparedCurve, Severity

_R_TOL = 1e-9       # 回收量判定容差（百分点），吸收浮点噪声
_T_TOL = 1e-6       # 平台段判定的温度容差（℃）
_KNOT_SNAP_T = 0.01  # 解对齐实测结点的温度窗口（℃）：结点附近 ±_R_TOL 的解对齐到结点
_BISECT_ITERS = 80  # 固定迭代次数 => 结果确定、可复现


def _first_crossing(curve: PreparedCurve, r_target: float, lo: float, hi: float) -> float:
    """[lo, hi] 内满足 R(T) ≥ r_target − _R_TOL 的最小 T（最早达到目标的温度）。

    前提：R(hi) ≥ r_target − _R_TOL（调用方已把 r_target 夹到 R_max）。
    二分不变式：R(lo) < 阈值 ≤ R(hi)，hi 始终可行，收敛后返回 hi。
    """
    if float(curve.pchip(lo)) >= r_target - _R_TOL:
        return lo
    for _ in range(_BISECT_ITERS):
        mid = 0.5 * (lo + hi)
        if float(curve.pchip(mid)) >= r_target - _R_TOL:
            hi = mid
        else:
            lo = mid
    return hi


def _last_crossing(curve: PreparedCurve, r_target: float, lo: float, hi: float) -> float:
    """[lo, hi] 内满足 R(T) ≤ r_target + _R_TOL 的最大 T（平台段右端）。

    前提：R(lo) ≤ r_target + _R_TOL。二分不变式：R(lo) ≤ 阈值 < R(hi)，
    lo 始终可行，收敛后返回 lo。
    """
    if float(curve.pchip(hi)) <= r_target + _R_TOL:
        return hi
    for _ in range(_BISECT_ITERS):
        mid = 0.5 * (lo + hi)
        if float(curve.pchip(mid)) <= r_target + _R_TOL:
            lo = mid
        else:
            hi = mid
    return lo


def _snap_to_knot(curve: PreparedCurve, t: float, r_target: float) -> float:
    """若 t 紧邻某个实测结点且该结点回收量与目标一致（容差内），对齐到结点。

    PCHIP 在平台端点处导数为 0，二分解会落在结点旁 ±1e-4 ℃ 量级；
    对齐到结点让建议温度是可核对的实测值（如 100 ℃ 而非 99.9998 ℃）。
    """
    for tk, rk in zip(curve.temps, curve.recovered):
        if abs(float(tk) - t) <= _KNOT_SNAP_T and abs(float(rk) - r_target) <= 1e-6:
            return float(tk)
    return t


def solve_end_temp(
    curve: PreparedCurve, start_temp_c: float, target_yield_pct: float
) -> dict:
    """由起点温度与目标体积产率反推终馏切点。返回结构化结果（不落库）。

    返回字段
    ~~~~~~~~
    * ``feasible`` / ``reason``：是否解出；拒绝原因
      （``curve_invalid`` / ``start_out_of_range`` / ``target_unreachable``）；
    * ``achievable_yield_pct``：可达产率区间 [0, R(T_max) − R(T_s)]；
    * ``suggested_end_temp_c`` / ``actual_yield_pct``：建议终点与实际产率；
    * ``multiple_solutions`` / ``latest_end_temp_c``：平台段多解标记与最晚可行终点；
    * ``messages``：人类可读说明（含平台段选择规则与体积基准声明）；
    * ``issues``：曲线硬错误、起点越界等问题清单。
    """
    tmin, tmax = curve.t_min, curve.t_max
    rmin, rmax = curve.r_min, curve.r_max
    issues: list[Issue] = []
    messages: list[str] = []

    result: dict = {
        "feasible": False,
        "reason": None,
        "basis": "volume",
        "start_temp_c": start_temp_c,
        "start_recovery_pct": None,
        "target_yield_pct": target_yield_pct,
        "target_recovery_pct": None,
        "achievable_yield_pct": None,
        "suggested_end_temp_c": None,
        "actual_yield_pct": None,
        "multiple_solutions": False,
        "latest_end_temp_c": None,
        "applicable_range": {
            "temp_c": [tmin, tmax],
            "recovered_pct": [rmin, rmax],
            "extrapolation": "none",
        },
        "messages": messages,
        "issues": issues,  # 返回前统一 as_dict
    }

    # ---- 曲线硬错误：数据未核实前不做反推 ----
    if curve.hard_errors:
        result["reason"] = "curve_invalid"
        issues.extend(curve.hard_errors)
        messages.append(
            "曲线存在硬错误（见问题清单），数据核实修正前不做反推求解；方案未改动。"
        )
        return _finalize(result)

    # ---- 起点必须在实测范围内（不外推）----
    r_start, iss = curve.recovery_at(start_temp_c)
    if r_start is None:
        result["reason"] = "start_out_of_range"
        if iss is not None:
            issues.append(iss)
        messages.append(
            f"起点 {start_temp_c:g} ℃ 超出实测温度范围 [{tmin:g}, {tmax:g}] ℃，"
            "无数据、不外推，无法求解；方案未改动。"
        )
        return _finalize(result)
    result["start_recovery_pct"] = round(r_start, 4)

    # ---- 可达产率区间 ----
    max_yield = max(rmax - r_start, 0.0)
    result["achievable_yield_pct"] = [0.0, round(max_yield, 4)]

    if target_yield_pct > max_yield + _R_TOL:
        result["reason"] = "target_unreachable"
        messages.append(
            f"目标产率 {target_yield_pct:g}% 不可达：起点 {start_temp_c:g} ℃ 处已回收 "
            f"{r_start:g}%，实测终点 {tmax:g} ℃ 累计回收 {rmax:g}%，"
            f"该馏分在实测范围内最多还能切出 {max_yield:g}%。"
            "请降低目标产率，或补充更高温度的试验点后再求解（不外推）；方案未改动。"
        )
        return _finalize(result)

    # ---- 求解 R(T_e) = R(T_s) + y（夹到 R_max，防浮点越界）----
    r_target = min(r_start + target_yield_pct, rmax)
    result["target_recovery_pct"] = round(r_target, 4)

    t_first = _snap_to_knot(curve, _first_crossing(curve, r_target, start_temp_c, tmax), r_target)
    t_last = _snap_to_knot(curve, _last_crossing(curve, r_target, t_first, tmax), r_target)
    multiple = (t_last - t_first) > _T_TOL

    # 建议温度取 4 位小数（与其余输出一致），实际产率按该温度复算，保证可核对
    t_suggest = round(t_first, 4)
    actual = float(curve.pchip(t_suggest)) - r_start

    result.update(
        feasible=True,
        suggested_end_temp_c=t_suggest,
        actual_yield_pct=round(actual, 4),
        multiple_solutions=multiple,
        latest_end_temp_c=round(t_last, 4) if multiple else None,
    )

    if multiple:
        messages.append(
            f"目标回收量 {r_target:g}% 落在平台段：{t_first:g} ~ {t_last:g} ℃ 之间曲线水平，"
            f"该区间内任意终点的产率都相同（{actual:g}%）。"
            f"按“最早可用温度”规则建议取 {t_suggest:g} ℃——产率相同的前提下尽早终馏，"
            f"避免把更重馏分混入本馏分；若工艺上希望切到平台末端，"
            f"可取至 {t_last:g} ℃，产率不变。"
        )
    else:
        messages.append(
            f"在实测范围 {tmin:g}~{tmax:g} ℃ 内解得唯一终点：起点 {start_temp_c:g} ℃ 处"
            f"累积回收 {r_start:g}%，目标回收 {r_target:g}% 对应终点 {t_suggest:g} ℃，"
            f"实际产率 {actual:g}%（体积基准）。"
        )
    messages.append(
        "注意：目标产率按进料体积基准（体积 %）解释；质量 % 是另一套基准"
        "（需密度换算），不能把质量百分比当作体积百分比代入。"
    )
    return _finalize(result)


def _finalize(result: dict) -> dict:
    result["issues"] = [i.as_dict() for i in result["issues"]]
    return result
