// DataEngine: owns all point arrays outside of React state (thesis 1.5.4: React state must not
// hold point arrays, otherwise every update re-reconciles the component tree).
//
// Live mode   : history of [t_end - W, t_end] from /api/series (refreshed periodically) +
//               finalized points and provisional tail from /ws/stream appended on top.
// Archive mode: /api/series for the visible range; zoom/pan rescales immediately with the
//               data already loaded (progressive refinement) and refetches after a short debounce.
// Overlays    : events, point flags, alarms and marks come from the separate events layer and
//               are drawn independently of the downsampling level.
import { api } from "../api/client";
import type { Alarm, EventItem, FlagPoint, ModeChange } from "../api/types";
import type { FromWorker, LiveEvent, LiveSeries, SeriesData, ToWorker, WsStatus } from "../workers/protocol";
import type { Mode } from "./workspaces";

export interface ChannelMeta {
  n: number;
  m: number;
  eta: number;
  source: string;
  level: number | null;
  vmin: number | null;
  vmax: number | null;
  mean: number | null;
  shown: number;
}

export interface EngineSnapshot {
  version: number;
  overlayVersion: number;
  overviewVersion: number;
  mode: Mode;
  windowS: number;
  range: [number, number];
  dataRange: [number, number] | null;
  loading: boolean;
  error: string | null;
  wsStatus: WsStatus;
  meta: Record<string, ChannelMeta>;
  last: Record<string, { t: number; v: number }>;
  liveLatencyMs: number | null;
  fetchMs: number | null;
  serverMs: number | null;
  /** events / point flags in the interval vs. shown (only the most significant if truncated) */
  layerCounts: { events: number; eventsShown: number; flags: number; flagsShown: number };
}

interface Arr {
  t: Float64Array;
  v: Float64Array;
}

export interface ChartData {
  channels: string[];
  x: number[];
  ys: (number | null)[][];
}

export interface Overlays {
  events: EventItem[];
  flags: FlagPoint[];
  alarms: Alarm[];
  modes: ModeChange[];
  /** results of the analysis preview (one layer per compared model) */
  previews: PreviewLayer[];
}

export interface PreviewLayer {
  key: string;
  label: string;
  color: string;
  channel: string;
  flags: FlagPoint[];
  t: number[]; // one-step-ahead forecast, seconds
  v: number[];
}

const MAX_LIVE_FLAGS = 5000;
const FETCH_DEBOUNCE_MS = 120;

function sliceAfter(a: Arr, t0: number): Arr {
  let i = 0;
  while (i < a.t.length && a.t[i] <= t0) i++;
  return { t: a.t.subarray(i), v: a.v.subarray(i) };
}

/** Align several series on the union of their timestamps (gaps -> null, spanned by uPlot). */
export function joinSeries(list: Arr[]): { x: number[]; ys: (number | null)[][] } {
  if (list.length === 1) {
    const { t, v } = list[0];
    const ys = new Array<number | null>(v.length);
    for (let i = 0; i < v.length; i++) ys[i] = Number.isNaN(v[i]) ? null : v[i];
    return { x: Array.from(t), ys: [ys] };
  }
  const all = new Float64Array(list.reduce((s, a) => s + a.t.length, 0));
  let o = 0;
  for (const a of list) {
    all.set(a.t, o);
    o += a.t.length;
  }
  all.sort();
  const x: number[] = [];
  for (let i = 0; i < all.length; i++) if (i === 0 || all[i] !== all[i - 1]) x.push(all[i]);
  const ys = list.map((a) => {
    const y = new Array<number | null>(x.length).fill(null);
    let j = 0;
    for (let i = 0; i < a.t.length; i++) {
      while (x[j] < a.t[i]) j++;
      const v = a.v[i];
      y[j] = Number.isNaN(v) ? null : v;
    }
    return y;
  });
  return { x, ys };
}

function concat(parts: Arr[]): Arr {
  const n = parts.reduce((s, p) => s + p.t.length, 0);
  const t = new Float64Array(n);
  const v = new Float64Array(n);
  let o = 0;
  for (const p of parts) {
    t.set(p.t, o);
    v.set(p.v, o);
    o += p.t.length;
  }
  return { t, v };
}

export class DataEngine {
  private worker: Worker;
  private listeners = new Set<() => void>();
  private snap: EngineSnapshot;

  private channels: string[] = [];
  private widthPx = 1200;
  private hist = new Map<string, Arr>();
  private liveFin = new Map<string, { t: number[]; v: number[] }>();
  private liveTail = new Map<string, Arr>();
  private overview = new Map<string, Arr>();
  overlays: Overlays = { events: [], flags: [], alarms: [], modes: [], previews: [] };

  private reqSeq = 0;
  private histReq = 0;
  private overviewReq = 0;
  private reqStarted = new Map<number, number>();
  private fetchTimer: ReturnType<typeof setTimeout> | null = null;
  private liveTimer: ReturnType<typeof setInterval> | null = null;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;
  private overviewTimer: ReturnType<typeof setInterval> | null = null;
  private latestDataT = 0;
  private chartCache: { version: number; data: ChartData } | null = null;
  private overviewCache: { version: number; data: ChartData } | null = null;

  hotWindowS = 600;
  liveMaxWindowS = 3600;
  cursorT: number | null = null;
  /** Called when the engine itself changes the view (zoom switches live -> archive, etc.). */
  onViewChange: ((mode: Mode, range: [number, number]) => void) | null = null;

  constructor() {
    this.worker = new Worker(new URL("../workers/data.worker.ts", import.meta.url), { type: "module" });
    this.worker.onmessage = (ev: MessageEvent<FromWorker>) => this.onWorker(ev.data);
    const now = Date.now() / 1000;
    this.snap = {
      version: 0,
      overlayVersion: 0,
      overviewVersion: 0,
      mode: "live",
      windowS: 1800,
      range: [now - 1800, now],
      dataRange: null,
      loading: false,
      error: null,
      wsStatus: "offline",
      meta: {},
      last: {},
      liveLatencyMs: null,
      fetchMs: null,
      serverMs: null,
      layerCounts: { events: 0, eventsShown: 0, flags: 0, flagsShown: 0 },
    };
  }

  // --- store protocol for useSyncExternalStore ---------------------------------

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getSnapshot = () => this.snap;

  private emit(patch: Partial<EngineSnapshot>) {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn());
  }
  private bumpData(patch: Partial<EngineSnapshot> = {}) {
    this.emit({ ...patch, version: this.snap.version + 1 });
  }
  private bumpOverlay() {
    this.emit({ overlayVersion: this.snap.overlayVersion + 1 });
  }
  private send(msg: ToWorker) {
    this.worker.postMessage(msg);
  }

  // --- configuration --------------------------------------------------------------

  configure(opts: { channels: string[]; mode: Mode; windowS: number; archive: [number, number] | null }) {
    const sameChannels = opts.channels.join("\u0000") === this.channels.join("\u0000");
    this.channels = [...opts.channels];
    if (!sameChannels) {
      this.hist.clear();
      this.liveFin.clear();
      this.liveTail.clear();
      this.overview.clear();
      this.overlays = { ...this.overlays, events: [], flags: [], alarms: [], previews: [] };
      this.latestDataT = 0;
      this.emit({ meta: {}, last: {} });
      void this.loadOverview();
    }
    this.stopTimers();
    if (opts.mode === "live") this.startLive(opts.windowS);
    else this.startArchive(opts.archive ?? this.defaultArchiveRange(opts.windowS));
    if (!this.overviewTimer) this.overviewTimer = setInterval(() => void this.loadOverview(), 60_000);
  }

  setWidth(px: number) {
    const w = Math.max(100, Math.round(px));
    if (Math.abs(w - this.widthPx) / this.widthPx > 0.15) {
      this.widthPx = w;
      this.scheduleFetch(0);
    } else this.widthPx = w;
  }

  dispose() {
    this.stopTimers();
    if (this.overviewTimer) clearInterval(this.overviewTimer);
    this.send({ type: "live-stop" });
    this.worker.terminate();
  }

  private stopTimers() {
    if (this.liveTimer) clearInterval(this.liveTimer);
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    if (this.fetchTimer) clearTimeout(this.fetchTimer);
    this.liveTimer = this.refreshTimer = this.fetchTimer = null;
  }

  private defaultArchiveRange(windowS: number): [number, number] {
    const end = this.snap.dataRange?.[1] ?? Date.now() / 1000;
    return [end - windowS, end];
  }

  // --- live mode ------------------------------------------------------------------

  private liveEnd(): number {
    return Math.max(Date.now() / 1000, this.latestDataT);
  }

  private startLive(windowS: number) {
    this.liveFin.clear();
    this.liveTail.clear();
    const end = this.liveEnd();
    this.emit({ mode: "live", windowS, range: [end - windowS, end + windowS * 0.01] });
    if (!this.channels.length) {
      this.send({ type: "live-stop" });
      this.bumpData();
      return;
    }
    this.send({
      type: "live-subscribe",
      sub: {
        channels: this.channels,
        window_s: Math.min(windowS, this.hotWindowS, this.liveMaxWindowS),
        width_px: this.widthPx,
      },
    });
    this.scheduleFetch(0);
    const refreshS = Math.min(60, Math.max(5, windowS / 100));
    this.refreshTimer = setInterval(() => this.scheduleFetch(0), refreshS * 1000);
    this.liveTimer = setInterval(() => this.advanceLive(), 1000);
  }

  private advanceLive() {
    if (this.snap.mode !== "live") return;
    const end = this.liveEnd();
    const w = this.snap.windowS;
    const start = end - w;
    for (const fin of this.liveFin.values()) {
      let k = 0;
      while (k < fin.t.length && fin.t[k] < start) k++;
      if (k) {
        fin.t.splice(0, k);
        fin.v.splice(0, k);
      }
    }
    this.bumpData({ range: [start, end + w * 0.01] });
  }

  goLive(windowS = this.snap.windowS) {
    this.stopTimers();
    this.startLive(windowS);
    this.onViewChange?.("live", this.snap.range);
  }

  // --- archive mode ---------------------------------------------------------------

  private startArchive(range: [number, number]) {
    this.send({ type: "live-stop" });
    this.emit({ mode: "archive", range });
    this.bumpData();
    this.scheduleFetch(0);
  }

  /** Show [a, b]; switches to archive mode (zoom/pan freeze the live view). */
  zoomTo(a: number, b: number) {
    if (!(b > a)) return;
    const minSpan = 0.05;
    if (b - a < minSpan) {
      const c = (a + b) / 2;
      a = c - minSpan / 2;
      b = c + minSpan / 2;
    }
    if (this.snap.mode === "live") {
      this.stopTimers();
      this.send({ type: "live-stop" });
    }
    this.emit({ mode: "archive", range: [a, b] });
    this.bumpData();
    this.scheduleFetch(FETCH_DEBOUNCE_MS);
    this.onViewChange?.("archive", [a, b]);
  }

  zoomAround(center: number, factor: number) {
    const [a, b] = this.snap.range;
    this.zoomTo(center - (center - a) * factor, center + (b - center) * factor);
  }

  panBy(fraction: number) {
    const [a, b] = this.snap.range;
    const d = (b - a) * fraction;
    this.zoomTo(a + d, b + d);
  }

  resetZoom() {
    if (this.snap.mode === "live") {
      this.goLive();
      return;
    }
    const r = this.snap.dataRange;
    if (r) this.zoomTo(r[0], r[1]);
  }

  // --- data requests -------------------------------------------------------------

  private scheduleFetch(delayMs: number) {
    if (this.fetchTimer) clearTimeout(this.fetchTimer);
    this.fetchTimer = setTimeout(() => {
      this.fetchTimer = null;
      this.fetchHistory();
    }, delayMs);
  }

  private fetchHistory() {
    if (!this.channels.length) {
      this.emit({ loading: false });
      return;
    }
    let [a, b] = this.snap.range;
    if (this.snap.mode === "live") {
      const end = this.liveEnd();
      a = end - this.snap.windowS;
      b = end + 60;
    }
    if (this.histReq) this.send({ type: "cancel", reqId: this.histReq });
    const reqId = ++this.reqSeq;
    this.histReq = reqId;
    this.reqStarted.set(reqId, performance.now());
    this.emit({ loading: true });
    this.send({ type: "series", reqId, url: api.seriesUrl(this.channels, a * 1e6, b * 1e6, this.widthPx) });
    void this.loadModes(a, b);
  }

  private async loadModes(a: number, b: number) {
    try {
      this.overlays = { ...this.overlays, modes: await api.modes(a * 1e6, b * 1e6) };
      this.bumpOverlay();
    } catch {
      /* modes are optional */
    }
  }

  async loadOverview() {
    if (!this.channels.length) return;
    let lo = Infinity;
    let hi = -Infinity;
    try {
      const stats = await api.channelsStats();
      for (const s of stats) {
        if (this.channels.includes(s.id)) {
          lo = Math.min(lo, s.first_ts / 1e6);
          hi = Math.max(hi, s.last_ts / 1e6);
        }
      }
    } catch {
      /* archive may be unavailable */
    }
    if (!Number.isFinite(lo)) {
      hi = Date.now() / 1000;
      lo = hi - 86400;
    }
    hi = Math.max(hi, this.latestDataT);
    this.emit({ dataRange: [lo, hi] });
    const reqId = ++this.reqSeq;
    this.overviewReq = reqId;
    this.send({ type: "series", reqId, url: api.seriesUrl(this.channels, lo * 1e6, hi * 1e6 + 1, 1000) });
  }

  private onWorker(msg: FromWorker) {
    switch (msg.type) {
      case "series":
        if (msg.reqId === this.overviewReq) this.applyOverview(msg.payload.series);
        else if (msg.reqId === this.histReq) this.applyHistory(msg.reqId, msg.payload);
        break;
      case "series-error":
        if (msg.reqId === this.histReq) this.emit({ loading: false, error: msg.error });
        break;
      case "live-snapshot":
        this.applyLive(msg.series, true);
        break;
      case "live-update":
        this.applyLive(msg.series, false);
        if (msg.events.length) this.applyLiveEvents(msg.events);
        if (msg.tIng) this.emit({ liveLatencyMs: (msg.tRecv - msg.tIng) / 1000 });
        break;
      case "ws-status":
        this.emit({ wsStatus: msg.status });
        break;
    }
  }

  private applyOverview(series: SeriesData[]) {
    this.overview.clear();
    for (const s of series) this.overview.set(s.channel, { t: s.t, v: s.v });
    this.emit({ overviewVersion: this.snap.overviewVersion + 1 });
  }

  private applyHistory(
    reqId: number,
    p: { series: SeriesData[]; events: EventItem[]; flags: FlagPoint[]; alarms: Alarm[]; eventsTotal: number; flagsTotal: number; timingMs: Record<string, number> },
  ) {
    const started = this.reqStarted.get(reqId);
    this.reqStarted.delete(reqId);
    this.histReq = 0;
    const meta: Record<string, ChannelMeta> = {};
    const last = { ...this.snap.last };
    for (const s of p.series) {
      this.hist.set(s.channel, { t: s.t, v: s.v });
      meta[s.channel] = { n: s.n, m: s.m, eta: s.eta, source: s.source, level: s.level, vmin: s.vmin, vmax: s.vmax, mean: s.mean, shown: s.t.length };
      if (s.t.length) {
        const tl = s.t[s.t.length - 1];
        if (!last[s.channel] || last[s.channel].t <= tl) last[s.channel] = { t: tl, v: s.v[s.v.length - 1] };
        // live points older than the refreshed history are now covered by it
        const fin = this.liveFin.get(s.channel);
        if (fin) {
          let k = 0;
          while (k < fin.t.length && fin.t[k] <= tl) k++;
          fin.t.splice(0, k);
          fin.v.splice(0, k);
        }
      }
    }
    const liveFlags = this.snap.mode === "live" ? this.overlays.flags.filter((f) => !p.flags.some((g) => g.ts === f.ts && g.channel === f.channel)) : [];
    this.overlays = { ...this.overlays, events: p.events, flags: [...p.flags, ...liveFlags].slice(-MAX_LIVE_FLAGS), alarms: p.alarms };
    this.emit({
      loading: false,
      error: null,
      meta,
      last,
      fetchMs: started ? performance.now() - started : null,
      serverMs: p.timingMs.total ?? null,
      layerCounts: { events: p.eventsTotal, eventsShown: p.events.length, flags: p.flagsTotal, flagsShown: p.flags.length },
    });
    this.bumpOverlay();
    this.bumpData();
  }

  private applyLive(series: LiveSeries[], snapshot: boolean) {
    if (this.snap.mode !== "live") return;
    const last = { ...this.snap.last };
    for (const s of series) {
      const histEnd = (() => {
        const h = this.hist.get(s.channel);
        return h && h.t.length ? h.t[h.t.length - 1] : -Infinity;
      })();
      let fin = this.liveFin.get(s.channel);
      if (!fin || snapshot) {
        fin = { t: [], v: [] };
        this.liveFin.set(s.channel, fin);
      }
      const lastFin = fin.t.length ? fin.t[fin.t.length - 1] : histEnd;
      for (let i = 0; i < s.t.length; i++) {
        if (s.t[i] > lastFin && s.t[i] > histEnd) {
          fin.t.push(s.t[i]);
          fin.v.push(s.v[i]);
        }
      }
      this.liveTail.set(s.channel, { t: s.tailT, v: s.tailV });
      const tEnd = s.tailT.length ? s.tailT[s.tailT.length - 1] : fin.t[fin.t.length - 1];
      const vEnd = s.tailT.length ? s.tailV[s.tailV.length - 1] : fin.v[fin.v.length - 1];
      if (tEnd !== undefined) {
        this.latestDataT = Math.max(this.latestDataT, tEnd);
        last[s.channel] = { t: tEnd, v: vEnd };
      }
    }
    const end = this.liveEnd();
    const w = this.snap.windowS;
    this.bumpData({ last, range: [end - w, end + w * 0.01] });
  }

  private applyLiveEvents(events: LiveEvent[]) {
    let { flags, events: evs, alarms } = this.overlays;
    for (const e of events) {
      if (e.type === "flags") {
        flags = flags.concat(
          e.points.map((p) => ({
            channel: e.channel, ts: p.ts, val: p.val, val_f: p.val_f, pred: null, resid: null, thr: null, prob: null,
            substituted: p.substituted, anomaly: p.anomaly,
          })),
        );
      } else if (e.type === "event") {
        evs = [e, ...evs];
      } else if (e.type === "alarm") {
        const idx = alarms.findIndex((a) => a.id === e.alarm_id);
        const prev = idx >= 0 ? alarms[idx] : null;
        const updated: Alarm = {
          id: e.alarm_id,
          rule_id: e.rule_id,
          channel_id: e.channel,
          state: e.state as Alarm["state"],
          priority: e.priority,
          ts_active: prev?.ts_active ?? e.ts,
          ts_return: e.reason === "returned" ? e.ts : (prev?.ts_return ?? null),
          ts_ack: e.reason === "acknowledged" ? e.ts : (prev?.ts_ack ?? null),
          acked_by: prev?.acked_by ?? null,
          value: e.value ?? prev?.value ?? null,
        };
        alarms = idx >= 0 ? alarms.map((a, i) => (i === idx ? updated : a)) : [updated, ...alarms];
      }
    }
    this.overlays = { ...this.overlays, flags: flags.slice(-MAX_LIVE_FLAGS), events: evs, alarms };
    this.bumpOverlay();
  }

  setPreviews(previews: PreviewLayer[]) {
    this.overlays = { ...this.overlays, previews };
    this.bumpOverlay();
  }

  async refreshModes() {
    const [a, b] = this.snap.range;
    await this.loadModes(a, b);
  }

  // --- chart data -------------------------------------------------------------------

  getChannels() {
    return this.channels;
  }

  private channelArr(ch: string): Arr {
    const h = this.hist.get(ch) ?? { t: new Float64Array(0), v: new Float64Array(0) };
    if (this.snap.mode !== "live") return h;
    const fin = this.liveFin.get(ch);
    const parts: Arr[] = [h];
    let lastT = h.t.length ? h.t[h.t.length - 1] : -Infinity;
    if (fin && fin.t.length) {
      parts.push({ t: Float64Array.from(fin.t), v: Float64Array.from(fin.v) });
      lastT = fin.t[fin.t.length - 1];
    }
    const tail = this.liveTail.get(ch);
    if (tail) parts.push(sliceAfter(tail, lastT));
    return parts.length === 1 ? h : concat(parts);
  }

  getChartData(): ChartData {
    if (this.chartCache && this.chartCache.version === this.snap.version) return this.chartCache.data;
    const chs = this.channels;
    const joined = chs.length ? joinSeries(chs.map((c) => this.channelArr(c))) : { x: [], ys: [] };
    const data = { channels: chs, ...joined };
    this.chartCache = { version: this.snap.version, data };
    return data;
  }

  /** Data of a single channel (stacked layout). */
  getChannelData(ch: string): ChartData {
    const j = joinSeries([this.channelArr(ch)]);
    return { channels: [ch], ...j };
  }

  getOverviewData(): ChartData {
    if (this.overviewCache && this.overviewCache.version === this.snap.overviewVersion) return this.overviewCache.data;
    const chs = this.channels.filter((c) => this.overview.has(c));
    const joined = chs.length ? joinSeries(chs.map((c) => this.overview.get(c)!)) : { x: [], ys: [] };
    const data = { channels: chs, ...joined };
    this.overviewCache = { version: this.snap.overviewVersion, data };
    return data;
  }
}

export const engine = new DataEngine();

// dev-only handle for debugging from the browser console / DevTools protocol
if (import.meta.env.DEV) (globalThis as unknown as { __tsa: DataEngine }).__tsa = engine;
