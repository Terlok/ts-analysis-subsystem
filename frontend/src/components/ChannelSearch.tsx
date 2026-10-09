// Search of available parameters by name / id with a short suggestion list (at most 5).
import { useMemo, useRef, useState } from "react";
import type { Channel, ChannelState } from "../api/types";
import { fmtNum } from "../lib/format";

const MAX_SUGGESTIONS = 5;

interface Props {
  channels: Channel[];
  states: Record<string, ChannelState>;
  exclude: string[];
  onPick: (id: string) => void;
}

function score(c: Channel, q: string): number {
  const fields = [c.id, c.name ?? "", c.description ?? "", c.group_name ?? ""].map((f) => f.toLowerCase());
  let best = -1;
  fields.forEach((f, i) => {
    const pos = f.indexOf(q);
    if (pos < 0) return;
    // prefix matches first, then earlier fields (id, name) before description/group
    const s = (pos === 0 ? 100 : 50) - i * 10 - Math.min(pos, 20);
    best = Math.max(best, s);
  });
  return best;
}

export function ChannelSearch({ channels, states, exclude, onPick }: Props) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const { items, total, onChart } = useMemo(() => {
    const query = q.trim().toLowerCase();
    const onChart = query ? channels.filter((c) => exclude.includes(c.id) && score(c, query) >= 0).map((c) => c.id) : [];
    const pool = channels.filter((c) => !exclude.includes(c.id));
    const ranked = query
      ? pool
          .map((c) => ({ c, s: score(c, query) }))
          .filter((x) => x.s >= 0)
          .sort((a, b) => b.s - a.s || a.c.id.localeCompare(b.c.id))
          .map((x) => x.c)
      : [...pool].sort((a, b) => a.id.localeCompare(b.id));
    return { items: ranked.slice(0, MAX_SUGGESTIONS), total: ranked.length, onChart };
  }, [q, channels, exclude]);

  const pick = (id: string) => {
    onPick(id);
    setQ("");
    setActive(0);
    setOpen(false);
    inputRef.current?.blur();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      setOpen(true);
      setActive((a) => Math.min(a + 1, items.length - 1));
      e.preventDefault();
    } else if (e.key === "ArrowUp") {
      setActive((a) => Math.max(a - 1, 0));
      e.preventDefault();
    } else if (e.key === "Enter") {
      if (items[active]) pick(items[active].id);
      else if (q.trim() && channels.some((c) => c.id === q.trim())) pick(q.trim());
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div className="search">
      <input
        ref={inputRef}
        className="input"
        placeholder="Пошук параметра…"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={onKey}
        style={{ width: 220 }}
      />
      {open && (
        <div className="suggest">
          {items.map((c, i) => {
            const st = states[c.id];
            return (
              <div
                key={c.id}
                className={`suggest-item ${i === active ? "active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(c.id);
                }}
                onMouseEnter={() => setActive(i)}
              >
                <div className="suggest-main">
                  <b>{c.name && c.name !== c.id ? c.name : c.id}</b>
                  {c.name && c.name !== c.id && <span className="muted"> {c.id}</span>}
                </div>
                <div className="suggest-side muted">
                  {st?.last_val != null ? `${fmtNum(st.last_val, 2)}${c.unit ? ` ${c.unit}` : ""}` : c.unit ?? ""}
                </div>
              </div>
            );
          })}
          {items.length === 0 && (
            <div className="suggest-empty muted">
              {onChart.length ? `Вже на графіку: ${onChart.slice(0, 3).join(", ")}${onChart.length > 3 ? "…" : ""}` : "Нічого не знайдено"}
            </div>
          )}
          {total > items.length && <div className="suggest-empty muted">ще {total - items.length} — уточніть запит</div>}
        </div>
      )}
    </div>
  );
}
