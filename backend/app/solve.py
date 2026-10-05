"""按目标体积产率反推终止切点温度（只在实测温度范围内求解）。

已知某馏分的起始温度 ``start_temp_c`` 与目标 **体积** 产率（进料体积基准 %），
在 PCHIP 累积回收曲线上求终止切点 ``T_end``，使

    R(T_end) − R(start_temp_c) = 目标产率

规则
----
1. **严格不外推**：起始温度必须落在实测温度区间 ``[T_min, T_max]`` 内，
   求解也只在 ``[start, T_max]`` 上进行；可达产率上限为
   ``R(T_max) − R(start)``，目标超出该范围即明确拒绝，不给任何外推温度。
2. **体积基准**：目标产率一律按体积百分数理解——曲线纵轴本身就是
   体积基准；质量百分比是另一套基准（见 ``density.py``），不参与求解，
   也不把质量% 当作体积% 代入。
3. **平台段多解**：PCHIP 单调非降，方程的解集是连续区间
   ``[T_first, T_last]``（曲线平台段上 R(T) 恒定）。此时按稳定规则取
   **最早可用温度** ``T_first``，并在结果中报告整个平台区间与说明。
4. 本模块只计算、不落库；是否采纳建议切点由用户在前端预览后决定。
"""
from __future__ import annotations

from .curve import Issue, PreparedCurve, Severity

# 二分收敛精度（℃）：温度范围通常 ≤ 600 ℃，64 次迭代可达 1e-14 量级
_BISECT_ITERS = 64
# 解集宽度超过该值（℃）即判定为平台段多解
_PLATEAU_MIN_WIDTH_C = 1e-6
# 产率比较容差（百分点）
_YIELD_TOL = 1e-9


def _first_reaching(curve: PreparedCurve, level: float, lo: float, hi: float) -> float:
    """单调非降 R(T) 在 [lo, hi] 上**最早**达到 ``level`` 的温度。

    前置条件：``R(lo) <= level <= R(hi)``。二分保持 ``R(lo) <= level``、
    ``R(hi) >= level`` 不变式，收敛到解集左端点（平台段时即平台左缘）。
    """
    for _ in range(_BISECT_ITERS):
        mid = 0.5 * (lo + hi)
        if float(curve.pchip(mid)) >= level:
            hi = mid
        else:
            lo = mid
    return hi


def _last_at_level(curve: PreparedCurve, level: float, lo: float, hi: float) -> float:
    """单调非降 R(T) 在 [lo, hi] 上**最晚**仍等于 ``level`` 的温度。

    前置条件：``R(lo) <= level <= R(hi)``。用于刻画平台段解集右端点。
    """
    for _ in range(_BISECT_ITERS):
        mid = 0.5 * (lo + hi)
        if float(curve.pchip(mid)) <= level:
            lo = mid
        else:
            hi = mid
    return lo


def solve_end_temp(
    curve: PreparedCurve,
    start_temp_c: float,
    target_yield_pct: float,
) -> dict:
    """按目标体积产率求解终止切点温度。

    返回结构化结果（``ok`` 标记是否给出建议温度）：

    * ``achievable_yield_pct``：从该起点出发、在实测范围内可达的体积产率
      区间 ``[0, R(T_max) − R(start)]``（连续单调曲线的介值集，无空洞）；
    * ``suggested_end_temp_c`` / ``actual_yield_pct``：建议终止切点及用
      PCHIP 复核的实际体积产率（供与目标核对）；
    * ``plateau``：平台段多解时给出 ``[T_first, T_last]`` 区间与说明，
      建议温度恒为最早可用温度 ``T_first``；
    * 拒绝时 ``ok=False`` 且不含建议温度，``issues`` 说明原因。
    """
    start = float(start_temp_c)
    target = float(target_yield_pct)
    issues: list[Issue] = []
    result: dict = {
        "ok": False,
        "basis": "volume",
        "basis_note": (
            "目标产率按体积百分数（进料体积基准）求解；曲线纵轴即体积基准，"
            "质量百分比是另一套基准，不参与本求解"
        ),
        "start_temp_c": start,
        "target_yield_pct": target,
        "achievable_yield_pct": None,
        "recovery_at_start_pct": None,
        "suggested_end_temp_c": None,
        "actual_yield_pct": None,
        "recovery_at_end_pct": None,
        "plateau": None,
        "issues": [],
    }

    def reject(issue: Issue) -> dict:
        issues.append(issue)
        result["issues"] = [i.as_dict() for i in issues]
        return result

    # ---- 曲线本身有硬错误：不求解，先核实数据 ----
    if curve.hard_errors:
        return reject(
            Issue(
                "solve_curve_blocked",
                Severity.ERROR,
                "蒸馏曲线存在硬错误（"
                + "；".join(i.message for i in curve.hard_errors[:2])
                + "），请先核实修正试验数据，再按目标产率反推切点",
            )
        )

    tmin, tmax = curve.t_min, curve.t_max

    # ---- 起始温度必须在实测范围内（不外推）----
    if start < tmin or start > tmax:
        return reject(
            Issue(
                "solve_start_out_of_range",
                Severity.ERROR,
                f"起始温度 {start:g} ℃ 超出实测温度范围 [{tmin:g}, {tmax:g}] ℃，"
                "范围外无数据、不外推，无法反推终止切点",
            )
        )

    r_start = float(curve.pchip(start))
    r_max = curve.r_max
    max_yield = max(r_max - r_start, 0.0)
    result["recovery_at_start_pct"] = round(r_start, 4)
    result["achievable_yield_pct"] = [0.0, round(max_yield, 4)]

    # ---- 目标产率合法性（体积百分数，0 ~ 可达上限）----
    if not (target >= 0.0):  # 含 NaN
        return reject(
            Issue(
                "solve_target_invalid",
                Severity.ERROR,
                f"目标产率 {target!r} 非法：体积产率应为 0 ~ 100 之间的数值",
            )
        )
    if target > max_yield + _YIELD_TOL:
        return reject(
            Issue(
                "target_out_of_reach",
                Severity.ERROR,
                f"目标体积产率 {target:g}% 超出可达范围：从 {start:g} ℃ "
                f"（累积回收 {r_start:g}%）出发，实测范围终点 {tmax:g} ℃ 处"
                f"累积回收 {r_max:g}%，最多只能切出 {max_yield:.4f}%。"
                "不外推高温段，请降低目标或补充更高温度的试验点",
            )
        )

    # ---- 在 [start, T_max] 上解 R(T) = R(start) + target ----
    level = min(r_start + max(target, 0.0), r_max)
    t_first = _first_reaching(curve, level, start, tmax)
    t_last = _last_at_level(curve, level, t_first, tmax)

    plateau = None
    if t_last - t_first > _PLATEAU_MIN_WIDTH_C:
        plateau = {
            "from_temp_c": round(t_first, 4),
            "to_temp_c": round(t_last, 4),
            "note": (
                f"目标产率落在曲线平台段：{t_first:.4g} ~ {t_last:.4g} ℃ 之间"
                f"累积回收恒为 {level:g}%，该区间内任意终止切点产率相同（多解）；"
                f"按稳定规则取最早可用温度 {t_first:.4g} ℃"
            ),
        }
        issues.append(
            Issue(
                "plateau_multiple_solutions",
                Severity.INFO,
                plateau["note"],
            )
        )

    # 建议温度保留 4 位小数（0.0001 ℃，远低于试验测温精度；同时吸收掉
    # 平台边缘 float64 求值的 ~1e-7 ℃ 舍入噪声），实际产率在该温度上复核
    t_suggest = round(t_first, 4)
    actual = float(curve.pchip(t_suggest)) - r_start

    result.update(
        ok=True,
        suggested_end_temp_c=t_suggest,
        actual_yield_pct=round(actual, 4),
        recovery_at_end_pct=round(float(curve.pchip(t_suggest)), 4),
        plateau=plateau,
    )
    result["issues"] = [i.as_dict() for i in issues]
    return result
