// Messages between the main thread and the data worker.
// Time in typed arrays is in SECONDS (uPlot convention); NaN marks missing values.
import type { Alarm, EventItem, FlagPoint } from "../api/types";

export interface SeriesData {
  channel: string;
  source: string;
  level: number | null;
  n: number;
  m: number;
  eta: number;
  t: Float64Array;
  v: Float64Array;
  vmin: number | null;
  vmax: number | null;
  mean: number | null;
}

export interface SeriesPayload {
  tFrom: number; // seconds
  tTo: number;
  series: SeriesData[];
  events: EventItem[];
  flags: FlagPoint[];
  alarms: Alarm[];
  eventsTotal: number;
  flagsTotal: number;
  timingMs: Record<string, number>;
  parseMs: number;
}

export interface LiveSeries {
  channel: string;
  t: Float64Array; // finalized points (append)
  v: Float64Array;
  tailT: Float64Array; // provisional tail (replaces the previous tail)
  tailV: Float64Array;
}

export interface LiveSubscription {
  channels: string[];
  window_s: number;
  width_px: number;
}

export type LiveEvent =
  | ({ type: "flags"; channel: string; points: Array<{ ts: number; val: number | null; val_f: number | null; substituted: boolean; anomaly: boolean }> })
  | ({ type: "event"; channel: string } & EventItem)
  | ({ type: "alarm"; channel: string; alarm_id: number; rule_id: number; kind: string; priority: number; ts: number; state: string; prev_state: string; reason: string; value: number | null });

export type ToWorker =
  | { type: "series"; reqId: number; url: string }
  | { type: "cancel"; reqId: number }
  | { type: "live-subscribe"; sub: LiveSubscription }
  | { type: "live-stop" };

export type WsStatus = "offline" | "connecting" | "online";

export type FromWorker =
  | { type: "series"; reqId: number; payload: SeriesPayload }
  | { type: "series-error"; reqId: number; error: string }
  | { type: "live-snapshot"; deltaS: number; series: LiveSeries[] }
  | { type: "live-update"; series: LiveSeries[]; events: LiveEvent[]; tSend: number; tIng: number | null; tRecv: number }
  | { type: "ws-status"; status: WsStatus };
