// Alarms (ISA-18.2): active list with acknowledgement, journal and rule editor.
import { useState } from "react";
import { api, errorText } from "../api/client";
import type { AlarmKind, AlarmRule, AlarmRuleIn } from "../api/types";
import { engine } from "../lib/engine";
import { ALARM_KIND_UA, ALARM_STATE_UA, fmtDateTime, fmtNum, usToS } from "../lib/format";
import { useAppData, usePolling } from "../lib/hooks";
import { storage } from "../lib/storage";
import type { Workspace } from "../lib/workspaces";

const EMPTY_RULE = (channel: string): AlarmRuleIn => ({
  channel_id: channel,
  kind: "hi",
  limit: 0,
  deadband: 0,
  on_delay_ms: 0,
  off_delay_ms: 0,
  min_repeat_ms: 0,
  priority: 2,
  enabled: true,
  description: null,
});

function RuleForm({ initial, channels, onSaved, onCancel }: { initial: AlarmRule | AlarmRuleIn; channels: string[]; onSaved: () => void; onCancel: () => void }) {
  const [r, setR] = useState<AlarmRuleIn>({ ...initial });
  const [err, setErr] = useState<string | null>(null);
  const id = "id" in initial ? initial.id : null;
  const num = (k: keyof AlarmRuleIn) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setR({ ...r, [k]: e.target.value === "" ? null : Number(e.target.value) });
  const save = async () => {
    try {
      if (id !== null) await api.updateRule(id, r);
      else await api.createRule(r);
      onSaved();
    } catch (e) {
      setErr(errorText(e));
    }
  };
  return (
    <div className="form">
      <label>
        Канал
        <select className="input" value={r.channel_id} onChange={(e) => setR({ ...r, channel_id: e.target.value })}>
          {channels.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
      </label>
      <label>
        Тип
        <select className="input" value={r.kind} onChange={(e) => setR({ ...r, kind: e.target.value as AlarmKind })}>
          {(["hihi", "hi", "lo", "lolo", "anomaly"] as const).map((k) => (
            <option key={k} value={k}>
              {ALARM_KIND_UA[k]}
            </option>
          ))}
        </select>
      </label>
      {r.kind !== "anomaly" && (
        <label>
          Межа
          <input className="input" type="number" value={r.limit ?? ""} onChange={num("limit")} />
        </label>
      )}
      <label title="Гістерезис: на скільки значення має повернутися за межу, щоб тривога зникла">
        Зона нечутл.
        <input className="input" type="number" value={r.deadband} onChange={num("deadband")} />
      </label>
      <label title="Затримка спрацювання, мс">
        Затримка вкл.
        <input className="input" type="number" value={r.on_delay_ms} onChange={num("on_delay_ms")} />
      </label>
      <label title="Затримка зникнення, мс">
        Затримка викл.
        <input className="input" type="number" value={r.off_delay_ms} onChange={num("off_delay_ms")} />
      </label>
      <label title="Мінімальний інтервал між повторними спрацюваннями, мс">
        Придушення
        <input className="input" type="number" value={r.min_repeat_ms} onChange={num("min_repeat_ms")} />
      </label>
      <label>
        Пріоритет
        <select className="input" value={r.priority} onChange={(e) => setR({ ...r, priority: Number(e.target.value) })}>
          {[1, 2, 3, 4].map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </label>
      <label className="toggle">
        <input type="checkbox" checked={r.enabled} onChange={(e) => setR({ ...r, enabled: e.target.checked })} />
        увімкнено
      </label>
      <label className="wide">
        Опис
        <input className="input" value={r.description ?? ""} onChange={(e) => setR({ ...r, description: e.target.value || null })} />
      </label>
      <div className="row">
        <button className="btn primary" onClick={save}>
          Зберегти
        </button>
        <button className="btn" onClick={onCancel}>
          Скасувати
        </button>
        {err && <span className="err">{err}</span>}
      </div>
    </div>
  );
}

export function AlarmsTab({ ws }: { ws: Workspace }) {
  const { channels } = useAppData();
  const [view, setView] = useState<"active" | "log" | "rules">("active");
  const [user, setUser] = useState(() => storage.get("tsa.user") ?? "оператор");
  const [editing, setEditing] = useState<AlarmRule | AlarmRuleIn | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const scope = ws.channels.length ? ws.channels : undefined;

  const active = usePolling(() => api.alarms({ active: true, channels: scope }), 3000, [ws.channels.join()], view === "active");
  const log = usePolling(() => api.alarmLog({ channels: scope, limit: 300 }), 5000, [ws.channels.join()], view === "log");
  const rules = usePolling(() => api.alarmRules(scope), 0, [ws.channels.join()], view === "rules");

  const ack = async (id: number) => {
    storage.set("tsa.user", user);
    try {
      await api.ackAlarm(id, user);
      setMsg(`Квитування тривоги #${id} надіслано`);
      setTimeout(active.reload, 600);
    } catch (e) {
      setMsg(errorText(e));
    }
  };

  const ruleChannels = ws.channels.length ? ws.channels : channels.map((c) => c.id);

  return (
    <div className="pane grow">
      <div className="toolbar">
        <span className="segmented">
          <button className={`seg ${view === "active" ? "active" : ""}`} onClick={() => setView("active")}>
            Активні {active.data ? `(${active.data.length})` : ""}
          </button>
          <button className={`seg ${view === "log" ? "active" : ""}`} onClick={() => setView("log")}>
            Журнал
          </button>
          <button className={`seg ${view === "rules" ? "active" : ""}`} onClick={() => setView("rules")}>
            Правила
          </button>
        </span>
        {view === "active" && (
          <>
            <span className="muted">Оператор:</span>
            <input className="input" value={user} onChange={(e) => setUser(e.target.value)} style={{ width: 120 }} />
          </>
        )}
        {view === "rules" && !editing && ruleChannels.length > 0 && (
          <button className="btn" onClick={() => setEditing(EMPTY_RULE(ruleChannels[0]))}>
            + Нове правило
          </button>
        )}
        {msg && <span className="muted">{msg}</span>}
        {(active.error || log.error || rules.error) && <span className="err">{active.error || log.error || rules.error}</span>}
      </div>

      {view === "active" && (
        <table className="table">
          <thead>
            <tr>
              <th>#</th>
              <th>Канал</th>
              <th>Пріоритет</th>
              <th>Стан</th>
              <th>Спрацювала</th>
              <th>Значення</th>
              <th>Зникла</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(active.data ?? []).map((a) => (
              <tr key={a.id} className={`alarm p${a.priority} ${a.state}`}>
                <td>{a.id}</td>
                <td className="clickable" onClick={() => engine.zoomTo(usToS(a.ts_active) - 300, usToS(a.ts_return ?? a.ts_active) + 300)}>
                  {a.channel_id}
                </td>
                <td>{a.priority}</td>
                <td>{ALARM_STATE_UA[a.state] ?? a.state}</td>
                <td>{fmtDateTime(usToS(a.ts_active))}</td>
                <td>{fmtNum(a.value, 3)}</td>
                <td>{a.ts_return ? fmtDateTime(usToS(a.ts_return)) : "—"}</td>
                <td>
                  {a.state.startsWith("unack") && (
                    <button className="btn" onClick={() => void ack(a.id)}>
                      Квитувати
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {active.data?.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  Активних тривог немає
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {view === "log" && (
        <table className="table">
          <thead>
            <tr>
              <th>Час</th>
              <th>Тривога</th>
              <th>Канал</th>
              <th>Перехід</th>
              <th>Причина</th>
              <th>Значення</th>
              <th>Користувач</th>
            </tr>
          </thead>
          <tbody>
            {(log.data ?? []).map((l) => (
              <tr key={l.id}>
                <td>{fmtDateTime(usToS(l.ts), true)}</td>
                <td>#{l.alarm_id}</td>
                <td>{l.channel_id}</td>
                <td>
                  {ALARM_STATE_UA[l.prev_state] ?? l.prev_state} → {ALARM_STATE_UA[l.new_state] ?? l.new_state}
                </td>
                <td>{l.reason}</td>
                <td>{fmtNum(l.value, 3)}</td>
                <td>{l.user ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {view === "rules" &&
        (editing ? (
          <RuleForm
            initial={editing}
            channels={ruleChannels}
            onCancel={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              rules.reload();
            }}
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Канал</th>
                <th>Тип</th>
                <th>Межа</th>
                <th>Зона</th>
                <th>Затримки, мс</th>
                <th>Пріоритет</th>
                <th>Стан</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(rules.data ?? []).map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td>{r.channel_id}</td>
                  <td>{ALARM_KIND_UA[r.kind]}</td>
                  <td>{r.limit ?? "—"}</td>
                  <td>{r.deadband}</td>
                  <td>
                    {r.on_delay_ms} / {r.off_delay_ms} / {r.min_repeat_ms}
                  </td>
                  <td>{r.priority}</td>
                  <td>{r.enabled ? "увімк." : "вимк."}</td>
                  <td className="row">
                    <button className="btn" onClick={() => setEditing(r)}>
                      Змінити
                    </button>
                    <button
                      className="btn"
                      onClick={async () => {
                        if (confirm(`Видалити правило #${r.id}?`)) {
                          await api.deleteRule(r.id);
                          rules.reload();
                        }
                      }}
                    >
                      Видалити
                    </button>
                  </td>
                </tr>
              ))}
              {rules.data?.length === 0 && (
                <tr>
                  <td colSpan={9} className="muted">
                    Правил немає. Тривоги за межами (H/HH/L/LL) і за аномаліями задаються тут.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        ))}
    </div>
  );
}
