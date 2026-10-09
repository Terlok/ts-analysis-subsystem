import { useEffect, useState } from "react";
import { engine } from "../lib/engine";
import { fmtDate, fmtTime } from "../lib/format";
import { useAppData, useEngine, useWorkspaces } from "../lib/hooks";
import { WINDOW_PRESETS_H, workspaces } from "../lib/workspaces";

function Clock() {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="clock" title="Локальний час">
      {fmtDate(now)} {fmtTime(now)}
    </span>
  );
}

export function TopBar() {
  const ws = useWorkspaces();
  const snap = useEngine();
  const { health } = useAppData();
  const active = workspaces.active();
  const n = active.channels.length;

  let status: { cls: string; text: string; title?: string };
  if (!n) status = { cls: "badge", text: "Немає датасетів" };
  else if (snap.error) status = { cls: "badge bad", text: "Помилка завантаження", title: snap.error };
  else if (snap.loading) status = { cls: "badge warn", text: "Завантаження…" };
  else status = { cls: "badge ok", text: `Дані завантажено (${n} ${n === 1 ? "датасет" : "датасетів"})` };

  const wsBadge = !n
    ? { cls: "badge", text: "WS: —" }
    : snap.mode === "archive"
      ? { cls: "badge", text: "WS: пауза" }
      : snap.wsStatus === "online"
        ? { cls: "badge ok", text: "WS: онлайн" }
        : snap.wsStatus === "connecting"
          ? { cls: "badge warn", text: "WS: з'єднання" }
          : { cls: "badge bad", text: "WS: офлайн" };

  const setWindow = (h: number) => {
    const s = h * 3600;
    if (snap.mode === "live") {
      workspaces.update(active.id, { windowS: s });
    } else {
      const end = snap.range[1];
      engine.zoomTo(end - s, end);
      workspaces.update(active.id, { windowS: s });
    }
  };

  const toggleArchive = () => {
    if (snap.mode === "live") engine.zoomTo(snap.range[0], snap.range[1]);
    else engine.goLive();
  };

  return (
    <header className="topbar">
      <div className="group">
        {ws.items.map((w) => (
          <span key={w.id} className={`tab ${w.id === ws.activeId ? "active" : ""}`}>
            <button
              className="btn"
              onClick={() => workspaces.activate(w.id)}
              onDoubleClick={() => {
                const name = prompt("Назва вікна", w.name);
                if (name) workspaces.update(w.id, { name });
              }}
              title="Подвійний клік — перейменувати"
            >
              {w.name}
            </button>
            {ws.items.length > 1 && w.id === ws.activeId && (
              <button className="btn x" title="Закрити вікно" onClick={() => workspaces.remove(w.id)}>
                ×
              </button>
            )}
          </span>
        ))}
        <button className="btn" title="Нове вікно" onClick={() => workspaces.add()}>
          +
        </button>
        <span className={status.cls} title={status.title}>
          {status.text}
        </span>
      </div>
      <div className="sep" />
      <div className="group">
        <span className="muted">Вікно:</span>
        <span className="segmented">
          {WINDOW_PRESETS_H.map((h) => (
            <button
              key={h}
              className={`seg ${Math.abs(snap.windowS - h * 3600) < 1 ? "active" : ""}`}
              onClick={() => setWindow(h)}
            >
              {h}г
            </button>
          ))}
        </span>
        <span className={wsBadge.cls}>{wsBadge.text}</span>
        <button className={`btn ${snap.mode === "archive" ? "pressed" : ""}`} onClick={toggleArchive} title="Ретроспективний перегляд архіву">
          Архів
        </button>
        {snap.mode === "archive" && (
          <button className="btn" onClick={() => engine.goLive()} title="Повернутися до живого потоку">
            ▶ Наживо
          </button>
        )}
        {snap.mode === "live" && snap.liveLatencyMs !== null && (
          <span className="muted" title="Затримка від приймання на сервері до отримання в браузері">
            L≈{snap.liveLatencyMs.toFixed(0)} мс
          </span>
        )}
      </div>
      <div className="spacer" />
      <div className="group">
        {(["questdb", "postgres", "redis"] as const).map((k) => (
          <span key={k} className="health" title={`${k}: ${health ? (health[k] ? "доступна" : "недоступна") : "невідомо"}`}>
            <i className={`dot ${health ? (health[k] ? "ok" : "bad") : ""}`} />
            {k === "questdb" ? "QuestDB" : k === "postgres" ? "PostgreSQL" : "Redis"}
          </span>
        ))}
        <a className="btn" href="http://127.0.0.1:8000/docs" target="_blank" rel="noreferrer" title="Документація REST API (OpenAPI)">
          API
        </a>
        <Clock />
      </div>
    </header>
  );
}
