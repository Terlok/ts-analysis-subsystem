// Hover tooltip: what is happening on the chart at the cursor — values, model forecasts,
// anomaly/outlier episodes, flagged points, alarms and marks.
import type uPlot from "uplot";
import { OVERLAY } from "../lib/colors";
import type { Overlays } from "../lib/engine";
import { ALARM_STATE_UA, fmtDateTime, fmtDuration, fmtNum } from "../lib/format";

export interface TipSeries {
  channel: string;
  label: string;
  color: string;
}

const US = 1e6;
const MAX_ITEMS = 4;

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const dot = (color: string) => `<i class="tip-dot" style="background:${color}"></i>`;

/** Last non-null value of a data column at or before index i (series with gaps). */
function valueAt(col: (number | null | undefined)[], i: number): number | null {
  for (let k = i; k >= 0 && i - k < 50; k--) {
    const v = col[k];
    if (v != null) return v;
  }
  return null;
}

/** Index of the element of sorted `arr` nearest to t. */
function nearest(arr: number[], t: number): number {
  let lo = 0;
  let hi = arr.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(arr[lo - 1] - t) < Math.abs(arr[lo] - t)) return lo - 1;
  return lo;
}

export function tooltipHtml(u: uPlot, series: TipSeries[], o: Overlays, t: number, idx: number | null): string {
  const xMin = u.scales.x.min ?? 0;
  const xMax = u.scales.x.max ?? 0;
  const width = u.bbox.width / (devicePixelRatio || 1);
  const tol = ((xMax - xMin) / Math.max(width, 1)) * 5; // ±5 px in seconds
  const chans = new Map(series.map((s) => [s.channel, s]));
  const rows: string[] = [`<div class="tip-time">${fmtDateTime(t, true)}</div>`];

  // values and model forecasts
  const values = new Map<string, number | null>();
  series.forEach((s, i) => {
    const v = idx != null ? valueAt(u.data[i + 1] as (number | null)[], idx) : null;
    values.set(s.channel, v);
    rows.push(`<div>${dot(s.color)}${esc(s.label)}: <b>${fmtNum(v, 3)}</b></div>`);
  });
  for (const p of o.previews) {
    if (!chans.has(p.channel) || !p.t.length) continue;
    const k = nearest(p.t, t);
    if (Math.abs(p.t[k] - t) > tol * 4) continue;
    const x = values.get(p.channel);
    const resid = x != null ? Math.abs(x - p.v[k]) : null;
    rows.push(
      `<div>${dot(p.color)}прогноз ${esc(p.label)}: <b>${fmtNum(p.v[k], 3)}</b>${resid != null ? ` · залишок ${fmtNum(resid, 3)}` : ""}</div>`,
    );
  }

  const extra: string[] = [];

  // episodes covering the cursor
  const eps = o.events.filter((e) => chans.has(e.channel_id) && e.ts_start / US - tol <= t && e.ts_end / US + tol >= t);
  for (const e of eps.slice(0, MAX_ITEMS)) {
    const anomaly = e.kind === "anomaly";
    const dur = (e.ts_end - e.ts_start) / US;
    extra.push(
      `<div>${dot(anomaly ? OVERLAY.anomaly : OVERLAY.outlier)}${anomaly ? "аномалія" : "викид"} · ${esc(e.channel_id)}` +
        ` · ${e.n_points} т.${dur > 0 ? `, ${fmtDuration(dur)}` : ""} · пік ${fmtNum(e.peak_value, 3)}` +
        `${e.score != null ? ` · оцінка ${fmtNum(e.score, 2)}` : ""}</div>`,
    );
  }
  if (eps.length > MAX_ITEMS) extra.push(`<div class="tip-muted">ще ${eps.length - MAX_ITEMS} подій…</div>`);

  // flagged points next to the cursor (stream diagnostics and model previews)
  const near = (ts: number) => Math.abs(ts / US - t) <= tol;
  const flags = o.flags.filter((f) => chans.has(f.channel) && near(f.ts));
  for (const f of flags.slice(0, MAX_ITEMS)) {
    if (f.anomaly) {
      const why = f.resid != null && f.thr != null ? ` · залишок ${fmtNum(f.resid, 3)} > поріг ${fmtNum(f.thr, 3)}` : "";
      extra.push(`<div>${dot(OVERLAY.anomaly)}аномальна точка ${esc(f.channel)}: ${fmtNum(f.val, 3)}${f.pred != null ? `, прогноз ${fmtNum(f.pred, 3)}` : ""}${why}</div>`);
    }
    if (f.substituted) {
      extra.push(`<div>${dot(OVERLAY.outlier)}викид замінено (Гампель): ${fmtNum(f.val, 3)} → ${fmtNum(f.val_f, 3)}</div>`);
    }
  }
  for (const p of o.previews) {
    const pf = p.flags.filter((f) => chans.has(f.channel) && near(f.ts) && f.anomaly);
    if (pf.length) {
      const f = pf[0];
      extra.push(
        `<div>${dot(p.color)}${esc(p.label)}: аномалія${f.resid != null && f.thr != null ? `, залишок ${fmtNum(f.resid, 3)} > ${fmtNum(f.thr, 3)}` : ""}</div>`,
      );
    }
  }

  // alarms active at the cursor
  for (const a of o.alarms.filter((a) => chans.has(a.channel_id) && a.ts_active / US <= t && (a.ts_return == null || a.ts_return / US >= t)).slice(0, MAX_ITEMS)) {
    extra.push(
      `<div>${dot(OVERLAY.alarm[a.priority] ?? OVERLAY.alarm[2])}тривога #${a.id} · ${esc(a.channel_id)} · пріоритет ${a.priority} · ${ALARM_STATE_UA[a.state] ?? a.state}</div>`,
    );
  }

  // marks / mode changes
  for (const m of o.modes.filter((m) => near(m.ts)).slice(0, MAX_ITEMS)) {
    extra.push(`<div>${dot(OVERLAY.mark)}⚑ ${esc(m.mode)}${m.robot !== "default" ? ` (${esc(m.robot)})` : ""}</div>`);
  }

  if (extra.length) rows.push(`<div class="tip-sep"></div>`, ...extra);
  return rows.join("");
}
