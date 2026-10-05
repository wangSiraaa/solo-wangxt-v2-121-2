import { useEffect, useState } from "react";
import { api } from "../api";
import type { CutInput, SolveEndTempResult } from "../types";
import { g } from "../format";

interface Props {
  expId: number;
  cuts: CutInput[];
  /** 求解成功：把建议终止切点应用到指定馏分（仅更新编辑器预览，不落库） */
  onApply: (cutIndex: number, endTempC: number) => void;
}

function fmt(v: number | null | undefined, d = 2) {
  return v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(d);
}

/**
 * 按目标体积产率反推终止切点。
 * 求解成功即把建议温度写入对应馏分（触发整体预览重算），
 * 是否保存仍由用户通过“保存方案”决定；求解失败时方案保持不变。
 */
export default function SolveEndTempPanel({ expId, cuts, onApply }: Props) {
  const [sel, setSel] = useState(0);
  const [target, setTarget] = useState("");
  const [result, setResult] = useState<SolveEndTempResult | null>(null);
  const [applied, setApplied] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [solving, setSolving] = useState(false);

  useEffect(() => {
    setResult(null);
    setApplied(null);
    setError(null);
    setSel(0);
  }, [expId]);

  if (cuts.length === 0) return null;
  const idx = Math.min(sel, cuts.length - 1);
  const cut = cuts[idx];

  const solve = async () => {
    const t = parseFloat(target);
    if (!Number.isFinite(t)) {
      setError("请输入目标体积产率（数字，进料体积基准 %）");
      return;
    }
    setSolving(true);
    setError(null);
    setApplied(null);
    try {
      const r = await api.solveEndTemp(expId, {
        start_temp_c: cut.start_temp_c,
        target_yield_pct: t,
      });
      setResult(r);
      if (r.ok && r.suggested_end_temp_c !== null) {
        // 应用建议切点 -> 触发 evaluate 预览（全部馏分 + 物料平衡），不落库
        onApply(idx, r.suggested_end_temp_c);
        setApplied(`已把「${cut.name}」终馏点设为 ${g(r.suggested_end_temp_c)} ℃（预览中，保存后生效）`);
      }
      // 求解被拒绝时不改动任何切点，原方案保持不变
    } catch (e) {
      setError((e as Error).message);
      setResult(null);
    } finally {
      setSolving(false);
    }
  };

  return (
    <div className="solver">
      <h3>按目标产率求终点（体积基准反推，仅实测范围内求解）</h3>
      <div className="row">
        <label>馏分（起始温度取自方案）
          <select value={idx} onChange={(e) => { setSel(parseInt(e.target.value)); setResult(null); setApplied(null); }}>
            {cuts.map((c, i) => (
              <option key={i} value={i}>
                {i + 1}. {c.name}（{g(c.start_temp_c)} ℃ 起）
              </option>
            ))}
          </select>
        </label>
        <label>目标体积产率 %（进料体积基准，非质量%）
          <input
            type="number" step="0.1" min="0" max="100"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="如 12"
          />
        </label>
        <button className="btn primary" onClick={solve} disabled={solving}>
          {solving ? "求解中…" : "反推终止切点"}
        </button>
      </div>

      {error && <div className="solve-msg bad">⛔ {error}</div>}

      {result && (
        <div className={`solve-msg ${result.ok ? "ok" : "bad"}`}>
          {result.achievable_yield_pct && (
            <div>
              可达体积产率范围：{fmt(result.achievable_yield_pct[0])}% ~{" "}
              {fmt(result.achievable_yield_pct[1])}%
              （起点 {g(result.start_temp_c)} ℃ 处累积回收 {fmt(result.recovery_at_start_pct)}%，
              实测终点处可达上限，不外推）
            </div>
          )}
          {result.ok ? (
            <>
              <div>
                ✅ 建议终馏点 <b>{g(result.suggested_end_temp_c)} ℃</b>
                （此处累积回收 {fmt(result.recovery_at_end_pct)}%），
                实际体积产率 <b>{fmt(result.actual_yield_pct)}%</b> ≈ 目标{" "}
                {g(result.target_yield_pct)}%
              </div>
              {result.plateau && (
                <div className="plateau-note">ℹ️ {result.plateau.note}</div>
              )}
              {applied && <div className="applied-note">{applied}</div>}
            </>
          ) : (
            result.issues.map((it, i) => <div key={i}>⛔ {it.message}</div>)
          )}
        </div>
      )}
      <p className="hint">
        求解只使用 PCHIP 曲线在实测温度范围内的部分：目标超出可达范围会明确拒绝且方案不变；
        平台段多解时取最早可用温度。应用建议后下方结果区即为全部馏分与物料平衡的预览，
        确认无误再点“保存方案”。
      </p>
    </div>
  );
}
