// Overview strip under the chart (whole archive of the selected channels) with a brush that
// shows the visible range: drag the brush to pan, click elsewhere to jump there.
// The overview changes rarely (once a minute), so the plot is simply rebuilt on every version.
import { useEffect, useRef } from "react";
import uPlot from "uplot";
import type { ChartData } from "../lib/engine";

const HEIGHT = 46;

interface Props {
  getData: () => ChartData;
  version: number;
  colorOf: (channel: string) => string;
  range: [number, number];
  dataRange: [number, number] | null;
  onMove: (a: number, b: number) => void;
}

export function Navigator(p: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const props = useRef(p);
  props.current = p;

  useEffect(() => {
    const el = wrap.current;
    const d = p.getData();
    if (!el || !d.channels.length || !d.x.length) return;
    const u = new uPlot(
      {
        width: el.clientWidth || 800,
        height: HEIGHT,
        pxAlign: 0,
        legend: { show: false },
        cursor: { show: false, drag: { x: false, y: false } },
        select: { show: false, left: 0, top: 0, width: 0, height: 0 },
        scales: {
          x: {
            time: true,
            range: (_u, min, max) => {
              const r = props.current.dataRange;
              // include the visible range so the brush is always on the strip
              const [a, b] = props.current.range;
              return [Math.min(r?.[0] ?? min, a), Math.max(r?.[1] ?? max, b)];
            },
          },
          ...Object.fromEntries(d.channels.map((_, i) => [`y${i}`, { auto: true }])),
        },
        axes: [{ show: false }, ...d.channels.map((_, i) => ({ show: false, scale: `y${i}` }))],
        series: [
          {},
          ...d.channels.map((c, i) => ({
            scale: `y${i}`,
            stroke: props.current.colorOf(c),
            width: 1,
            spanGaps: true,
            points: { show: false },
          })),
        ],
        hooks: {
          draw: [
            (u) => {
              const [a, b] = props.current.range;
              const { ctx, bbox } = u;
              const x1 = Math.max(bbox.left, u.valToPos(a, "x", true));
              const x2 = Math.min(bbox.left + bbox.width, u.valToPos(b, "x", true));
              ctx.save();
              ctx.fillStyle = "rgba(60, 90, 110, 0.16)";
              ctx.fillRect(bbox.left, bbox.top, Math.max(0, x1 - bbox.left), bbox.height);
              ctx.fillRect(x2, bbox.top, Math.max(0, bbox.left + bbox.width - x2), bbox.height);
              ctx.strokeStyle = "#3f6f8f";
              ctx.lineWidth = devicePixelRatio || 1;
              ctx.strokeRect(x1, bbox.top + 0.5, Math.max(x2 - x1, 2), bbox.height - 1);
              ctx.restore();
            },
          ],
        },
      },
      [d.x, ...d.ys] as uPlot.AlignedData,
      el,
    );
    plot.current = u;

    let drag: { startX: number; a: number; b: number } | null = null;
    const valAt = (clientX: number) => u.posToVal(clientX - u.over.getBoundingClientRect().left, "x");
    const onDown = (ev: PointerEvent) => {
      const [a, b] = props.current.range;
      const t = valAt(ev.clientX);
      if (t >= a && t <= b) {
        drag = { startX: t, a, b };
        u.over.setPointerCapture(ev.pointerId);
      } else {
        const half = (b - a) / 2;
        props.current.onMove(t - half, t + half);
      }
    };
    const onMove = (ev: PointerEvent) => {
      if (!drag) return;
      const dt = valAt(ev.clientX) - drag.startX;
      props.current.onMove(drag.a + dt, drag.b + dt);
    };
    const onUp = () => {
      drag = null;
    };
    u.over.addEventListener("pointerdown", onDown);
    u.over.addEventListener("pointermove", onMove);
    u.over.addEventListener("pointerup", onUp);
    u.over.style.cursor = "pointer";

    const ro = new ResizeObserver(() => {
      if (el.clientWidth > 0 && el.clientWidth !== u.width) u.setSize({ width: el.clientWidth, height: HEIGHT });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.destroy();
      plot.current = null;
    };
  }, [p.version]); // eslint-disable-line react-hooks/exhaustive-deps

  // the brush follows the visible range
  useEffect(() => {
    const u = plot.current;
    if (!u) return;
    u.setScale("x", { min: u.scales.x.min ?? 0, max: u.scales.x.max ?? 1 }); // re-run the range function
  }, [p.range[0], p.range[1], p.dataRange?.[0], p.dataRange?.[1]]); // eslint-disable-line react-hooks/exhaustive-deps

  return <div className="navigator" ref={wrap} style={{ height: HEIGHT }} />;
}
