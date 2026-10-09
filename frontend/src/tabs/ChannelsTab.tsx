// Channel (tag) registry: metadata M_i and per-channel analytics parameters.
import { useState } from "react";
import { api, errorText } from "../api/client";
import type { Channel, ChannelPatch } from "../api/types";
import { fmtDateTime, fmtNum, usToS } from "../lib/format";
import { useAppData, usePolling } from "../lib/hooks";
import { workspaces, type Workspace } from "../lib/workspaces";

const NUM_FIELDS: Array<[keyof ChannelPatch, string, string]> = [
  ["x_min", "x min", "Нижня межа допустимого діапазону"],
  ["x_max", "x max", "Верхня межа допустимого діапазону"],
  ["f_nominal_hz", "f, Гц", "Номінальна частота дискретизації"],
  ["deadband", "Зона нечутл.", "Зона нечутливості"],
  ["hampel_window", "w Гампеля", "Ширина вікна фільтра Гампеля"],
  ["hampel_kappa", "κ Гампеля", "Поріг заміни, σ"],
  ["hampel_min_sigma", "σ min", "Нижня межа масштабу для σ=0"],
  ["feature_window", "Вікно ознак", "Кількість відліків для ознак"],
  ["residual_k", "k залишку", "ε = k·σr"],
];

function Editor({ channel, onDone }: { channel: Channel; onDone: () => void }) {
  const [c, setC] = useState<Channel>(channel);
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    const { id: _id, auto_registered: _a, ...patch } = c;
    try {
      await api.updateChannel(channel.id, patch);
      onDone();
    } catch (e) {
      setErr(errorText(e));
    }
  };
  const text = (k: "name" | "unit" | "description" | "source" | "source_field" | "group_name", label: string) => (
    <label>
      {label}
      <input className="input" value={c[k] ?? ""} onChange={(e) => setC({ ...c, [k]: e.target.value || null })} />
    </label>
  );
  return (
    <div className="form">
      <b className="wide">Канал {channel.id}</b>
      {text("name", "Назва")}
      {text("unit", "Одиниця")}
      {text("group_name", "Група")}
      {text("source", "Джерело (тема ROS 2)")}
      {text("source_field", "Поле")}
      {text("description", "Опис")}
      {NUM_FIELDS.map(([k, label, hint]) => (
        <label key={k} title={hint}>
          {label}
          <input
            className="input"
            type="number"
            value={(c[k] as number | null) ?? ""}
            onChange={(e) => setC({ ...c, [k]: e.target.value === "" ? null : Number(e.target.value) })}
          />
        </label>
      ))}
      <label className="toggle">
        <input type="checkbox" checked={c.analytics_enabled !== false} onChange={(e) => setC({ ...c, analytics_enabled: e.target.checked })} />
        діагностика
      </label>
      <div className="row wide">
        <button className="btn primary" onClick={save}>
          Зберегти
        </button>
        <button className="btn" onClick={onDone}>
          Скасувати
        </button>
        {err && <span className="err">{err}</span>}
      </div>
    </div>
  );
}

export function ChannelsTab({ ws }: { ws: Workspace }) {
  const { channels, reloadChannels, states } = useAppData();
  const stats = usePolling(() => api.channelsStats(), 30_000);
  const [editing, setEditing] = useState<Channel | null>(null);
  const [filter, setFilter] = useState("");
  const [newId, setNewId] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const statOf = new Map((stats.data ?? []).map((s) => [s.id, s]));

  const create = async () => {
    try {
      await api.createChannel({ id: newId.trim() });
      setNewId("");
      reloadChannels();
    } catch (e) {
      setErr(errorText(e));
    }
  };

  if (editing)
    return (
      <div className="pane grow">
        <Editor
          channel={editing}
          onDone={() => {
            setEditing(null);
            reloadChannels();
          }}
        />
      </div>
    );

  const list = channels.filter((c) => !filter || c.id.toLowerCase().includes(filter.toLowerCase()) || (c.name ?? "").toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="pane grow">
      <div className="toolbar">
        <input className="input" placeholder="Пошук…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="muted">{channels.length} каналів</span>
        <input className="input" placeholder="id нового каналу" value={newId} onChange={(e) => setNewId(e.target.value)} style={{ width: 140 }} />
        <button className="btn" onClick={create} disabled={!newId.trim()}>
          Зареєструвати
        </button>
        {err && <span className="err">{err}</span>}
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>id</th>
            <th>Назва</th>
            <th>Од.</th>
            <th>Діапазон</th>
            <th>Точок в архіві</th>
            <th>Архів</th>
            <th>Останнє</th>
            <th>Діагн.</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.map((c) => {
            const s = statOf.get(c.id);
            const st = states[c.id];
            const onChart = ws.channels.includes(c.id);
            return (
              <tr key={c.id}>
                <td>
                  {c.id}
                  {c.auto_registered && (
                    <span className="muted small" title="Зареєстровано автоматично при першому надходженні даних">
                      {" "}
                      авто
                    </span>
                  )}
                </td>
                <td>{c.name !== c.id ? c.name : ""}</td>
                <td>{c.unit ?? ""}</td>
                <td>{c.x_min !== null || c.x_max !== null ? `${c.x_min ?? "−∞"} … ${c.x_max ?? "∞"}` : ""}</td>
                <td>{s ? s.count.toLocaleString("uk-UA") : ""}</td>
                <td className="small">{s ? `${fmtDateTime(usToS(s.first_ts))} — ${fmtDateTime(usToS(s.last_ts))}` : ""}</td>
                <td>{st?.last_val != null ? fmtNum(st.last_val, 3) : ""}</td>
                <td>{c.analytics_enabled === false ? "вимк." : "так"}</td>
                <td className="row">
                  <button className="btn" disabled={onChart} onClick={() => workspaces.update(ws.id, { channels: [...ws.channels, c.id] })}>
                    На графік
                  </button>
                  <button className="btn" onClick={() => setEditing(c)}>
                    Змінити
                  </button>
                  <button
                    className="btn"
                    onClick={async () => {
                      if (confirm(`Видалити канал ${c.id} з реєстру? Дані в архіві залишаться.`)) {
                        await api.deleteChannel(c.id);
                        reloadChannels();
                      }
                    }}
                  >
                    Видалити
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
