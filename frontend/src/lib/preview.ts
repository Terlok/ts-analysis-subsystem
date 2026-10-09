// Run the diagnostic pipeline over an interval (no persistence) and overlay the result.
import { api } from "../api/client";
import type { AnalysisPreviewRequest, AnalysisPreviewResponse } from "../api/types";
import { engine } from "./engine";

export async function runPreview(req: AnalysisPreviewRequest): Promise<AnalysisPreviewResponse> {
  const r = await api.analysisPreview(req);
  // an older backend does not return the forecast: draw only the flags then
  const ft = r.forecast_t ?? [];
  engine.setPreview(r.flags, ft.length ? { channel: r.channel, t: ft.map((t) => t / 1e6), v: r.forecast_v ?? [] } : null);
  return r;
}

export function clearPreview() {
  engine.setPreview([], null);
}

/** "MAE 0.038 проти 0.048 у наївного (−21%)" */
export function maeText(model: number | null, naive: number | null): string {
  if (model == null || naive == null) return "";
  const gain = naive > 0 ? (1 - model / naive) * 100 : 0;
  const sign = gain >= 0 ? "−" : "+";
  return `MAE прогнозу ${model.toPrecision(3)} проти ${naive.toPrecision(3)} у наївного (${sign}${Math.abs(gain).toFixed(1)}%)`;
}
