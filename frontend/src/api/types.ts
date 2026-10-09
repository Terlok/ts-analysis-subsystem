// Types mirroring backend/app/schemas.py. Timestamps from the API are int64 microseconds.

export type Micros = number;

export interface Channel {
  id: string;
  name: string | null;
  description: string | null;
  unit: string | null;
  x_min: number | null;
  x_max: number | null;
  f_nominal_hz: number | null;
  deadband: number | null;
  source: string | null;
  source_field: string | null;
  group_name: string | null;
  hampel_window: number | null;
  hampel_kappa: number | null;
  hampel_min_sigma: number | null;
  feature_window: number | null;
  residual_k: number | null;
  analytics_enabled: boolean | null;
  auto_registered: boolean;
}

export type ChannelPatch = Partial<Omit<Channel, "id" | "auto_registered">>;

export interface ChannelState {
  id: string;
  last_ts: Micros | null;
  last_val: number | null;
  last_quality: string | null;
}

export interface ChannelStats {
  id: string;
  count: number;
  first_ts: Micros;
  last_ts: Micros;
}

export interface SeriesJson {
  channel: string;
  source: "raw" | "hot" | "agg" | "empty";
  level: number | null;
  n: number;
  m: number;
  eta: number;
  t: Micros[];
  v: (number | null)[];
  q: string[] | null;
  vmin: number | null;
  vmax: number | null;
  mean: number | null;
}

export interface EventItem {
  id: number;
  channel_id: string;
  kind: "anomaly" | "outlier" | string;
  ts_start: Micros;
  ts_end: Micros;
  n_points: number;
  peak_ts: Micros;
  peak_value: number | null;
  score: number | null;
  run: string;
  model_version: string | null;
  details: Record<string, unknown> | null;
}

export interface FlagPoint {
  channel: string;
  ts: Micros;
  val: number | null;
  val_f: number | null;
  pred: number | null;
  resid: number | null;
  thr: number | null;
  prob: number | null;
  substituted: boolean;
  anomaly: boolean;
}

export type AlarmStateName = "normal" | "unack_active" | "ack_active" | "unack_rtn";

export interface Alarm {
  id: number;
  rule_id: number;
  channel_id: string;
  state: AlarmStateName;
  priority: number;
  ts_active: Micros;
  ts_return: Micros | null;
  ts_ack: Micros | null;
  acked_by: string | null;
  value: number | null;
}

export interface AlarmLogItem {
  id: number;
  alarm_id: number;
  rule_id: number;
  channel_id: string;
  ts: Micros;
  prev_state: string;
  new_state: string;
  reason: string;
  value: number | null;
  user: string | null;
}

export type AlarmKind = "hihi" | "hi" | "lo" | "lolo" | "anomaly";

export interface AlarmRuleIn {
  channel_id: string;
  kind: AlarmKind;
  limit: number | null;
  deadband: number;
  on_delay_ms: number;
  off_delay_ms: number;
  min_repeat_ms: number;
  priority: number;
  enabled: boolean;
  description: string | null;
}

export interface AlarmRule extends AlarmRuleIn {
  id: number;
}

export interface SeriesResponseJson {
  t_from: Micros;
  t_to: Micros;
  width_px: number;
  series: SeriesJson[];
  events: EventItem[];
  flags: FlagPoint[];
  alarms: Alarm[];
  events_total: number;
  flags_total: number;
  timing_ms: Record<string, number>;
}

export interface RawPoint {
  ts: Micros;
  val: number | null;
  quality: string;
  nd: boolean;
  otkl: number;
  substituted: boolean;
  val_f: number | null;
}

export interface ModeChange {
  id: number;
  robot: string;
  mode: string;
  ts: Micros;
  details: Record<string, unknown> | null;
}

export interface ModelVersion {
  id: number;
  channel_id: string;
  kind: string;
  version: string;
  path: string;
  params: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  active: boolean;
}

export interface AnalysisPreviewRequest {
  channel: string;
  t_from: Micros;
  t_to: Micros;
  hampel_window?: number | null;
  hampel_kappa?: number | null;
  feature_window?: number | null;
  residual_k?: number | null;
  use_model: boolean;
  model_id?: number | null;
}

export interface AnalysisPreviewResponse {
  channel: string;
  n: number;
  substituted: number;
  anomalies: number;
  model: string;
  episodes: Array<{ kind: string; ts_start: Micros; ts_end: Micros; n_points: number; peak_value: number; peak_score: number }>;
  flags: FlagPoint[];
  elapsed_ms: number;
  forecast_t: Micros[];
  forecast_v: number[];
  mae_model: number | null;
  mae_naive: number | null;
}

export interface Health {
  ok: boolean;
  redis: boolean;
  postgres: boolean;
  questdb: boolean;
}

export interface LatencyStats {
  n: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  max_ms: number;
}

export interface Metrics {
  latency: Record<string, { description: string; stats: LatencyStats | null }>;
  workers: Record<string, Record<string, number>>;
  stream: { length?: number; groups: Array<{ name: string; pending: number; lag: number | null }> };
  tiles: { hit: number; miss: number; hit_ratio: number | null };
  redis: { used_memory_bytes: number; maxmemory_bytes: number };
}

export interface ClientConfig {
  detail_c: number;
  preselect_rho: number;
  max_width_px: number;
  hot_window_s: number;
  live_rate_hz: number;
  live_max_window_s: number;
  grid: { delta0_us: number; base: number; levels: number[]; tile_buckets: number };
}

export interface TrainingJob {
  id: string;
  channel: string;
  t_from: Micros | null;
  t_to: Micros | null;
  algorithm: string;
  status: "queued" | "running" | "done" | "error";
  created_at: number; // unix seconds
  finished_at: number | null;
  result: { model_id: number; version: string; path: string; active: boolean; metrics: Record<string, number> } | null;
  error: string | null;
}
