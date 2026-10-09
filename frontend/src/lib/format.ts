// Formatting helpers. Chart time is in seconds; API time is in microseconds.

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

export function fmtTime(sec: number, withSeconds = true): string {
  const d = new Date(sec * 1000);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withSeconds ? `${hm}:${pad(d.getSeconds())}` : hm;
}

export function fmtDate(sec: number): string {
  const d = new Date(sec * 1000);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
}

export function fmtDateTime(sec: number, ms = false): string {
  const d = new Date(sec * 1000);
  const base = `${fmtDate(sec)} ${fmtTime(sec)}`;
  return ms ? `${base}.${pad(d.getMilliseconds(), 3)}` : base;
}

export const usToS = (us: number) => us / 1e6;
export const sToUs = (s: number) => Math.round(s * 1e6);

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(2);
  return v.toFixed(digits);
}

export function fmtDuration(sec: number): string {
  if (sec < 1) return `${(sec * 1000).toFixed(0)} мс`;
  if (sec < 60) return `${sec.toFixed(1)} с`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)} хв`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} год`;
  return `${(sec / 86400).toFixed(1)} доби`;
}

export function fmtBytes(b: number | null | undefined): string {
  if (!b) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) {
    b /= 1024;
    i++;
  }
  return `${b.toFixed(1)} ${units[i]}`;
}

/** Value for <input type="datetime-local"> in local time. */
export function toLocalInput(sec: number): string {
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fromLocalInput(v: string): number | null {
  const t = new Date(v).getTime();
  return Number.isFinite(t) ? t / 1000 : null;
}

export const QUALITY_UA: Record<string, string> = {
  good: "норма",
  uncertain: "сумнівне",
  bad: "недостовірне",
  substituted: "замінене",
};

export const ALARM_STATE_UA: Record<string, string> = {
  normal: "норма",
  unack_active: "активна, не квитована",
  ack_active: "активна, квитована",
  unack_rtn: "зникла, не квитована",
};

export const ALARM_KIND_UA: Record<string, string> = {
  hihi: "HH",
  hi: "H",
  lo: "L",
  lolo: "LL",
  anomaly: "аномалія",
};
