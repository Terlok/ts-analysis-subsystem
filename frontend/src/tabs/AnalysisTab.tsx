// Off-stream analysis preview (ФВ-3.5): run the diagnostic pipeline over the visible interval
// with other parameters, without persisting anything, and overlay the result on the chart.
import { useState } from "react";
import { api, errorText } from "../api/client";
import { clearPreview, maeText, runPreview } from "../lib/preview";
import type { AnalysisPreviewResponse } from "../api/types";
import { engine } from "../lib/engine";
import { fmtDateTime, fmtDuration, fmtNum, usToS } from "../lib/format";
import { useAppData, useEngine, usePolling } from "../lib/hooks";
import type { Workspace } from "../lib/workspaces";

export function AnalysisTab({ ws }: { ws: Workspace }) {
  const snap = useEngine();
  const { channels } = useAppData();
  const [channel, setChannel] = useState(ws.channels[0] ?? "");
  const ch = ws.channels.includes(channel) ? channel : (ws.channels[0] ?? "");
  const reg = channels.find((c) => c.id === ch);
  const [params, setParams] = useState({ hampel_window: "", hampel_kappa: "", feature_window: "", residual_k: "" });
  // "active" | "naive" | model version id
  const [forecast, setForecast] = useState<string>("active");
  const models = usePolling(() => api.models(ch), 0, [ch], !!ch);
  const [res, setRes] = useState<AnalysisPreviewResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const num = (s: string) => (s.trim() === "" ? null : Number(s));
  const run = async () => {
    const [a, b] = engine.getSnapshot().range;
    setBusy(true);
    setErr(null);
    try {
      const r = await runPreview({
        channel: ch,
        t_from: Math.floor(a * 1e6),
        t_to: Math.ceil(b * 1e6),
        hampel_window: num(params.hampel_window),
        hampel_kappa: num(params.hampel_kappa),
        feature_window: num(params.feature_window),
        residual_k: num(params.residual_k),
        use_model: forecast !== "naive",
        model_id: forecast !== "naive" && forecast !== "active" ? Number(forecast) : null,
      });
      setRes(r);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  if (!ws.channels.length) return <p className="muted pad">Немає датасетів.</p>;
  const field = (k: keyof typeof params, label: string, hint: string, def: number | null | undefined) => (
    <label title={hint}>
      {label}
      <input className="input" type="number" placeholder={def != null ? String(def) : "за замовч."} value={params[k]} onChange={(e) => setParams({ ...params, [k]: e.target.value })} />
    </label>
  );

  return (
    <div className="split">
      <div className="pane side wide">
        <div className="form">
          <label className="wide">
            Канал
            <select className="input" value={ch} onChange={(e) => setChannel(e.target.value)}>
              {ws.channels.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          {field("hampel_window", "w Гампеля", "Ширина причинного вікна фільтра Гампеля", reg?.hampel_window)}
          {field("hampel_kappa", "κ Гампеля", "Поріг заміни в σ (типово 3)", reg?.hampel_kappa)}
          {field("feature_window", "Вікно ознак", "Кількість відліків для віконних ознак", reg?.feature_window)}
          {field("residual_k", "k залишку", "Поріг ε = k·σr для залишку прогнозу", reg?.residual_k)}
          <label className="wide" title="Модель, яка прогнозує наступне значення (залишковий критерій)">
            Прогноз
            <select className="input" value={forecast} onChange={(e) => setForecast(e.target.value)}>
              <option value="active">активна модель каналу (або наївний, якщо її немає)</option>
              <option value="naive">наївний: xₜ₊₁ = xₜ</option>
              {(models.data ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  #{m.id} {m.version}
                  {m.active ? " (активна)" : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="row wide">
            <button className="btn primary" onClick={run} disabled={busy}>
              {busy ? "Аналіз…" : "Аналізувати видимий інтервал"}
            </button>
            <button
              className="btn"
              onClick={() => {
                setRes(null);
                clearPreview();
              }}
            >
              Очистити
            </button>
          </div>
          <p className="muted small wide">
            {fmtDateTime(snap.range[0])} — {fmtDateTime(snap.range[1])}. Прогноз показується на графіку фіолетовим пунктиром, позначки —
            фіолетовими ромбами; нічого не зберігається.
          </p>
          {err && <span className="err wide">{err}</span>}
        </div>
      </div>
      <div className="pane grow">
        {res && (
          <>
            <div className="toolbar">
              <b>{res.channel}</b>
              <span>точок: {res.n.toLocaleString("uk-UA")}</span>
              <span>замінено: {res.substituted}</span>
              <span>аномальних: {res.anomalies}</span>
              <span>модель: {res.model}</span>
              <span className="muted">{res.elapsed_ms.toFixed(0)} мс</span>
              {res.mae_model != null && <span>{maeText(res.mae_model, res.mae_naive)}</span>}
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th>Тип</th>
                  <th>Початок</th>
                  <th>Тривалість</th>
                  <th>Точок</th>
                  <th>Пік</th>
                  <th>Оцінка</th>
                </tr>
              </thead>
              <tbody>
                {res.episodes.map((e, i) => (
                  <tr key={i} className="clickable" onClick={() => engine.zoomTo(usToS(e.ts_start) - 60, usToS(e.ts_end) + 60)}>
                    <td>
                      <span className={`pill ${e.kind}`}>{e.kind === "anomaly" ? "аномалія" : "викид"}</span>
                    </td>
                    <td>{fmtDateTime(usToS(e.ts_start))}</td>
                    <td>{fmtDuration(usToS(e.ts_end - e.ts_start))}</td>
                    <td>{e.n_points}</td>
                    <td>{fmtNum(e.peak_value, 3)}</td>
                    <td>{fmtNum(e.peak_score, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}
