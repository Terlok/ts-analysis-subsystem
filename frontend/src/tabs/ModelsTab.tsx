// Forecasting models of the residual criterion: training on the archive, versions, activation.
import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";
import type { ModelVersion } from "../api/types";
import { engine } from "../lib/engine";
import { fmtDateTime, fmtDuration, usToS } from "../lib/format";
import { useAppData, useEngine, usePolling } from "../lib/hooks";
import { maeText, runPreview } from "../lib/preview";
import { workspaces, type Workspace } from "../lib/workspaces";

type Scope = "all" | "view" | "before";

const fmtMetric = (v: unknown, digits = 3) => (typeof v === "number" ? v.toPrecision(digits) : "—");

/** Start of the test part (last 20% of the training data, after the gap) in seconds. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function testStart(mt: Record<string, any>): number | null {
  const sp = mt.split;
  if (!mt.t_from || !mt.t_to || !sp) return null;
  const total = sp.train + sp.val + sp.test + 2 * sp.gap;
  const frac = (sp.train + sp.val + 2 * sp.gap) / total;
  return usToS(mt.t_from + (mt.t_to - mt.t_from) * frac);
}

function ModelDetails({
  m,
  ws,
  onClose,
  onActivate,
  onDelete,
}: {
  m: ModelVersion;
  ws: Workspace;
  onClose: () => void;
  onActivate: () => void;
  onDelete: () => void;
}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mt = (m.metrics ?? {}) as Record<string, any>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const pr = (m.params ?? {}) as Record<string, any>;
  const [check, setCheck] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const t0 = testStart(mt);

  const show = async () => {
    if (t0 === null) return;
    const a = t0;
    const b = Math.min(t0 + 3600, usToS(mt.t_to));
    if (!ws.channels.includes(m.channel_id)) workspaces.update(ws.id, { channels: [...ws.channels, m.channel_id] });
    engine.zoomTo(a, b);
    setBusy(true);
    try {
      const r = await runPreview(
        { channel: m.channel_id, t_from: Math.floor(a * 1e6), t_to: Math.ceil(b * 1e6), use_model: true, model_id: m.id },
        `#${m.id} ${m.version}`,
      );
      setCheck(`${fmtDateTime(a)} — ${fmtDateTime(b)}: ${maeText(r.mae_model, r.mae_naive)}; аномальних точок ${r.anomalies}, замінено ${r.substituted}`);
    } catch (e) {
      setCheck(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const row = (k: string, v: React.ReactNode, hint?: string) => (
    <div className="kvrow" title={hint}>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </div>
  );
  return (
    <div className="model-card">
      <div className="toolbar">
        <b>
          Модель #{m.id} · {m.channel_id} · {m.version}
        </b>
        {m.active ? (
          <span className="pill ok">активна</span>
        ) : (
          <button className="btn" onClick={onActivate}>
            Активувати
          </button>
        )}
        <button className="btn primary" onClick={show} disabled={busy || t0 === null} title="Перша година тестової частини: дані, яких модель не бачила під час навчання">
          {busy ? "Обчислення…" : "Показати на графіку (тестова частина)"}
        </button>
        <button className="btn" onClick={onDelete} title="Видалити версію моделі та її файл">
          Видалити
        </button>
        <button className="btn" onClick={onClose}>
          ×
        </button>
      </div>
      {check && <p className="pad small">{check}</p>}
      <div className="model-grid">
        <dl className="kv">
          {row("Дані", mt.t_from ? `${fmtDateTime(usToS(mt.t_from))} — ${fmtDateTime(usToS(mt.t_to))}` : "—")}
          {row("Тривалість", mt.t_from ? fmtDuration(usToS(mt.t_to - mt.t_from)) : "—")}
          {row("Точок", mt.n_points?.toLocaleString("uk-UA") ?? "—")}
          {row(
            "Розбиття",
            mt.split ? `${mt.split.train.toLocaleString("uk-UA")} / ${mt.split.val.toLocaleString("uk-UA")} / ${mt.split.test.toLocaleString("uk-UA")}, проміжок ${mt.split.gap}` : "—",
            "навчальна / валідаційна / тестова частини у часі, з проміжками між ними",
          )}
          {row("Тест починається", t0 ? fmtDateTime(t0) : "—")}
          {row("Замінено Гампелем", mt.substituted_share != null ? `${(mt.substituted_share * 100).toFixed(2)}%` : "—")}
        </dl>
        <dl className="kv">
          {row("MAE на тесті", fmtMetric(mt.mae_test), "середня абсолютна похибка прогнозу наступного значення")}
          {row("MAE наївного", fmtMetric(mt.mae_test_naive), "xₜ₊₁ = xₜ")}
          {row("Виграш", gain(m.metrics))}
          {row("σr", fmtMetric(mt.sigma_r), "1.4826·MAD залишків на валідаційній частині")}
          {row("Поріг ε при k=3 / k=5", mt.sigma_r != null ? `${fmtMetric(3 * mt.sigma_r)} / ${fmtMetric(5 * mt.sigma_r)}` : "—")}
          {row("Навчання", mt.fit_s != null ? `${mt.fit_s} с` : "—")}
          {row("Інференс", mt.batch_inference_us_per_point != null ? `${mt.batch_inference_us_per_point} мкс/точку` : "—", "пакетний виклик")}
        </dl>
        <dl className="kv">
          {row("Алгоритм", ALGO_UA[pr.algorithm ?? "hgb"] ?? pr.algorithm)}
          {row("Дерев / глибина", `${pr.trees ?? "—"} / ${pr.depth ?? "—"}`)}
          {row("w, κ Гампеля", `${pr.hampel_window ?? "—"}, ${pr.hampel_kappa ?? "—"}`)}
          {row("Вікно ознак", pr.feature_window ?? "—")}
          {row("Ознаки", Array.isArray(pr.features) ? pr.features.join(", ") : "—")}
          {row("Ціль", pr.target === "increment" ? "приріст xₜ₊₁ − xₜ" : (pr.target ?? "—"))}
          {row("Файл", <span className="small">{m.path}</span>)}
        </dl>
      </div>
    </div>
  );
}

const ALGO_UA: Record<string, string> = {
  hgb: "градієнтний бустинг",
  rf: "випадковий ліс",
  ridge: "лінійна регресія (Ridge)",
};

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
  const [algorithm, setAlgorithm] = useState("hgb");
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  const remove = async (m: ModelVersion) => {
    const note = m.active ? "\nЦе активна модель: канал перейде на наївний прогноз." : "";
    if (!confirm(`Видалити модель #${m.id} (${m.channel_id}, ${m.version})?${note}`)) return;
    try {
      await api.deleteModel(m.id);
      if (selected === m.id) setSelected(null);
      models.reload();
    } catch (e) {
      setErr(errorText(e));
    }
  };

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
      await api.trainModel({ channel: ch, trees, depth, activate, algorithm, ...range });
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
          <label className="wide" title="Ансамблі дерев з дисертації та лінійна базова модель">
            Алгоритм
            <select className="input" value={algorithm} onChange={(e) => setAlgorithm(e.target.value)}>
              {Object.entries(ALGO_UA).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
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
              <option value="auto">якщо точніша за наївний прогноз і за поточну активну</option>
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
                  <td>
                    {j.channel} <span className="muted small">{j.algorithm}</span>
                  </td>
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
        {(() => {
          const sel = (models.data ?? []).find((x) => x.id === selected);
          return sel ? (
            <ModelDetails
              m={sel}
              ws={ws}
              onClose={() => setSelected(null)}
              onDelete={() => void remove(sel)}
              onActivate={async () => {
                try {
                  await api.activateModel(sel.id);
                  models.reload();
                } catch (e) {
                  setErr(errorText(e));
                }
              }}
            />
          ) : null;
        })()}
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
                <tr key={m.id} className={`clickable ${selected === m.id ? "sel" : ""}`} onClick={() => setSelected(m.id)} title="Відкрити картку моделі">
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
                        onClick={async (ev) => {
                          ev.stopPropagation();
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
                    <button
                      className="btn"
                      title="Видалити"
                      onClick={(ev) => {
                        ev.stopPropagation();
                        void remove(m);
                      }}
                    >
                      ✕
                    </button>
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
