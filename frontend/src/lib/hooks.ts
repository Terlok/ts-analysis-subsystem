import { createContext, useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Channel, ChannelState, Health } from "../api/types";
import { engine } from "./engine";
import { workspaces } from "./workspaces";

export const useEngine = () => useSyncExternalStore(engine.subscribe, engine.getSnapshot);
export const useWorkspaces = () => useSyncExternalStore(workspaces.subscribe, workspaces.get);

/** Calls `fn` now and every `ms` while mounted (and `enabled`). */
export function usePolling<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = [], enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const run = async () => {
      try {
        const d = await fnRef.current();
        if (alive) {
          setData(d);
          setError(null);
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void run();
    const id = ms > 0 ? setInterval(run, ms) : null;
    return () => {
      alive = false;
      if (id) clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled, tick, ...deps]);
  return { data, error, reload };
}

export interface AppData {
  channels: Channel[];
  reloadChannels: () => void;
  states: Record<string, ChannelState>;
  health: Health | null;
  colorOf: (channel: string) => string;
}

export const AppDataContext = createContext<AppData | null>(null);

export function useAppData(): AppData {
  const v = useContext(AppDataContext);
  if (!v) throw new Error("AppDataContext missing");
  return v;
}

/** Height of an element, tracked with ResizeObserver. */
export function useElementHeight<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [h, setH] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setH(el.clientHeight));
    ro.observe(el);
    setH(el.clientHeight);
    return () => ro.disconnect();
  }, []);
  return [ref, h];
}
