import { useState } from "react";
import { storage } from "../lib/storage";
import type { Workspace } from "../lib/workspaces";
import { AlarmsTab } from "../tabs/AlarmsTab";
import { AnalysisTab } from "../tabs/AnalysisTab";
import { ChannelsTab } from "../tabs/ChannelsTab";
import { EventsTab } from "../tabs/EventsTab";
import { ModelsTab } from "../tabs/ModelsTab";
import { RawTab } from "../tabs/RawTab";
import { StatsTab } from "../tabs/StatsTab";
import { SystemTab } from "../tabs/SystemTab";

type Tab = "stats" | "events" | "alarms" | "raw" | "analysis" | "channels" | "models" | "system";
export type PanelSize = "normal" | "large" | "collapsed";

export function BottomPanel({ ws, size, onSize }: { ws: Workspace; size: PanelSize; onSize: (s: PanelSize) => void }) {
  const [tab, setTab] = useState<Tab>(() => (storage.get("tsa.tab") as Tab | null) ?? "stats");

  const select = (t: Tab) => {
    setTab(t);
    storage.set("tsa.tab", t);
    if (size === "collapsed") onSize("normal");
  };

  const tabs: Array<[Tab, string]> = [
    ["stats", `Статистика датасетів (${ws.channels.length})`],
    ["events", "Події"],
    ["alarms", "Тривоги"],
    ["raw", "Первинні дані"],
    ["analysis", "Аналіз"],
    ["channels", "Канали"],
    ["models", "Моделі"],
    ["system", "Система"],
  ];

  return (
    <section className={`bottom ${size}`}>
      <div className="bottom-head">
        <nav className="tabs">
          {tabs.map(([k, label]) => (
            <button key={k} className={`tabbtn ${tab === k ? "active" : ""}`} onClick={() => select(k)}>
              {label}
            </button>
          ))}
        </nav>
        <div className="chevrons">
          <button className="btn round" title={size === "large" ? "Зменшити панель" : "Згорнути / розгорнути"} onClick={() => onSize(size === "collapsed" ? "normal" : size === "large" ? "normal" : "collapsed")}>
            {size === "collapsed" ? "▴" : "▾"}
          </button>
          <button className="btn round" title="Розгорнути на більшу висоту" onClick={() => onSize(size === "large" ? "normal" : "large")}>
            {size === "large" ? "▾▾" : "▴▴"}
          </button>
        </div>
      </div>
      {size !== "collapsed" && (
        <div className="bottom-body">
          {tab === "stats" && <StatsTab ws={ws} />}
          {tab === "events" && <EventsTab ws={ws} />}
          {tab === "alarms" && <AlarmsTab ws={ws} />}
          {tab === "raw" && <RawTab ws={ws} />}
          {tab === "analysis" && <AnalysisTab ws={ws} />}
          {tab === "channels" && <ChannelsTab ws={ws} />}
          {tab === "models" && <ModelsTab ws={ws} />}
          {tab === "system" && <SystemTab />}
        </div>
      )}
    </section>
  );
}
