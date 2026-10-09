// Thin typed wrappers over the backend REST API (same origin; Vite forwards /api to the backend).
import type {
  Alarm,
  AlarmLogItem,
  AlarmRule,
  AlarmRuleIn,
  AnalysisPreviewRequest,
  AnalysisPreviewResponse,
  Channel,
  ChannelPatch,
  ChannelState,
  ChannelStats,
  ClientConfig,
  EventItem,
  FlagPoint,
  Health,
  Metrics,
  Micros,
  ModeChange,
  ModelVersion,
  RawPoint,
  TrainingJob,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(method: string, path: string, query?: Query, body?: unknown): Promise<T> {
  const res = await fetch(buildUrl(path, query), {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(path: string, query?: Query) => request<T>("GET", path, query);

export const api = {
  // system
  health: () => get<Health>("/api/health"),
  config: () => get<ClientConfig>("/api/config"),
  metrics: () => get<Metrics>("/api/metrics"),

  // channel registry
  channels: () => get<Channel[]>("/api/channels"),
  channel: (id: string) => get<Channel>(`/api/channels/${encodeURIComponent(id)}`),
  channelsState: () => get<ChannelState[]>("/api/channels/state"),
  channelsStats: () => get<ChannelStats[]>("/api/channels/stats"),
  createChannel: (c: ChannelPatch & { id: string }) => request<Channel>("POST", "/api/channels", undefined, c),
  updateChannel: (id: string, patch: ChannelPatch) =>
    request<Channel>("PATCH", `/api/channels/${encodeURIComponent(id)}`, undefined, patch),
  deleteChannel: (id: string) => request<void>("DELETE", `/api/channels/${encodeURIComponent(id)}`),

  // data (series itself is loaded through the data worker, see workers/data.worker.ts)
  seriesUrl: (channels: string[], from: Micros, to: Micros, width: number, c?: number) =>
    buildUrl("/api/series", { channels: channels.join(","), from: Math.floor(from), to: Math.ceil(to), width, c }),
  raw: (channel: string, from: Micros, to: Micros) =>
    get<RawPoint[]>("/api/raw", { channel, from: Math.floor(from), to: Math.ceil(to), format: "json" }),
  rawCsvUrl: (channel: string, from: Micros, to: Micros) =>
    buildUrl("/api/raw", { channel, from: Math.floor(from), to: Math.ceil(to), format: "csv" }),
  flags: (channels: string[], from: Micros, to: Micros, run = "online") =>
    get<FlagPoint[]>("/api/flags", { channels: channels.join(","), from: Math.floor(from), to: Math.ceil(to), run }),

  // events layer and robot modes
  events: (q: { channels?: string[]; from?: Micros; to?: Micros; kind?: string; run?: string; limit?: number }) =>
    get<EventItem[]>("/api/events", { ...q, channels: q.channels?.join(","), from: q.from, to: q.to }),
  event: (id: number) => get<EventItem>(`/api/events/${id}`),
  modes: (from?: Micros, to?: Micros, robot?: string) => get<ModeChange[]>("/api/modes", { from, to, robot }),
  addMode: (m: { robot?: string; mode: string; ts: Micros; details?: Record<string, unknown> }) =>
    request<ModeChange>("POST", "/api/modes", undefined, m),

  // alarms
  alarms: (q: { channels?: string[]; active?: boolean; state?: string; limit?: number } = {}) =>
    get<Alarm[]>("/api/alarms", { ...q, channels: q.channels?.join(",") }),
  alarmLog: (q: { alarm_id?: number; channels?: string[]; limit?: number } = {}) =>
    get<AlarmLogItem[]>("/api/alarms/log", { ...q, channels: q.channels?.join(",") }),
  ackAlarm: (id: number, user: string) => request<{ accepted: boolean }>("POST", `/api/alarms/${id}/ack`, undefined, { user }),
  alarmRules: (channels?: string[]) => get<AlarmRule[]>("/api/alarms/rules", { channels: channels?.join(",") }),
  createRule: (r: AlarmRuleIn) => request<AlarmRule>("POST", "/api/alarms/rules", undefined, r),
  updateRule: (id: number, r: AlarmRuleIn) => request<AlarmRule>("PUT", `/api/alarms/rules/${id}`, undefined, r),
  deleteRule: (id: number) => request<void>("DELETE", `/api/alarms/rules/${id}`),

  // models and offline analysis
  models: (channel?: string) => get<ModelVersion[]>("/api/models", { channel }),
  activateModel: (id: number) => request<ModelVersion>("POST", `/api/models/${id}/activate`),
  trainModel: (r: {
    channel: string;
    t_from?: Micros | null;
    t_to?: Micros | null;
    trees?: number;
    depth?: number;
    activate?: "auto" | "always" | "never";
    algorithm?: string;
  }) =>
    request<TrainingJob>("POST", "/api/models/train", undefined, r),
  trainingJobs: () => get<TrainingJob[]>("/api/models/jobs"),
  deleteModel: (id: number) => request<void>("DELETE", `/api/models/${id}`),
  modelAlgorithms: () => get<Record<string, string>>("/api/models/algorithms"),
  analysisPreview: (req: AnalysisPreviewRequest) =>
    request<AnalysisPreviewResponse>("POST", "/api/analysis/preview", undefined, req),
};

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return `${e.status}: ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}
