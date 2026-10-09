// Off-stream analysis (ФВ-3.5): run the diagnostic pipeline over the visible interval with
// other parameters and compare up to two forecasting models side by side. Nothing is stored;
// forecasts and flagged points of both models are overlaid on the chart in their colors.
import { useState } from "react";
import { api, errorText } from "../api/client";
import type { AnalysisPreviewResponse, ModelVersion } from "../api/types";
import { engine } from "../lib/engine";
import { fmtDateTime, fmtDuration, fmtNum, usToS } from "../lib/format";
import { useAppData, useEngine, usePolling } from "../lib/hooks";
import { PREVIEW_COLORS, clearPreview, gainPercent, toLayer } from "../lib/preview";
import type { Workspace } from "../lib/workspaces";

// "" (none, for B) | "active" | "naive" | model version id
type Choice = string;

interface Result {
  key: "A" | "B";
  label: string;
  color: string;
  r: AnalysisPreviewResponse;
}

function choiceLabel(c: Choice, models: ModelVersion[]): string {
  if (c === "active") return "активна модель";
  if (c === "naive") return "наївний прогноз";
  const m = models.find((x) => String(x.id) === c);
  return m ? `#${m.id} ${m.version}` : c;
}

function ModelSelect({ value, onChange, models, allowNone }: { value: Choice; onChange: (v: Choice) => void; models: ModelVersion[]; allowNone?: boolean }) {
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      {allowNone && <option value="">— не порівнювати —</option>}
      <option value="active">активна модель каналу (або наївний, якщо її немає)</option>
      <option value="naive">наївний: xₜ₊₁ = xₜ</option>
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          #{m.id} {m.version}
          {m.active ? " (активна)" : ""}
        </option>
      ))}
    </select>
  );
}

export function AnalysisTab({ ws }: { ws: Workspace }) {
  const snap = useEngine();
  const { channels } = useAppData();
  const [channel, setChannel] = useState(ws.channels[0] ?? "");
  const ch = ws.channels.includes(channel) ? channel : (ws.channels[0] ?? "");
  const reg = channels.find((c) => c.id === ch);
  const [params, setParams] = useState({ hampel_window: "", hampel_kappa: "", feature_window: "", residual_k: "" });
  const [choiceA, setChoiceA] = useState<Choice>("active");
  const [choiceB, setChoiceB] = useState<Choice>("");
  const [results, setResults] = useState<Result[]>([]);
  const [shown, setShown] = useState<"A" | "B">("A");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const models = usePolling(() => api.models(ch), 0, [ch], !!ch);
  const modelList = models.data ?? [];

  const num = (s: string) => (s.trim() === "" ? null : Number(s));
  const run = async () => {
    const [a, b] = engine.getSnapshot().range;
    setBusy(true);
    setErr(null);
    const base = {
      channel: ch,
      t_from: Math.floor(a * 1e6),
      t_to: Math.ceil(b * 1e6),
      hampel_window: num(params.hampel_window),
      hampel_kappa: num(params.hampel_kappa),
      feature_window: num(params.feature_window),
      residual_k: num(params.residual_k),
    };
    const jobs = ([["A", choiceA], ["B", choiceB]] as const).filter(([, c]) => c !== "");
    try {
      const out = await Promise.all(
        jobs.map(async ([key, c], i) => {
          const r = await api.analysisPreview({
            ...base,
            use_model: c !== "naive",
            model_id: c !== "naive" && c !== "active" ? Number(c) : null,
          });
          return { key, label: `${key}: ${choiceLabel(c, modelList)}`, color: PREVIEW_COLORS[i], r } as Result;
        }),
      );
      setResults(out);
      setShown("A");
      engine.setPreviews(out.map((x) => toLayer(x.r, x.key, x.label, x.color)));
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
  const current = results.find((x) => x.key === shown) ?? results[0];

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
          <label className="wide">
            <span>
              <i className="swatch" style={{ background: PREVIEW_COLORS[0] }} /> Прогноз A
            </span>
            <ModelSelect value={choiceA} onChange={setChoiceA} models={modelList} />
          </label>
          <label className="wide">
            <span>
              <i className="swatch" style={{ background: PREVIEW_COLORS[1] }} /> Прогноз B (для порівняння)
            </span>
            <ModelSelect value={choiceB} onChange={setChoiceB} models={modelList} allowNone />
          </label>
          {field("hampel_window", "w Гампеля", "Ширина причинного вікна фільтра Гампеля", reg?.hampel_window)}
          {field("hampel_kappa", "κ Гампеля", "Поріг заміни в σ (типово 3)", reg?.hampel_kappa)}
          {field("feature_window", "Вікно ознак", "Кількість відліків для віконних ознак", reg?.feature_window)}
          {field("residual_k", "k залишку", "Поріг ε = k·σr для залишку прогнозу", reg?.residual_k)}
          <div className="row wide">
            <button className="btn primary" onClick={run} disabled={busy}>
              {busy ? "Аналіз…" : "Аналізувати видимий інтервал"}
            </button>
            <button
              className="btn"
              onClick={() => {
                setResults([]);
                clearPreview();
              }}
            >
              Очистити
            </button>
          </div>
          <p className="muted small wide">
            {fmtDateTime(snap.range[0])} — {fmtDateTime(snap.range[1])}. Прогноз кожної моделі малюється пунктиром її кольору, позначені нею
            точки — ромбами; параметри фільтра й порогу однакові для обох. Нічого не зберігається.
          </p>
          {err && <span className="err wide">{err}</span>}
        </div>
      </div>
      <div className="pane grow">
        {results.length > 0 && (
          <>
            <table className="table">
              <thead>
                <tr>
                  <th>Прогноз</th>
                  <th title="середня абсолютна похибка прогнозу наступного значення (на відфільтрованих значеннях)">MAE</th>
                  <th>MAE наївного</th>
                  <th>Виграш</th>
                  <th>Аномальних точок</th>
                  <th>Епізодів аномалій</th>
                  <th>Замінено</th>
                  <th>Час</th>
                </tr>
              </thead>
              <tbody>
                {results.map(({ key, label, color, r }) => {
                  const g = gainPercent(r.mae_model, r.mae_naive);
                  return (
                    <tr key={key} className={`clickable ${current?.key === key ? "sel" : ""}`} onClick={() => setShown(key)} title="Показати епізоди цього прогнозу">
                      <td>
                        <i className="swatch" style={{ background: color }} /> {label}
                        <span className="muted small"> ({r.model})</span>
                      </td>
                      <td>{fmtNum(r.mae_model, 4)}</td>
                      <td>{fmtNum(r.mae_naive, 4)}</td>
                      <td className={g != null && g > 0 ? "good" : ""}>{g != null ? `${g.toFixed(1)}%` : "—"}</td>
                      <td>
                        {r.anomalies} ({((r.anomalies / Math.max(r.n, 1)) * 100).toFixed(2)}%)
                      </td>
                      <td>{r.episodes.filter((e) => e.kind === "anomaly").length}</td>
                      <td>{r.substituted}</td>
                      <td className="muted">{r.elapsed_ms.toFixed(0)} мс</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {current && (
              <>
                <div className="toolbar">
                  <b>
                    Епізоди: {current.label}
                  </b>
                  <span className="muted">{current.r.n.toLocaleString("uk-UA")} точок</span>
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
                    {current.r.episodes.map((e, i) => (
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
          </>
        )}
      </div>
    </div>
  );
}
