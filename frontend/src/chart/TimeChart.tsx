// Canvas time chart (uPlot). Point arrays are taken from the DataEngine on every data version;
// they never pass through React state.
import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { ChartData, Overlays } from "../lib/engine";
import { fmtDate, fmtDateTime, fmtNum, fmtTime } from "../lib/format";
import { drawOverlays } from "./overlays";

export interface ChartSeries {
  channel: string;
  color: string;
  label: string;
  limits?: [number | null, number | null];
}

interface Props {
  series: ChartSeries[];
  getData: () => ChartData;
  version: number;
  overlayVersion: number;
  getOverlays: () => Overlays;
  range: [number, number];
  showXAxis?: boolean;
  syncKey?: string;
  layers: { events: boolean; flags: boolean; alarms: boolean };
  onZoom: (a: number, b: number) => void;
  onWheelZoom: (center: number, factor: number) => void;
  onPan: (fraction: number) => void;
  onReset: () => void;
  onCursor?: (t: number | null) => void;
  onWidth?: (px: number) => void;
}

/** Data in the order of this chart's series. A channel that the engine has not loaded yet
 * (e.g. just added: the chart re-renders before the engine is reconfigured) gets an empty
 * column, so uPlot never sees fewer data columns than series. */
function aligned(d: ChartData, series: ChartSeries[]): uPlot.AlignedData {
  const idx = new Map(d.channels.map((c, i) => [c, i]));
  const empty = () => new Array<number | null>(d.x.length).fill(null);
  return [d.x, ...series.map((s) => (idx.has(s.channel) ? d.ys[idx.get(s.channel)!] : empty()))] as uPlot.AlignedData;
}

function yRange(limits: [number | null, number | null] | undefined) {
  return (_u: uPlot, min: number, max: number): uPlot.Range.MinMax => {
    let lo = limits?.[0] ?? null;
    let hi = limits?.[1] ?? null;
    if (lo === null || hi === null) {
      if (!Number.isFinite(min) || !Number.isFinite(max)) return [lo ?? 0, hi ?? 1];
      const pad = (max - min) * 0.08 || Math.abs(max) * 0.05 || 1;
      lo ??= min - pad;
      hi ??= max + pad;
    }
    return [lo, hi];
  };
}

export function TimeChart(p: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const props = useRef(p);
  props.current = p;

  const structureKey = JSON.stringify([p.series, p.showXAxis, p.syncKey, p.layers]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const width = el.clientWidth || 800;
    const opts: uPlot.Options = {
      width,
      height: Math.max(60, el.clientHeight - 30),
      pxAlign: 0,
      cursor: {
        drag: { x: true, y: false, setScale: false },
        sync: p.syncKey ? { key: p.syncKey } : undefined,
        points: { size: 6 },
      },
      select: { show: true, left: 0, top: 0, width: 0, height: 0 },
      legend: {
        show: true,
        live: true,
      },
      scales: {
        x: { time: true, range: () => props.current.range as uPlot.Range.MinMax },
        ...Object.fromEntries(p.series.map((s, i) => [`y${i}`, { auto: true, range: yRange(s.limits) }])),
      },
      axes: [
        {
          show: p.showXAxis !== false,
          stroke: "#4a6a80",
          grid: { stroke: "rgba(80, 110, 130, 0.18)", width: 1 },
          ticks: { stroke: "rgba(80, 110, 130, 0.4)", width: 1 },
          font: "11px monospace",
          space: 90,
          // 24-hour local time; the date is added under ticks where the day changes
          values: (_u: uPlot, splits: number[], _ax: number, _space: number, incr: number) => {
            let prevDay = "";
            return splits.map((s) => {
              const day = fmtDate(s);
              const t = incr >= 86400 ? "" : fmtTime(s, incr < 60);
              const label = day !== prevDay ? (t ? `${t}\n${day}` : day) : t;
              prevDay = day;
              return label;
            });
          },
        },
        ...p.series.map((s, i) => ({
          scale: `y${i}`,
          side: i === 0 ? 3 : 1,
          stroke: p.series.length > 1 ? s.color : "#4a6a80",
          grid: { show: i === 0, stroke: "rgba(80, 110, 130, 0.18)", width: 1 },
          ticks: { stroke: "rgba(80, 110, 130, 0.4)", width: 1 },
          font: "11px monospace",
          size: 60,
          values: (_u: uPlot, vals: number[]) => vals.map((v) => fmtNum(v, Math.abs(v) < 10 ? 2 : 1)),
        })),
      ],
      series: [
        { label: "Час", value: (_u, v) => (v == null ? "—" : fmtDateTime(v, true)) },
        ...p.series.map((s, i) => ({
          label: s.label,
          scale: `y${i}`,
          stroke: s.color,
          width: 1.25,
          spanGaps: true,
          points: { show: false },
          value: (_u: uPlot, v: number | null) => (v == null ? "—" : fmtNum(v, 3)),
        })),
      ],
      hooks: {
        setSelect: [
          (u) => {
            if (u.select.width > 4) {
              const a = u.posToVal(u.select.left, "x");
              const b = u.posToVal(u.select.left + u.select.width, "x");
              props.current.onZoom(a, b);
            }
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          },
        ],
        setCursor: [
          (u) => {
            const left = u.cursor.left;
            props.current.onCursor?.(left != null && left >= 0 ? u.posToVal(left, "x") : null);
          },
        ],
        draw: [
          (u) =>
            drawOverlays(u, {
              channels: props.current.series.map((s) => s.channel),
              overlays: props.current.getOverlays(),
              showEvents: props.current.layers.events,
              showFlags: props.current.layers.flags,
              showAlarms: props.current.layers.alarms,
            }),
        ],
      },
    };
    const u = new uPlot(opts, aligned(props.current.getData(), props.current.series), el);
    plot.current = u;

    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const rect = u.over.getBoundingClientRect();
      if (ev.shiftKey) {
        props.current.onPan((ev.deltaY > 0 ? 1 : -1) * 0.1);
        return;
      }
      const center = u.posToVal(ev.clientX - rect.left, "x");
      props.current.onWheelZoom(center, ev.deltaY > 0 ? 1.25 : 0.8);
    };
    const onDbl = () => props.current.onReset();
    u.over.addEventListener("wheel", onWheel, { passive: false });
    u.over.addEventListener("dblclick", onDbl);

    const fit = () => {
      const legend = (u.root.querySelector(".u-legend") as HTMLElement | null)?.offsetHeight ?? 0;
      const w = el.clientWidth;
      const h = Math.max(60, el.clientHeight - legend - 2);
      if (w > 0 && (w !== u.width || h !== u.height)) {
        u.setSize({ width: w, height: h });
        props.current.onWidth?.(u.bbox.width / (devicePixelRatio || 1));
      }
    };
    const ro = new ResizeObserver(fit);
    fit();
    ro.observe(el);
    props.current.onWidth?.(u.bbox.width / (devicePixelRatio || 1));

    return () => {
      ro.disconnect();
      u.over.removeEventListener("wheel", onWheel);
      u.over.removeEventListener("dblclick", onDbl);
      u.destroy();
      plot.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structureKey]);

  // new data or new visible range
  useEffect(() => {
    const u = plot.current;
    if (!u) return;
    u.batch(() => {
      u.setData(aligned(p.getData(), p.series), false);
      u.setScale("x", { min: p.range[0], max: p.range[1] });
    });
  }, [p.version, p.range[0], p.range[1]]); // eslint-disable-line react-hooks/exhaustive-deps

  // overlays changed: repaint only
  useEffect(() => {
    plot.current?.redraw(false);
  }, [p.overlayVersion]);

  return <div className="chart" ref={wrap} />;
}
