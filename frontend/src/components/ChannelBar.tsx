import { useEffect, useState } from "react";
import { api, errorText } from "../api/client";
import { engine } from "../lib/engine";
import { fmtDateTime, fromLocalInput, sToUs, toLocalInput } from "../lib/format";
import { useAppData, useEngine } from "../lib/hooks";
import { workspaces, type Workspace } from "../lib/workspaces";
import { ChannelSearch } from "./ChannelSearch";

export interface Layers {
  events: boolean;
  flags: boolean;
  alarms: boolean;
}

interface Props {
  ws: Workspace;
  layers: Layers;
  onLayers: (l: Layers) => void;
}

export function ChannelBar({ ws, layers, onLayers }: Props) {
  const { channels, states, colorOf } = useAppData();
  const snap = useEngine();
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  useEffect(() => {
    setFrom(toLocalInput(snap.range[0]));
    setTo(toLocalInput(snap.range[1]));
  }, [snap.range[0], snap.range[1]]); // eslint-disable-line react-hooks/exhaustive-deps

  const applyRange = () => {
    const a = fromLocalInput(from);
    const b = fromLocalInput(to);
    if (a !== null && b !== null && b > a) engine.zoomTo(a, b);
  };

  const mark = async () => {
    const t = engine.cursorT ?? snap.range[1];
    const label = prompt(`Мітка на ${fmtDateTime(t)}\nТекст мітки (режим, подія, коментар):`, "мітка");
    if (!label) return;
    try {
      await api.addMode({ mode: label, ts: sToUs(t), details: { channels: ws.channels, source: "ui" } });
      await engine.refreshModes();
    } catch (e) {
      alert(`Не вдалося зберегти мітку: ${errorText(e)}`);
    }
  };

  return (
    <div className="channelbar">
      <div className="group">
        <ChannelSearch
          channels={channels}
          states={states}
          exclude={ws.channels}
          onPick={(id) => workspaces.update(ws.id, { channels: [...ws.channels, id] })}
        />
        {ws.channels.map((c) => (
          <span key={c} className="chip" style={{ borderColor: colorOf(c) }}>
            <i className="swatch" style={{ background: colorOf(c) }} />
            {c}
            <button className="x" title="Прибрати з графіка" onClick={() => workspaces.update(ws.id, { channels: ws.channels.filter((x) => x !== c) })}>
              ×
            </button>
          </span>
        ))}
      </div>
      {snap.mode === "archive" && (
        <div className="group">
          <span className="muted">з</span>
          <input className="input" type="datetime-local" step={1} value={from} onChange={(e) => setFrom(e.target.value)} />
          <span className="muted">по</span>
          <input className="input" type="datetime-local" step={1} value={to} onChange={(e) => setTo(e.target.value)} />
          <button className="btn" onClick={applyRange}>
            Показати
          </button>
          <button className="btn" title="Назад" onClick={() => engine.panBy(-0.5)}>
            ◀
          </button>
          <button className="btn" title="Вперед" onClick={() => engine.panBy(0.5)}>
            ▶
          </button>
        </div>
      )}
      <div className="spacer" />
      <div className="group">
        <label className="toggle" title="Епізоди аномалій і викидів">
          <input type="checkbox" checked={layers.events} onChange={(e) => onLayers({ ...layers, events: e.target.checked })} />
          події
        </label>
        <label className="toggle" title="Аномальні та замінені точки (повна роздільність)">
          <input type="checkbox" checked={layers.flags} onChange={(e) => onLayers({ ...layers, flags: e.target.checked })} />
          позначки
        </label>
        <label className="toggle" title="Тривоги">
          <input type="checkbox" checked={layers.alarms} onChange={(e) => onLayers({ ...layers, alarms: e.target.checked })} />
          тривоги
        </label>
        <span className="segmented">
          <button className={`seg ${ws.layout === "single" ? "active" : ""}`} onClick={() => workspaces.update(ws.id, { layout: "single" })} title="Усі датасети на одному графіку">
            Один
          </button>
          <button className={`seg ${ws.layout === "stacked" ? "active" : ""}`} onClick={() => workspaces.update(ws.id, { layout: "stacked" })} title="Окремий графік на кожен датасет, синхронний курсор">
            Окремі
          </button>
        </span>
        <button className="btn" onClick={mark} disabled={!ws.channels.length} title="Поставити мітку в позиції курсора (або в кінці вікна)">
          ⚑ Мітка
        </button>
        <button className="btn" onClick={() => engine.resetZoom()} title="Скинути масштаб (також подвійний клік по графіку)">
          Скинути
        </button>
      </div>
    </div>
  );
}
