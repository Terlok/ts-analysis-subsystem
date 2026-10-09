// Events layer: anomaly and outlier episodes; click to open the incident context (scenario С2).
import { useState } from "react";
import { api } from "../api/client";
import type { EventItem } from "../api/types";
import { engine } from "../lib/engine";
import { fmtDateTime, fmtDuration, fmtNum, usToS } from "../lib/format";
import { useAppData, useEngine, usePolling } from "../lib/hooks";
import type { Workspace } from "../lib/workspaces";

export function EventsTab({ ws }: { ws: Workspace }) {
  const snap = useEngine();
  const { colorOf } = useAppData();
  const [scope, setScope] = useState<"view" | "all">("view");
  const [kind, setKind] = useState("");
  const [run, setRun] = useState("online");
  const [selected, setSelected] = useState<EventItem | null>(null);

  // In live mode the range moves every second: poll instead of refetching on every change.
  const rangeKey = snap.mode === "archive" && scope === "view" ? `${Math.round(snap.range[0])}-${Math.round(snap.range[1])}` : snap.mode;
  const { data, error } = usePolling(
    () => {
      const [a, b] = engine.getSnapshot().range;
      return api.events({
        channels: ws.channels,
        from: scope === "view" ? a * 1e6 : undefined,
        to: scope === "view" ? b * 1e6 : undefined,
        kind: kind || undefined,
        run,
        limit: 500,
      });
    },
    snap.mode === "live" ? 5000 : 0,
    [ws.channels.join(), scope, kind, run, rangeKey],
    ws.channels.length > 0,
  );

  const open = async (e: EventItem) => {
    setSelected(await api.event(e.id));
    const t0 = usToS(e.ts_start);
    const t1 = usToS(e.ts_end);
    const pad = Math.max(30, (t1 - t0) * 2);
    engine.zoomTo(t0 - pad, t1 + pad);
  };

  if (!ws.channels.length) return <p className="muted pad">Немає датасетів.</p>;
  return (
    <div className="split">
      <div className="pane grow">
        <div className="toolbar">
          <span className="segmented">
            <button className={`seg ${scope === "view" ? "active" : ""}`} onClick={() => setScope("view")}>
              У видимому інтервалі
            </button>
            <button className={`seg ${scope === "all" ? "active" : ""}`} onClick={() => setScope("all")}>
              Усі
            </button>
          </span>
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">усі типи</option>
            <option value="anomaly">аномалії</option>
            <option value="outlier">викиди (Гампель)</option>
          </select>
          <input className="input" value={run} onChange={(e) => setRun(e.target.value)} title="online — потокова діагностика; інше — id повторного аналізу" style={{ width: 110 }} />
          <span className="muted">{data ? `${data.length} подій` : ""}</span>
          {error && <span className="err">{error}</span>}
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Канал</th>
              <th>Тип</th>
              <th>Початок</th>
              <th>Тривалість</th>
              <th>Точок</th>
              <th>Пік</th>
              <th>Оцінка</th>
              <th>Модель</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((e) => (
              <tr key={e.id} className={`clickable ${selected?.id === e.id ? "sel" : ""}`} onClick={() => void open(e)} title="Показати контекст події на графіку">
                <td>
                  <i className="swatch" style={{ background: colorOf(e.channel_id) }} /> {e.channel_id}
                </td>
                <td>
                  <span className={`pill ${e.kind}`}>{e.kind === "anomaly" ? "аномалія" : e.kind === "outlier" ? "викид" : e.kind}</span>
                </td>
                <td>{fmtDateTime(usToS(e.ts_start))}</td>
                <td>{fmtDuration(usToS(e.ts_end - e.ts_start))}</td>
                <td>{e.n_points}</td>
                <td>{fmtNum(e.peak_value, 3)}</td>
                <td>{fmtNum(e.score, 2)}</td>
                <td className="muted">{e.model_version ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected && (
        <div className="pane side">
          <div className="toolbar">
            <b>Подія #{selected.id}</b>
            <button className="btn" onClick={() => setSelected(null)}>
              ×
            </button>
          </div>
          <dl className="kv">
            <dt>Канал</dt>
            <dd>{selected.channel_id}</dd>
            <dt>Тип</dt>
            <dd>{selected.kind}</dd>
            <dt>Інтервал</dt>
            <dd>
              {fmtDateTime(usToS(selected.ts_start), true)} — {fmtDateTime(usToS(selected.ts_end), true)}
            </dd>
            <dt>Пік</dt>
            <dd>
              {fmtNum(selected.peak_value, 4)} о {fmtDateTime(usToS(selected.peak_ts), true)}
            </dd>
            <dt>Запуск</dt>
            <dd>{selected.run}</dd>
          </dl>
          <pre className="json">{JSON.stringify(selected.details, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
