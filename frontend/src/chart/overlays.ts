// Events layer drawn on top of the curve. It comes from diagnostics on full-resolution data,
// so markers are shown at their exact positions regardless of how much the curve is
// downsampled (thesis, formula 1.16).
import type uPlot from "uplot";
import { OVERLAY } from "../lib/colors";
import type { Overlays } from "../lib/engine";

export interface OverlayOptions {
  channels: string[]; // channels of this chart, scale of channel i is `y${i}`
  overlays: Overlays;
  showEvents: boolean;
  showFlags: boolean;
  showAlarms: boolean;
}

const US = 1e6;

export function drawOverlays(u: uPlot, o: OverlayOptions) {
  const { ctx, bbox } = u;
  const dpr = devicePixelRatio || 1;
  const xMin = u.scales.x.min ?? 0;
  const xMax = u.scales.x.max ?? 0;
  const xPos = (s: number) => u.valToPos(s, "x", true);
  const chIndex = new Map(o.channels.map((c, i) => [c, i]));

  ctx.save();
  ctx.beginPath();
  ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
  ctx.clip();

  // anomaly / outlier episodes: translucent bands + solid strip at the top
  if (o.showEvents) {
    for (const e of o.overlays.events) {
      if (!chIndex.has(e.channel_id)) continue;
      const a = e.ts_start / US;
      const b = e.ts_end / US;
      if (b < xMin || a > xMax) continue;
      let x1 = xPos(a);
      let x2 = xPos(b);
      if (x2 - x1 < 3 * dpr) {
        const c = (x1 + x2) / 2;
        x1 = c - 1.5 * dpr;
        x2 = c + 1.5 * dpr;
      }
      const anomaly = e.kind === "anomaly";
      ctx.fillStyle = anomaly ? OVERLAY.anomalyBand : OVERLAY.outlierBand;
      ctx.fillRect(x1, bbox.top, x2 - x1, bbox.height);
      ctx.fillStyle = anomaly ? OVERLAY.anomaly : OVERLAY.outlier;
      ctx.fillRect(x1, bbox.top, x2 - x1, 4 * dpr);
    }
  }

  // alarms: strip at the bottom, colored by priority, hatched while unacknowledged
  if (o.showAlarms) {
    for (const al of o.overlays.alarms) {
      if (!chIndex.has(al.channel_id)) continue;
      const a = al.ts_active / US;
      const b = al.ts_return ? al.ts_return / US : xMax;
      if (b < xMin || a > xMax) continue;
      const x1 = xPos(a);
      const x2 = Math.max(xPos(b), x1 + 2 * dpr);
      const y = bbox.top + bbox.height - 6 * dpr;
      ctx.fillStyle = OVERLAY.alarm[al.priority] ?? OVERLAY.alarm[2];
      ctx.globalAlpha = al.state.startsWith("unack") ? 1 : 0.55;
      ctx.fillRect(x1, y, x2 - x1, 6 * dpr);
      ctx.globalAlpha = 1;
    }
  }

  // point flags: anomalies (filled red), substituted outliers (amber ring), analysis preview (violet)
  const drawPoints = (pts: typeof o.overlays.flags, kind: "flags" | "preview") => {
    for (const f of pts) {
      const i = chIndex.get(f.channel);
      if (i === undefined || f.val === null) continue;
      const t = f.ts / US;
      if (t < xMin || t > xMax) continue;
      const x = xPos(t);
      const y = u.valToPos(f.val, `y${i}`, true);
      if (!Number.isFinite(y)) continue;
      ctx.beginPath();
      if (kind === "preview") {
        const r = 4 * dpr;
        ctx.moveTo(x, y - r);
        ctx.lineTo(x + r, y);
        ctx.lineTo(x, y + r);
        ctx.lineTo(x - r, y);
        ctx.closePath();
        ctx.strokeStyle = OVERLAY.preview;
        ctx.lineWidth = 1.5 * dpr;
        ctx.stroke();
      } else if (f.anomaly) {
        ctx.arc(x, y, 3.5 * dpr, 0, Math.PI * 2);
        ctx.fillStyle = OVERLAY.anomaly;
        ctx.fill();
      } else if (f.substituted) {
        ctx.arc(x, y, 3.5 * dpr, 0, Math.PI * 2);
        ctx.strokeStyle = OVERLAY.outlier;
        ctx.lineWidth = 1.5 * dpr;
        ctx.stroke();
      }
    }
  };
  // forecast of the analysed model: dashed line on the channel scale
  const fc = o.overlays.forecast;
  const fi = fc ? chIndex.get(fc.channel) : undefined;
  if (fc && fi !== undefined && fc.t.length > 1) {
    ctx.strokeStyle = OVERLAY.preview;
    ctx.lineWidth = 1.25 * dpr;
    ctx.setLineDash([5 * dpr, 3 * dpr]);
    ctx.beginPath();
    let started = false;
    for (let k = 0; k < fc.t.length; k++) {
      if (fc.t[k] < xMin || fc.t[k] > xMax) {
        started = false;
        continue;
      }
      const x = xPos(fc.t[k]);
      const y = u.valToPos(fc.v[k], `y${fi}`, true);
      if (started) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
      started = true;
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  if (o.showFlags) drawPoints(o.overlays.flags, "flags");
  drawPoints(o.overlays.preview, "preview");

  // marks / robot mode changes: dashed vertical line with label
  ctx.font = `${10 * dpr}px monospace`;
  ctx.textBaseline = "top";
  for (const m of o.overlays.modes) {
    const t = m.ts / US;
    if (t < xMin || t > xMax) continue;
    const x = Math.round(xPos(t)) + 0.5;
    ctx.strokeStyle = OVERLAY.mark;
    ctx.lineWidth = 1 * dpr;
    ctx.setLineDash([4 * dpr, 3 * dpr]);
    ctx.beginPath();
    ctx.moveTo(x, bbox.top);
    ctx.lineTo(x, bbox.top + bbox.height);
    ctx.stroke();
    ctx.setLineDash([]);
    const label = `⚑ ${m.mode}`;
    const w = ctx.measureText(label).width + 6 * dpr;
    ctx.fillStyle = "rgba(233, 240, 245, 0.9)";
    ctx.fillRect(x + 2 * dpr, bbox.top + 6 * dpr, w, 13 * dpr);
    ctx.fillStyle = OVERLAY.mark;
    ctx.fillText(label, x + 5 * dpr, bbox.top + 8 * dpr);
  }
  ctx.restore();
}
