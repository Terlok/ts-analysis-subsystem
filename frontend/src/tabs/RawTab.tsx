// Details on demand: primary values with quality codes and substitution marks, CSV export.
import { useState } from "react";
import { api, errorText } from "../api/client";
import type { FlagPoint, RawPoint } from "../api/types";
import { engine } from "../lib/engine";
import { QUALITY_UA, fmtDateTime, fmtNum, usToS } from "../lib/format";
import { useEngine } from "../lib/hooks";
import type { Workspace } from "../lib/workspaces";

const MAX_ROWS = 20_000;

export function RawTab({ ws }: { ws: Workspace }) {
  const snap = useEngine();
  const [channel, setChannel] = useState(ws.channels[0] ?? "");
  const [rows, setRows] = useState<RawPoint[] | null>(null);
  const [flags, setFlags] = useState<Map<number, FlagPoint>>(new Map());
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ch = ws.channels.includes(channel) ? channel : (ws.channels[0] ?? "");
  const [a, b] = snap.range;
  const n = snap.meta[ch]?.n ?? 0;

  const load = async () => {
    const [ra, rb] = engine.getSnapshot().range;
    setLoading(true);
    setErr(null);
    try {
      const [raw, fl] = await Promise.all([api.raw(ch, ra * 1e6, rb * 1e6), api.flags([ch], ra * 1e6, rb * 1e6)]);
      setRows(raw.slice(0, MAX_ROWS));
      setFlags(new Map(fl.map((f) => [f.ts, f])));
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setLoading(false);
    }
  };

  if (!ws.channels.length) return <p className="muted pad">Немає датасетів.</p>;
  return (
    <div className="pane grow">
      <div className="toolbar">
        <select className="input" value={ch} onChange={(e) => setChannel(e.target.value)}>
          {ws.channels.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <span className="muted">
          {fmtDateTime(a)} — {fmtDateTime(b)} · {n.toLocaleString("uk-UA")} вимірювань
        </span>
        <button className="btn" onClick={load} disabled={loading || n > MAX_ROWS} title={n > MAX_ROWS ? "Звузьте інтервал (масштабування на графіку)" : ""}>
          {loading ? "Завантаження…" : "Показати первинні дані"}
        </button>
        <a className="btn" href={api.rawCsvUrl(ch, a * 1e6, b * 1e6)} download>
          Експорт CSV
        </a>
        {n > MAX_ROWS && <span className="muted">для таблиці звузьте інтервал до {MAX_ROWS.toLocaleString("uk-UA")} точок</span>}
        {err && <span className="err">{err}</span>}
      </div>
      {rows && (
        <table className="table mono">
          <thead>
            <tr>
              <th>Час</th>
              <th>Значення</th>
              <th>Якість</th>
              <th>nd</th>
              <th>otkl</th>
              <th>Відфільтроване</th>
              <th>Прогноз</th>
              <th>Залишок / поріг</th>
              <th>Діагностика</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const f = flags.get(r.ts);
              return (
                <tr key={r.ts} className={f?.anomaly ? "row-anomaly" : r.substituted ? "row-subst" : r.quality === "bad" ? "row-bad" : ""}>
                  <td>{fmtDateTime(usToS(r.ts), true)}</td>
                  <td>{fmtNum(r.val, 4)}</td>
                  <td>{QUALITY_UA[r.quality] ?? r.quality}</td>
                  <td>{r.nd ? "так" : ""}</td>
                  <td>{r.otkl || ""}</td>
                  <td>{f ? fmtNum(f.val_f, 4) : ""}</td>
                  <td>{f ? fmtNum(f.pred, 4) : ""}</td>
                  <td>{f ? `${fmtNum(f.resid, 4)} / ${fmtNum(f.thr, 4)}` : ""}</td>
                  <td>{f ? [f.anomaly && "аномалія", f.substituted && "замінено"].filter(Boolean).join(", ") : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
