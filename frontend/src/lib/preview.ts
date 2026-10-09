// Run the diagnostic pipeline over an interval (no persistence) and overlay the result.
import { api } from "../api/client";
import type { AnalysisPreviewRequest, AnalysisPreviewResponse } from "../api/types";
import { engine, type PreviewLayer } from "./engine";

export const PREVIEW_COLORS = ["#8a3fd1", "#0f8c84"]; // model A, model B

export function toLayer(r: AnalysisPreviewResponse, key: string, label: string, color: string): PreviewLayer {
  // an older backend does not return the forecast: only the flags are drawn then
  const ft = r.forecast_t ?? [];
  return { key, label, color, channel: r.channel, flags: r.flags, t: ft.map((t) => t / 1e6), v: r.forecast_v ?? [] };
}

/** Run one preview and show it as the only layer. */
export async function runPreview(req: AnalysisPreviewRequest, label = "модель"): Promise<AnalysisPreviewResponse> {
  const r = await api.analysisPreview(req);
  engine.setPreviews([toLayer(r, "A", label, PREVIEW_COLORS[0])]);
  return r;
}

export function clearPreview() {
  engine.setPreviews([]);
}

export function gainPercent(model: number | null, naive: number | null): number | null {
  if (model == null || naive == null || naive <= 0) return null;
  return (1 - model / naive) * 100;
}

/** "MAE 0.038 проти 0.048 у наївного (−21%)" */
export function maeText(model: number | null, naive: number | null): string {
  const g = gainPercent(model, naive);
  if (model == null || naive == null || g == null) return "";
  return `MAE прогнозу ${model.toPrecision(3)} проти ${naive.toPrecision(3)} у наївного (${g >= 0 ? "−" : "+"}${Math.abs(g).toFixed(1)}%)`;
}
