import { useEffect, useState } from "react";
import { fmtDateTime, fmtNum, usToS } from "../lib/format";
import { useAppData, useEngine } from "../lib/hooks";
import { workspaces, type Workspace } from "../lib/workspaces";

function LimitInput({ value, onCommit, placeholder }: { value: number | null; onCommit: (v: number | null) => void; placeholder: string }) {
  const [text, setText] = useState(value === null ? "" : String(value));
  useEffect(() => setText(value === null ? "" : String(value)), [value]);
  const commit = () => {
    const t = text.trim().replace(",", ".");
    if (t === "") onCommit(null);
    else if (Number.isFinite(Number(t))) onCommit(Number(t));
    else setText(value === null ? "" : String(value));
  };
  return (
    <input
      className="input"
      value={text}
      placeholder={placeholder}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
    />
  );
}

export function StatsTab({ ws }: { ws: Workspace }) {
  const snap = useEngine();
  const { channels, states, colorOf } = useAppData();
  if (!ws.channels.length) return <p className="muted pad">Додайте канал у рядку над графіком, щоб побачити його статистику.</p>;

  const setLimit = (ch: string, idx: 0 | 1, v: number | null) => {
    const cur = ws.limits[ch] ?? [null, null];
    const next: [number | null, number | null] = idx === 0 ? [v, cur[1]] : [cur[0], v];
    workspaces.update(ws.id, { limits: { ...ws.limits, [ch]: next } });
  };

  return (
    <div className="cards">
      {ws.channels.map((ch) => {
        const meta = snap.meta[ch];
        const reg = channels.find((c) => c.id === ch);
        const st = states[ch];
        const live = snap.last[ch];
        const last = live && (!st?.last_ts || live.t >= usToS(st.last_ts)) ? live : st?.last_ts ? { t: usToS(st.last_ts), v: st.last_val ?? NaN } : null;
        const lim = ws.limits[ch] ?? [null, null];
        const unit = reg?.unit ? ` ${reg.unit}` : "";
        return (
          <div className="card" key={ch} style={{ borderTopColor: colorOf(ch) }}>
            <div className="card-head">
              <i className="swatch" style={{ background: colorOf(ch) }} />
              <b title={reg?.description ?? undefined}>{reg?.name && reg.name !== ch ? `${reg.name} (${ch})` : ch}</b>
              <button className="x" title="Прибрати" onClick={() => workspaces.update(ws.id, { channels: ws.channels.filter((c) => c !== ch) })}>
                ⊗
              </button>
            </div>
            <div className="label">Межі:</div>
            <div className="limits">
              <LimitInput value={lim[0]} placeholder="авто" onCommit={(v) => setLimit(ch, 0, v)} />
              <LimitInput value={lim[1]} placeholder="авто" onCommit={(v) => setLimit(ch, 1, v)} />
            </div>
            <dl className="kv">
              <dt>Мін:</dt>
              <dd>{fmtNum(meta?.vmin, 2)}{unit}</dd>
              <dt>Макс:</dt>
              <dd>{fmtNum(meta?.vmax, 2)}{unit}</dd>
              <dt>Сер:</dt>
              <dd>{fmtNum(meta?.mean, 2)}{unit}</dd>
              <dt>Точок:</dt>
              <dd title="Вимірювань в інтервалі / точок на графіку">
                {meta ? `${meta.n.toLocaleString("uk-UA")} / ${meta.shown.toLocaleString("uk-UA")}` : "—"}
              </dd>
              <dt>Джерело:</dt>
              <dd title="hot — гаряче вікно Redis, raw — архів, agg — агрегати рівня ℓ">
                {meta ? `${meta.source}${meta.level !== null ? ` ℓ=${meta.level}` : ""} · η=${(meta.eta * 100).toFixed(1)}%` : "—"}
              </dd>
            </dl>
            <div className="kv last">
              <span>Останнє:</span>
              <b>{last ? `${fmtNum(last.v, 2)}${unit}` : "—"}</b>
            </div>
            <div className="muted small">{last ? fmtDateTime(last.t) : ""}</div>
          </div>
        );
      })}
    </div>
  );
}
