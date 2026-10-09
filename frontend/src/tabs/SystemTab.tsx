// Subsystem metrics (criteria К1, К2, К6) and client-side timings.
import { api } from "../api/client";
import { fmtBytes, fmtDuration } from "../lib/format";
import { useAppData, useEngine, usePolling } from "../lib/hooks";

export function SystemTab() {
  const { health } = useAppData();
  const snap = useEngine();
  const metrics = usePolling(() => api.metrics(), 3000);
  const config = usePolling(() => api.config(), 0);
  const m = metrics.data;
  const c = config.data;

  return (
    <div className="sys">
      <section>
        <h4>Сховища</h4>
        <dl className="kv">
          {(["questdb", "postgres", "redis"] as const).map((k) => (
            <div key={k} className="kvrow">
              <dt>{k}</dt>
              <dd>
                <i className={`dot ${health ? (health[k] ? "ok" : "bad") : ""}`} /> {health ? (health[k] ? "доступна" : "недоступна") : "…"}
              </dd>
            </div>
          ))}
          <div className="kvrow">
            <dt>Redis, пам'ять</dt>
            <dd>
              {fmtBytes(m?.redis.used_memory_bytes)} / {m?.redis.maxmemory_bytes ? fmtBytes(m.redis.maxmemory_bytes) : "∞"}
            </dd>
          </div>
          <div className="kvrow">
            <dt>Кеш тайлів</dt>
            <dd>
              {m ? `${m.tiles.hit} / ${m.tiles.miss}` : "—"}
              {m?.tiles.hit_ratio != null ? ` (влучання ${(m.tiles.hit_ratio * 100).toFixed(1)}%)` : ""}
            </dd>
          </div>
        </dl>
      </section>
      <section>
        <h4>Затримки (К1)</h4>
        <table className="table">
          <thead>
            <tr>
              <th>Етап</th>
              <th>p50</th>
              <th>p95</th>
              <th>p99</th>
              <th>n</th>
            </tr>
          </thead>
          <tbody>
            {m &&
              Object.entries(m.latency).map(([k, v]) => (
                <tr key={k} title={v.description}>
                  <td>{k}</td>
                  <td>{v.stats ? `${v.stats.p50_ms.toFixed(1)} мс` : "—"}</td>
                  <td>{v.stats ? `${v.stats.p95_ms.toFixed(1)} мс` : "—"}</td>
                  <td>{v.stats ? `${v.stats.p99_ms.toFixed(1)} мс` : "—"}</td>
                  <td>{v.stats?.n ?? 0}</td>
                </tr>
              ))}
            <tr>
              <td title="Від приймання на сервері до отримання в браузері">ingest → браузер</td>
              <td colSpan={4}>{snap.liveLatencyMs !== null ? `${snap.liveLatencyMs.toFixed(1)} мс (останнє)` : "—"}</td>
            </tr>
            <tr>
              <td>/api/series</td>
              <td colSpan={4}>
                {snap.fetchMs !== null ? `${snap.fetchMs.toFixed(0)} мс у браузері` : "—"}
                {snap.serverMs !== null ? `, ${snap.serverMs.toFixed(0)} мс на сервері` : ""}
              </td>
            </tr>
          </tbody>
        </table>
      </section>
      <section>
        <h4>Обробники (К2: ρ = λτ)</h4>
        <table className="table">
          <thead>
            <tr>
              <th>Воркер</th>
              <th>λ, точок/с</th>
              <th>τ, мкс</th>
              <th>ρ</th>
              <th>Усього</th>
            </tr>
          </thead>
          <tbody>
            {m &&
              Object.entries(m.workers).map(([k, w]) => (
                <tr key={k} className={w.rho_load > 0.7 ? "row-bad" : ""}>
                  <td>{k}</td>
                  <td>{w.lambda_pts_s?.toFixed(1)}</td>
                  <td>{w.tau_us?.toFixed(1)}</td>
                  <td>{w.rho_load?.toFixed(3)}</td>
                  <td>{w.points_total?.toLocaleString("uk-UA")}</td>
                </tr>
              ))}
            {m && !Object.keys(m.workers).length && (
              <tr>
                <td colSpan={5} className="muted">
                  Немає активності за останні 5 хв (дані не надходять або воркери зупинені)
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <h4>Черги потоку</h4>
        <table className="table">
          <tbody>
            <tr>
              <td>Довжина потоку</td>
              <td>{m?.stream.length ?? "—"}</td>
            </tr>
            {m?.stream.groups.map((g) => (
              <tr key={g.name}>
                <td>{g.name}</td>
                <td>
                  очікують {g.pending}, відставання {g.lag ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section>
        <h4>Параметри сервера</h4>
        {c && (
          <dl className="kv">
            <dt>c (деталізація)</dt>
            <dd>{c.detail_c}</dd>
            <dt>ρ (MinMax)</dt>
            <dd>{c.preselect_rho}</dd>
            <dt>Гаряче вікно</dt>
            <dd>{fmtDuration(c.hot_window_s)}</dd>
            <dt>Оновлення live</dt>
            <dd>{c.live_rate_hz} Гц</dd>
            <dt>Рівні Δℓ</dt>
            <dd className="small">{c.grid.levels.map((d) => fmtDuration(d / 1e6)).join(", ")}</dd>
            <dt>K (кошиків у тайлі)</dt>
            <dd>{c.grid.tile_buckets}</dd>
          </dl>
        )}
      </section>
    </div>
  );
}
