import { api, errorText } from "../api/client";
import { useState } from "react";
import { usePolling } from "../lib/hooks";
import type { Workspace } from "../lib/workspaces";

export function ModelsTab({ ws }: { ws: Workspace }) {
  const [onlyChart, setOnlyChart] = useState(true);
  const { data, error, reload } = usePolling(() => api.models(), 0);
  const [err, setErr] = useState<string | null>(null);
  const list = (data ?? []).filter((m) => !onlyChart || ws.channels.includes(m.channel_id));

  return (
    <div className="pane grow">
      <div className="toolbar">
        <label className="toggle">
          <input type="checkbox" checked={onlyChart} onChange={(e) => setOnlyChart(e.target.checked)} />
          лише канали графіка
        </label>
        <span className="muted">
          Моделі навчаються скриптом <code>scripts/train_forecaster.py --register</code>; без моделі використовується наївний прогноз.
        </span>
        {(error || err) && <span className="err">{error || err}</span>}
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>#</th>
            <th>Канал</th>
            <th>Тип</th>
            <th>Версія</th>
            <th>MAE тест / наївний</th>
            <th>σr</th>
            <th>Інференс, мкс/точку</th>
            <th>Стан</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.map((m) => {
            const mt = (m.metrics ?? {}) as Record<string, number>;
            return (
              <tr key={m.id}>
                <td>{m.id}</td>
                <td>{m.channel_id}</td>
                <td>{m.kind === "forecaster" ? "прогноз" : "класифікатор"}</td>
                <td>{m.version}</td>
                <td>{mt.mae_test != null ? `${mt.mae_test.toPrecision(3)} / ${mt.mae_test_naive?.toPrecision(3)}` : "—"}</td>
                <td>{mt.sigma_r != null ? mt.sigma_r.toPrecision(3) : "—"}</td>
                <td>{mt.batch_inference_us_per_point ?? "—"}</td>
                <td>{m.active ? <span className="pill ok">активна</span> : ""}</td>
                <td>
                  {!m.active && (
                    <button
                      className="btn"
                      onClick={async () => {
                        try {
                          await api.activateModel(m.id);
                          reload();
                        } catch (e) {
                          setErr(errorText(e));
                        }
                      }}
                    >
                      Активувати
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
          {list.length === 0 && (
            <tr>
              <td colSpan={9} className="muted">
                Зареєстрованих моделей немає.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
