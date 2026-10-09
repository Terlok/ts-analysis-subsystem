// Forecasting models of the residual criterion: training on the archive, versions, activation.
import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";
import type { ModelVersion } from "../api/types";
import { engine } from "../lib/engine";
import { fmtDateTime, usToS } from "../lib/format";
import { useAppData, useEngine, usePolling } from "../lib/hooks";
import type { Workspace } from "../lib/workspaces";

type Scope = "all" | "view" | "before";

const fmtMetric = (v: unknown, digits = 3) => (typeof v === "number" ? v.toPrecision(digits) : "—");

function gain(m: Record<string, unknown> | null): string {
  const g = m?.mae_gain;
  return typeof g === "number" ? `${(g * 100).toFixed(1)}%` : "—";
}

export function ModelsTab({ ws }: { ws: Workspace }) {
  const snap = useEngine();
  const { channels } = useAppData();
  const [onlyChart, setOnlyChart] = useState(true);
  const [channel, setChannel] = useState(ws.channels[0] ?? "");
  const [scope, setScope] = useState<Scope>("all");
  const [trees, setTrees] = useState(100);
  const [depth, setDepth] = useState(6);
  const [activate, setActivate] = useState<"auto" | "always" | "never">("auto");
  const [err, setErr] = useState<string | null>(null);

  const models = usePolling(() => api.models(), 0);
  const jobs = usePolling(() => api.trainingJobs(), 2000);
  const running = (jobs.data ?? []).some((j) => j.status === "running" || j.status === "queued");
  const doneCount = (jobs.data ?? []).filter((j) => j.status === "done").length;
  const reloadModels = models.reload;
  useEffect(() => reloadModels(), [doneCount, reloadModels]); // a finished job adds a version

  const ch = channel || ws.channels[0] || channels[0]?.id || "";
  const train = async () => {
    setErr(null);
    const [a, b] = engine.getSnapshot().range;
    const range =
      scope === "view" ? { t_from: Math.floor(a * 1e6), t_to: Math.ceil(b * 1e6) } : scope === "before" ? { t_to: Math.floor(a * 1e6) } : {};
    try {
      await api.trainModel({ channel: ch, trees, depth, activate, ...range });
      jobs.reload();
    } catch (e) {
      setErr(errorText(e));
    }
  };

  const list: ModelVersion[] = (models.data ?? []).filter((m) => !onlyChart || ws.channels.includes(m.channel_id));
  const channelOptions = Array.from(new Set([...ws.channels, ...channels.map((c) => c.id)]));

  return (
    <div className="split">
      <div className="pane side wide">
        <div className="form">
          <b className="wide">Навчити прогнозну модель</b>
          <label className="wide">
            Канал
            <select className="input" value={ch} onChange={(e) => setChannel(e.target.value)}>
              {channelOptions.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="wide">
            Дані для навчання
            <select className="input" value={scope} onChange={(e) => setScope(e.target.value as Scope)}>
              <option value="all">увесь архів каналу</option>
              <option value="view">видимий інтервал графіка</option>
              <option value="before">архів до початку видимого інтервалу</option>
            </select>
          </label>
          <label title="Кількість дерев ансамблю (стаття: 100)">
            Дерев
            <input className="input" type="number" min={10} max={1000} value={trees} onChange={(e) => setTrees(Number(e.target.value))} />
          </label>
          <label title="Максимальна глибина дерева (стаття: 6)">
            Глибина
            <input className="input" type="number" min={2} max={16} value={depth} onChange={(e) => setDepth(Number(e.target.value))} />
          </label>
          <label className="wide" title="Модель, гірша за наївний прогноз, лише погіршить діагностику">
            Активація
            <select className="input" value={activate} onChange={(e) => setActivate(e.target.value as typeof activate)}>
              <option value="auto">якщо точніша за наївний прогноз</option>
              <option value="always">завжди</option>
              <option value="never">лише зареєструвати</option>
            </select>
          </label>
          <div className="row wide">
            <button className="btn primary" onClick={train} disabled={!ch}>
              Навчити
            </button>
            {running && <span className="muted">навчання триває…</span>}
            {err && <span className="err">{err}</span>}
          </div>
          <p className="muted small wide">
            Фільтр Гампеля → віконні ознаки → ансамбль дерев прогнозує приріст наступного значення. Вибірка ділиться в часі 60/20/20 з
            проміжками; поріг ε = k·σr береться з валідаційної частини. Нова версія активується, воркери аналітики підхоплюють її
            автоматично. Без моделі використовується наївний прогноз xₜ₊₁ = xₜ.
            {scope !== "all" && ` Інтервал: ${fmtDateTime(snap.range[0])} — ${fmtDateTime(snap.range[1])}.`}
          </p>
        </div>
        {(jobs.data ?? []).length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Завдання</th>
                <th>Канал</th>
                <th>Стан</th>
                <th>Результат</th>
              </tr>
            </thead>
            <tbody>
              {(jobs.data ?? []).slice(0, 8).map((j) => (
                <tr key={j.id} className={j.status === "error" ? "row-bad" : ""}>
                  <td>{fmtDateTime(j.created_at)}</td>
                  <td>{j.channel}</td>
                  <td>{{ queued: "у черзі", running: "навчання…", done: "готово", error: "помилка" }[j.status]}</td>
                  <td className="small" title={j.error ?? undefined}>
                    {j.result
                      ? `#${j.result.model_id}, MAE ${fmtMetric(j.result.metrics.mae_test)} проти ${fmtMetric(j.result.metrics.mae_test_naive)}, ${j.result.metrics.n_points.toLocaleString("uk-UA")} точок`
                      : (j.error ?? "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="pane grow">
        <div className="toolbar">
          <label className="toggle">
            <input type="checkbox" checked={onlyChart} onChange={(e) => setOnlyChart(e.target.checked)} />
            лише канали графіка
          </label>
          {(models.error || err) && <span className="err">{models.error || err}</span>}
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>#</th>
              <th>Канал</th>
              <th>Версія</th>
              <th>Дані</th>
              <th>MAE тест / наївний</th>
              <th>Виграш</th>
              <th>σr</th>
              <th>мкс/точку</th>
              <th>Стан</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((m) => {
              const mt = (m.metrics ?? {}) as Record<string, number>;
              return (
                <tr key={m.id}>
                  <td>{m.id}</td>
                  <td>{m.channel_id}</td>
                  <td>{m.version}</td>
                  <td className="small">
                    {mt.t_from ? `${fmtDateTime(usToS(mt.t_from))} — ${fmtDateTime(usToS(mt.t_to))}` : ""}
                    {mt.n_points ? ` (${mt.n_points.toLocaleString("uk-UA")})` : ""}
                  </td>
                  <td>
                    {fmtMetric(mt.mae_test)} / {fmtMetric(mt.mae_test_naive)}
                  </td>
                  <td>{gain(m.metrics)}</td>
                  <td>{fmtMetric(mt.sigma_r)}</td>
                  <td>{mt.batch_inference_us_per_point ?? "—"}</td>
                  <td>{m.active ? <span className="pill ok">активна</span> : ""}</td>
                  <td>
                    {!m.active && (
                      <button
                        className="btn"
                        onClick={async () => {
                          try {
                            await api.activateModel(m.id);
                            models.reload();
                          } catch (e) {
                            setErr(errorText(e));
                          }
                        }}
                      >
                        Активувати
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {list.length === 0 && (
              <tr>
                <td colSpan={10} className="muted">
                  Зареєстрованих моделей немає — навчіть першу ліворуч.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
