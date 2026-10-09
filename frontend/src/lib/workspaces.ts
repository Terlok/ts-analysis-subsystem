// Workspaces ("Вікно 1", "Вікно 2", ...): independent sets of datasets and view settings,
// persisted per browser in localStorage.

export type Mode = "live" | "archive";
export type Layout = "single" | "stacked";

export interface Workspace {
  id: string;
  name: string;
  channels: string[];
  mode: Mode;
  windowS: number; // live window (0.5h .. 24h)
  archive: [number, number] | null; // last archive range, seconds
  limits: Record<string, [number | null, number | null]>; // y-axis limits per channel ("Межі")
  layout: Layout;
}

export interface WorkspacesState {
  items: Workspace[];
  activeId: string;
}

const KEY = "tsa.workspaces.v1";
export const WINDOW_PRESETS_H = [0.5, 1, 3, 12, 24];

let counter = 0;
const newId = () => `w${Date.now().toString(36)}${(counter++).toString(36)}`;

export function newWorkspace(n: number): Workspace {
  return { id: newId(), name: `Вікно ${n}`, channels: [], mode: "live", windowS: 1800, archive: null, limits: {}, layout: "single" };
}

function load(): WorkspacesState {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const s = JSON.parse(raw) as WorkspacesState;
      if (s.items?.length && s.items.some((w) => w.id === s.activeId)) return s;
    }
  } catch {
    /* storage unavailable or corrupted */
  }
  const w = newWorkspace(1);
  return { items: [w], activeId: w.id };
}

/** A link like ?channels=A,B&mode=archive&window=3 opens a new workspace with that view. */
function fromUrl(s: WorkspacesState): WorkspacesState {
  let q: URLSearchParams;
  try {
    q = new URLSearchParams(window.location.search);
  } catch {
    return s;
  }
  const channels = (q.get("channels") ?? "").split(",").filter(Boolean);
  if (!channels.length) return s;
  // apply once: reloading the page must not create another workspace
  try {
    window.history.replaceState(null, "", window.location.pathname);
  } catch {
    /* ignore */
  }
  const existing =
    s.items.find((x) => x.channels.join(",") === channels.join(",")) ??
    (s.items.length === 1 && !s.items[0].channels.length ? s.items[0] : undefined); // reuse a fresh empty one
  const w = existing ? { ...existing } : newWorkspace(s.items.length + 1);
  w.channels = channels;
  if (q.get("mode") === "archive") w.mode = "archive";
  const h = Number(q.get("window"));
  if (h > 0) w.windowS = h * 3600;
  const from = Number(q.get("from"));
  const to = Number(q.get("to"));
  if (from > 0 && to > from) {
    w.mode = "archive";
    w.archive = [from, to];
  }
  return {
    items: existing ? s.items.map((x) => (x.id === w.id ? w : x)) : [...s.items, w],
    activeId: w.id,
  };
}

let state: WorkspacesState = fromUrl(load());
const listeners = new Set<() => void>();

function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* ignore */
  }
}

export const workspaces = {
  get: () => state,
  subscribe(fn: () => void) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  set(next: WorkspacesState) {
    state = next;
    save();
    listeners.forEach((fn) => fn());
  },
  active(): Workspace {
    return state.items.find((w) => w.id === state.activeId) ?? state.items[0];
  },
  update(id: string, patch: Partial<Workspace>) {
    workspaces.set({ ...state, items: state.items.map((w) => (w.id === id ? { ...w, ...patch } : w)) });
  },
  add() {
    const w = newWorkspace(state.items.length + 1);
    workspaces.set({ items: [...state.items, w], activeId: w.id });
  },
  remove(id: string) {
    if (state.items.length <= 1) return;
    const items = state.items.filter((w) => w.id !== id);
    workspaces.set({ items, activeId: state.activeId === id ? items[0].id : state.activeId });
  },
  activate(id: string) {
    workspaces.set({ ...state, activeId: id });
  },
};
