import { useEffect, useState } from "react";
import { api } from "../api";
import type { CutInput, SolveEndResult } from "../types";
import { g } from "../format";

interface Props {
  expId: number;
  cuts: CutInput[];
  basis: "volume" | "mass";
  /** 把建议终点填入编辑器（仅改编辑器状态触发预览重算，不保存） */
  onApply: (cutIndex: number, endTemp: number) => void;
  /** 在曲线图上显示/清除建议终点预览线 */
  onPreview: (temp: number | null) => void;
}

/**
 * 按目标产率求终点：已知馏分起点温度与目标体积产率，反推终馏切点。
 * 只在实测温度范围内求解；求解本身不落库，应用后由用户决定是否保存。
 */
export default function YieldSolver({ expId, cuts, basis, onApply, onPreview }: Props) {
  const [cutIdx, setCutIdx] = useState(0);
  const [target, setTarget] = useState("10");
  const [result, setResult] = useState<SolveEndResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [appliedNote, setAppliedNote] = useState<string | null>(null);

  // 卸载（切换试验）时清除图上的预览线
  useEffect(() => () => onPreview(null), [onPreview]);

  if (cuts.length === 0) return null;
  const idx = Math.min(cutIdx, cuts.length - 1);
  const cut = cuts[idx];
  const targetVal = parseFloat(target);
  const targetOk = Number.isFinite(targetVal) && targetVal >= 0 && targetVal <= 100;
  // 求解之后起点被改动 => 结果过期，要求重新求解
  const stale = result !== null && result.start_temp_c !== cut.start_temp_c;

  const reset = () => {
    setResult(null);
    setAppliedNote(null);
    setError(null);
    onPreview(null);
  };

  const solve = async () => {
    setBusy(true);
    setError(null);
    setAppliedNote(null);
    try {
      const r = await api.solveEnd(expId, {
        start_temp_c: cut.start_temp_c,
        target_yield_pct: targetVal,
      });
      setResult(r);
      onPreview(r.feasible ? r.suggested_end_temp_c : null);
    } catch (e) {
      setResult(null);
      onPreview(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const apply = () => {
    if (!result || result.suggested_end_temp_c === null) return;
    onApply(idx, result.suggested_end_temp_c);
    onPreview(null);
    setAppliedNote(
      `已把 ${g(result.suggested_end_temp_c)} ℃ 填入「${cut.name}」的终馏点（未保存）。` +
      "下方图表与各馏分产率、物料平衡已按新切点重算；确认无误后再点击“保存方案”。"
    );
  };

  return (
    <div className="solver">
      <h3>按目标产率求终点（反推终馏点，只在实测范围内求解）</h3>
      <div className="row">
        <label>馏分（起点温度取自该馏分初馏点）
          <select
            value={idx}
            onChange={(e) => { setCutIdx(parseInt(e.target.value)); reset(); }}
          >
            {cuts.map((c, i) => (
              <option key={i} value={i}>
                #{i + 1} {c.name}（起点 {g(c.start_temp_c)} ℃）
              </option>
            ))}
          </select>
        </label>
        <label>目标体积产率 %（进料体积基准）
          <input
            type="number" step="0.1" min="0" max="100" value={target}
            onChange={(e) => { setTarget(e.target.value); reset(); }}
          />
        </label>
        <button className="btn primary" onClick={solve} disabled={!targetOk || busy}>
          {busy ? "求解中…" : "求解终点"}
        </button>
      </div>

      {basis === "mass" && (
        <p className="solver-warn">
          当前产率口径为质量 %：本求解仍按<b>体积 %</b> 进行（曲线纵轴是累积回收体积百分比），
          质量 % 需经密度换算，<b>不能把质量百分比当作体积百分比输入</b>。
        </p>
      )}
      {error && <p className="solver-warn">求解失败：{error}</p>}

      {result && !result.feasible && (
        <div className="solver-result reject">
          <div><b>无法给出建议终点，原方案未改动。</b></div>
          {result.achievable_yield_pct && (
            <div>
              该馏分在实测范围 {g(result.applicable_range.temp_c[0])}~
              {g(result.applicable_range.temp_c[1])} ℃ 内的可达产率：
              {g(result.achievable_yield_pct[0])}% ~ {g(result.achievable_yield_pct[1])}%
            </div>
          )}
          <ul>{result.messages.map((m, i) => <li key={i}>{m}</li>)}</ul>
          {result.issues.map((it, i) => (
            <div key={i} className="solver-issue">[{it.code}] {it.message}</div>
          ))}
        </div>
      )}

      {result && result.feasible && (
        <div className="solver-result ok">
          <div className="headline">
            建议终馏点 <span className="big">{g(result.suggested_end_temp_c)} ℃</span>
            ，实际产率 <b>{g(result.actual_yield_pct)}%</b>（体积）
            {result.achievable_yield_pct && (
              <span className="range-note">
                可达范围 {g(result.achievable_yield_pct[0])}% ~ {g(result.achievable_yield_pct[1])}%
              </span>
            )}
          </div>
          {result.multiple_solutions && result.latest_end_temp_c !== null && (
            <div className="plateau-note">
              平台段多解：{g(result.suggested_end_temp_c)} ~ {g(result.latest_end_temp_c)} ℃
              内产率相同，已按“最早可用温度”规则取值。
            </div>
          )}
          <ul>{result.messages.map((m, i) => <li key={i}>{m}</li>)}</ul>
          {stale ? (
            <p className="solver-warn">
              「{cut.name}」的起点已改为 {g(cut.start_temp_c)} ℃，与求解时
              （{g(result.start_temp_c)} ℃）不同，请重新求解后再应用。
            </p>
          ) : (
            <button className="btn primary" onClick={apply}>
              应用到「{cut.name}」的终馏点（仅预览，保存由你决定）
            </button>
          )}
        </div>
      )}

      {appliedNote && <p className="applied-note">{appliedNote}</p>}
    </div>
  );
}
