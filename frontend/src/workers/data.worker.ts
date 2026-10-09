/// <reference lib="webworker" />
// Data worker: network I/O and deserialization off the main thread (thesis 1.5.4).
// - /api/series responses are parsed here and converted into Float64Arrays that are
//   transferred (not copied) to the main thread;
// - the live WebSocket (/ws/stream) lives here as well, with reconnection.
import type { FromWorker, LiveSeries, LiveSubscription, SeriesData, ToWorker, WsStatus } from "./protocol";
import type { SeriesResponseJson } from "../api/types";

declare const self: DedicatedWorkerGlobalScope;

const US = 1e6;
const inflight = new Map<number, AbortController>();

function post(msg: FromWorker, transfer: Transferable[] = []) {
  self.postMessage(msg, transfer);
}

function toSeconds(ts: number[]): Float64Array {
  const out = new Float64Array(ts.length);
  for (let i = 0; i < ts.length; i++) out[i] = ts[i] / US;
  return out;
}

function toValues(v: (number | null)[]): Float64Array {
  const out = new Float64Array(v.length);
  for (let i = 0; i < v.length; i++) out[i] = v[i] ?? NaN;
  return out;
}

async function loadSeries(reqId: number, url: string) {
  const ctrl = new AbortController();
  inflight.set(reqId, ctrl);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const t0 = performance.now();
    const j = (await res.json()) as SeriesResponseJson;
    const series: SeriesData[] = j.series.map((s) => ({
      channel: s.channel,
      source: s.source,
      level: s.level,
      n: s.n,
      m: s.m,
      eta: s.eta,
      t: toSeconds(s.t),
      v: toValues(s.v),
      vmin: s.vmin,
      vmax: s.vmax,
      mean: s.mean,
    }));
    const transfer = series.flatMap((s) => [s.t.buffer, s.v.buffer]);
    post(
      {
        type: "series",
        reqId,
        payload: {
          tFrom: j.t_from / US,
          tTo: j.t_to / US,
          series,
          events: j.events,
          flags: j.flags,
          alarms: j.alarms,
          eventsTotal: j.events_total ?? j.events.length,
          flagsTotal: j.flags_total ?? j.flags.length,
          timingMs: j.timing_ms,
          parseMs: performance.now() - t0,
        },
      },
      transfer,
    );
  } catch (e) {
    if (!ctrl.signal.aborted) post({ type: "series-error", reqId, error: e instanceof Error ? e.message : String(e) });
  } finally {
    inflight.delete(reqId);
  }
}

// --- live stream -------------------------------------------------------------

let ws: WebSocket | null = null;
let sub: LiveSubscription | null = null;
let wanted = false;
let backoff = 500;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

function setStatus(status: WsStatus) {
  post({ type: "ws-status", status });
}

function wsUrl(): string {
  const proto = self.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${self.location.host}/ws/stream`;
}

function convertLive(list: Array<{ channel: string; t: number[]; v: number[]; tail_t: number[]; tail_v: number[] }>) {
  const out: LiveSeries[] = list.map((s) => ({
    channel: s.channel,
    t: toSeconds(s.t),
    v: toValues(s.v),
    tailT: toSeconds(s.tail_t),
    tailV: toValues(s.tail_v),
  }));
  const transfer = out.flatMap((s) => [s.t.buffer, s.v.buffer, s.tailT.buffer, s.tailV.buffer]);
  return { out, transfer };
}

function connect() {
  if (!wanted || !sub) return;
  setStatus("connecting");
  const sock = new WebSocket(wsUrl());
  sock.binaryType = "arraybuffer";
  ws = sock;
  sock.onopen = () => {
    backoff = 500;
    setStatus("online");
    if (sub) sock.send(JSON.stringify({ op: "subscribe", ...sub }));
  };
  sock.onmessage = (ev) => {
    const text = typeof ev.data === "string" ? ev.data : new TextDecoder().decode(ev.data as ArrayBuffer);
    const msg = JSON.parse(text);
    if (msg.type === "snapshot") {
      const { out, transfer } = convertLive(msg.series);
      post({ type: "live-snapshot", deltaS: msg.delta_us / US, series: out }, transfer);
    } else if (msg.type === "update") {
      const { out, transfer } = convertLive(msg.series);
      post(
        { type: "live-update", series: out, events: msg.events ?? [], tSend: msg.t_send, tIng: msg.t_ing, tRecv: Date.now() * 1000 },
        transfer,
      );
    }
  };
  sock.onclose = () => {
    if (ws === sock) ws = null;
    if (!wanted) {
      setStatus("offline");
      return;
    }
    setStatus("connecting");
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10_000);
  };
  sock.onerror = () => sock.close();
}

function stopLive() {
  wanted = false;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  ws?.close();
  ws = null;
  setStatus("offline");
}

self.onmessage = (ev: MessageEvent<ToWorker>) => {
  const msg = ev.data;
  switch (msg.type) {
    case "series":
      void loadSeries(msg.reqId, msg.url);
      break;
    case "cancel":
      inflight.get(msg.reqId)?.abort();
      break;
    case "live-subscribe":
      sub = msg.sub;
      wanted = true;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ op: "subscribe", ...sub }));
      else if (!ws) connect();
      break;
    case "live-stop":
      stopLive();
      break;
  }
};
