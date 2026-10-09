// Series palette tuned for the light steel-blue SCADA background; distinct in hue and lightness.
export const SERIES_COLORS = ["#2f6f9f", "#c0562b", "#3f8a4a", "#7a4fa8", "#a8871c", "#1f8f8f", "#b03a6b", "#5b6b7a"];

export const seriesColor = (i: number) => SERIES_COLORS[i % SERIES_COLORS.length];

export const OVERLAY = {
  anomaly: "#d23c3c",
  anomalyBand: "rgba(210, 60, 60, 0.12)",
  outlier: "#d98a1c",
  outlierBand: "rgba(217, 138, 28, 0.10)",
  preview: "#8a3fd1",
  mark: "#33566f",
  alarm: { 1: "#c62828", 2: "#e0701f", 3: "#d7b21f", 4: "#6d8fa8" } as Record<number, string>,
};
