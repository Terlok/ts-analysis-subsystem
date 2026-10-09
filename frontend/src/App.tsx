import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api/client";
import type { ChannelState } from "./api/types";
import { Navigator } from "./chart/Navigator";
import { TimeChart, type ChartSeries } from "./chart/TimeChart";
import { BottomPanel, type PanelSize } from "./components/BottomPanel";
import { ChannelBar, type Layers } from "./components/ChannelBar";
import { TopBar } from "./components/TopBar";
import { seriesColor } from "./lib/colors";
import { engine } from "./lib/engine";
import { AppDataContext, useAppData, useEngine, usePolling, useWorkspaces, type AppData } from "./lib/hooks";
import { storage } from "./lib/storage";
import { workspaces, type Workspace } from "./lib/workspaces";

/** Engine configuration key: a change means the data stream has to be (re)started. */
const keyOf = (w: Workspace) =>
  w.mode === "live" ? `${w.id}|${w.channels.join(",")}|live|${w.windowS}` : `${w.id}|${w.channels.join(",")}|archive`;

function ChartArea({ ws, layers }: { ws: Workspace; layers: Layers }) {
  const snap = useEngine();
  const { channels, colorOf } = useAppData();
  const getOverlays = useCallback(() => engine.overlays, []);

  const series: ChartSeries[] = ws.channels.map((ch) => {
    const reg = channels.find((c) => c.id === ch);
    const label = reg?.name && reg.name !== ch ? `${reg.name}` : ch;
    return { channel: ch, color: colorOf(ch), label: reg?.unit ? `${label}, ${reg.unit}` : label, limits: ws.limits[ch] };
  });

  const common = {
    version: snap.version,
    overlayVersion: snap.overlayVersion,
    getOverlays,
    range: snap.range,
    layers,
    syncKey: `sync-${ws.id}`,
    onZoom: (a: number, b: number) => engine.zoomTo(a, b),
    onWheelZoom: (c: number, f: number) => engine.zoomAround(c, f),
    onPan: (f: number) => engine.panBy(f),
    onReset: () => engine.resetZoom(),
    onCursor: (t: number | null) => {
      engine.cursorT = t;
    },
    onWidth: (px: number) => engine.setWidth(px),
  };

  const empty = snap.mode === "live" && !snap.loading && ws.channels.length > 0 && Object.values(snap.meta).every((m) => m.n === 0) && !Object.keys(snap.last).length;

  return (
    <main className="chart-area">
      {!ws.channels.length ? (
        <div className="placeholder">
          Додайте канал (тег) у рядку вище або на вкладці «Канали», щоб побачити його графік.
        </div>
      ) : ws.layout === "single" || ws.channels.length === 1 ? (
        <TimeChart key={`single-${ws.id}`} {...common} series={series} getData={() => engine.getChartData()} />
      ) : (
        ws.channels.map((ch, i) => (
          <TimeChart
            key={`stack-${ws.id}-${ch}`}
            {...common}
            series={[series[i]]}
            getData={() => engine.getChannelData(ch)}
            showXAxis={i === ws.channels.length - 1}
          />
        ))
      )}
      {empty && (
        <div className="hint">
          У живому вікні немає даних. Запустіть відтворювач (<code>python -m scripts.replay data/…</code>) або відкрийте «Архів».
        </div>
      )}
      {ws.channels.length > 0 && (
        <Navigator
          getData={() => engine.getOverviewData()}
          version={snap.overviewVersion * 1000 + ws.channels.length}
          colorOf={colorOf}
          range={snap.range}
          dataRange={snap.dataRange}
          onMove={(a, b) => engine.zoomTo(a, b)}
        />
      )}
    </main>
  );
}

export function App() {
  useWorkspaces();
  const ws = workspaces.active();
  const channelsQ = usePolling(() => api.channels(), 30_000);
  const statesQ = usePolling(() => api.channelsState(), 5_000);
  const healthQ = usePolling(() => api.health(), 10_000);
  const [layers, setLayers] = useState<Layers>(() => {
    try {
      return { events: true, flags: true, alarms: true, ...JSON.parse(storage.get("tsa.layers") ?? "{}") };
    } catch {
      return { events: true, flags: true, alarms: true };
    }
  });
  const [panel, setPanel] = useState<PanelSize>(() => (storage.get("tsa.panel") as PanelSize | null) ?? "normal");
  const lastKey = useRef("");

  // server parameters the live mode has to respect
  useEffect(() => {
    api
      .config()
      .then((c) => {
        engine.hotWindowS = c.hot_window_s;
        engine.liveMaxWindowS = c.live_max_window_s;
      })
      .catch(() => undefined);
  }, []);

  // view changes made by the engine (zoom freezes live, "▶ Наживо") are persisted in the workspace
  useEffect(() => {
    engine.onViewChange = (mode, range) => {
      const cur = workspaces.active();
      const archive = mode === "archive" ? range : cur.archive;
      lastKey.current = keyOf({ ...cur, mode, archive });
      workspaces.update(cur.id, { mode, archive });
    };
    return () => {
      engine.onViewChange = null;
    };
  }, []);

  const key = keyOf(ws);
  useEffect(() => {
    if (key === lastKey.current) return;
    lastKey.current = key;
    engine.configure({ channels: ws.channels, mode: ws.mode, windowS: ws.windowS, archive: ws.archive });
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const data: AppData = useMemo(() => {
    const states: Record<string, ChannelState> = {};
    for (const s of statesQ.data ?? []) states[s.id] = s;
    return {
      channels: channelsQ.data ?? [],
      reloadChannels: channelsQ.reload,
      states,
      health: healthQ.data,
      colorOf: (c: string) => {
        const i = ws.channels.indexOf(c);
        return seriesColor(i >= 0 ? i : 0);
      },
    };
  }, [channelsQ.data, channelsQ.reload, statesQ.data, healthQ.data, ws.channels]);

  return (
    <AppDataContext.Provider value={data}>
      <div className={`app panel-${panel}`}>
        <TopBar />
        <ChannelBar
          ws={ws}
          layers={layers}
          onLayers={(l) => {
            setLayers(l);
            storage.set("tsa.layers", JSON.stringify(l));
          }}
        />
        <ChartArea ws={ws} layers={layers} />
        <BottomPanel
          ws={ws}
          size={panel}
          onSize={(s) => {
            setPanel(s);
            storage.set("tsa.panel", s);
          }}
        />
      </div>
    </AppDataContext.Provider>
  );
}
