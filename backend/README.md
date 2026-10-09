# Backend підсистеми візуалізації і аналізу часових рядів

FastAPI-сервер, потокові воркери та аналітичне ядро. Відповідає архітектурі з розділу 1.5.1
дисертації та рис. 1 статті.

```
replay.py ──ws/ingest──► API (FastAPI) ──XADD──► Redis Stream telem:in
                         │  (перевірка, якість, t_ing,    │ consumer groups
                         │   гаряче вікно, last, wm)      ├─► archiver   → QuestDB telemetry
                         │                                ├─► analytics  → QuestDB point_flags,
                         │                                │                PostgreSQL events/alarms,
                         │                                │                Redis Pub/Sub
                         │                                └─► aggregator → QuestDB telemetry_agg
                         ├─ /api/series: рівень ℓ*, тайли (Redis) → MinMax → LTTB + шар подій
                         └─ /ws/stream: LTTB з прив'язкою до абсолютної сітки, події, тривоги
```

| Каталог | Зміст |
|---|---|
| `tsa_core/` | Аналітичне ядро без залежностей від БД і вебу: Гампель, віконні ознаки, діагностика, тривоги ISA-18.2, агрегати, LTTB/MinMax, вибір рівня |
| `app/` | FastAPI: конфігурація, доступ до БД, сервіси, REST/WebSocket |
| `workers/` | Окремі процеси: `archiver`, `analytics`, `aggregator` |
| `scripts/` | Відтворювач телеметрії, експорт з QuestDB, ініціалізація БД, навчання моделей, повторний аналіз, генератор тестових даних |
| `tests/` | Тести ядра, сервісів, API і відтворювача (без реальних БД) |

## Встановлення

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[scripts,ml,dev]"
cp .env.example .env          # параметри підключення
.venv/bin/python -m pytest    # тести не потребують БД
```

## Запуск

1. Підняти сховища. Можна через `docker compose up -d` у корені репозиторію (QuestDB на 9000/8812,
   PostgreSQL на **5433**, Redis на 6379) або використати власні інстанси й прописати їх у `.env`.
2. Створити таблиці (повторний запуск безпечний):
   ```bash
   .venv/bin/python -m scripts.init_db
   ```
3. Запустити API і воркери:
   ```bash
   ./run_dev.sh                 # або SHARDS=2 ./run_dev.sh
   ```
   OpenAPI-документація: http://localhost:8000/docs
4. Подати дані (див. нижче).

## Дані: експорт і відтворення

Таблиця-джерело на віддаленому сервері:

```sql
CREATE TABLE 'analog' (id SYMBOL, ts TIMESTAMP, val FLOAT, nd BOOLEAN, otkl SHORT)
  timestamp(ts) PARTITION BY HOUR TTL 8 WEEKS DEDUP UPSERT KEYS(id, ts);
```

**Експорт у файл** (через HTTP `/exp`, по годинних зрізах):

```bash
python -m scripts.export_questdb --url http://REMOTE:9000 \
    --from 2025-05-01T00:00:00Z --to 2025-05-02T00:00:00Z --out data/analog.csv.gz
python -m scripts.export_questdb --url http://REMOTE:9000 --last 6h --ids P1,P2 --out data/p.csv
```

Підходить і власний CSV або Parquet з колонками `id, ts, val, nd, otkl`. Час може бути в ISO 8601 або
числом (с / мс / мкс / нс, одиниця визначається автоматично). Назви колонок задаються через `--col-*`.

**Імітація надсилання сигналів:**

```bash
python -m scripts.replay data/analog.csv.gz                    # реальний час, мітки зсунуті на «зараз»
python -m scripts.replay data/analog.csv.gz --speed 10         # у 10 разів швидше
python -m scripts.replay data/analog.csv.gz --speed 10 --compress-time   # і час даних стиснутий
python -m scripts.replay data/analog.csv.gz --speed 0 --time-mode original  # швидкий імпорт історії
python -m scripts.replay data/analog.csv.gz --channels P1,P2 --start 2025-05-01T10:00Z --loop
python -m scripts.replay data/analog.csv.gz --dry-run          # лише статистика, без надсилання
```

Як працює відтворювач:

- файл читається частинами, тому його розмір не обмежений пам'яттю;
- рядки нарізаються в пакети по `--batch-ms` часу даних (за замовчуванням 100 мс, це L_batch) або
  `--max-points` вимірювань; пакет колонковий і містить усі канали;
- пакет надсилається в момент `start + (t_end − t_first) / speed`;
- режим часу `rebase` зсуває мітки так, що перший відлік припадає на «зараз», і потік виглядає
  живим: працюють гаряче вікно й живі графіки; режим `original` надсилає мітки як є (імпорт історії);
- доставка йде через WebSocket з підтвердженням кожного пакета; непідтверджені пакети повторно
  надсилаються після перепідключення, дублікатів не виникає завдяки `DEDUP` за `(channel, ts)`;
  для HTTP є `--http http://localhost:8000/api/ingest`;
- кожні 5 с друкується статистика: пакети, точки, швидкість, відставання від розкладу, відхилені
  точки, нові канали;
- проксі зі змінних оточення за замовчуванням ігнорується (`--use-proxy`, щоб увімкнути).

Відображення полів `analog` на коди якості: `nd = true` або порожнє `val` дає `bad`,
`otkl ≠ 0` дає `uncertain`, вихід за діапазон `[x_min, x_max]` з реєстру каналів теж дає
`uncertain`, інакше `good`. Фільтр Гампеля позначає замінені точки як `substituted`.
Значення `nd` і `otkl` зберігаються в архіві без змін.

Тестові дані з впровадженими аномаліями й розміткою:

```bash
python -m scripts.make_sample data/sample.csv --channels 8 --hours 2
```

## Сховища

| Сховище | Що зберігає |
|---|---|
| QuestDB `telemetry` | первинні вимірювання: `channel, ts, val, quality, nd, otkl, t_ing`, `DEDUP (ts, channel)` |
| QuestDB `point_flags` | розріджено: замінені Гампелем і аномальні точки (`val_f`, прогноз, залишок, поріг, ймовірність), `run` = `online` або id повторного аналізу |
| QuestDB `telemetry_agg` | агрегати рівнів ℓ: min/max з часом, first/last, Σ, count |
| PostgreSQL | реєстр каналів Mᵢ, правила й журнал тривог, епізоди подій з посиланням `(channel, ts)`, версії моделей, режими робота |
| Redis | потік `telem:in`, гаряче вікно `hot:{ch}` (пакетами), `last`, водяні знаки `wm`, тайли `tile:{ch}:{ℓ}:{k}`, Pub/Sub `events`/`alarms`, метрики |

## API (коротко)

| Метод | Шлях | Призначення |
|---|---|---|
| GET | `/api/series?channels=A,B&from=&to=&width=1600&c=2` | проріджені ряди (m = min(N, ⌈c·W⌉)), шар подій, точкові позначки, тривоги |
| GET | `/api/raw?channel=&from=&to=&format=json\|csv` | первинні значення, якість, ознаки заміни, експорт |
| GET | `/api/flags`, `/api/events`, `/api/modes` | шар подій, режими |
| GET/POST/PATCH/DELETE | `/api/channels…` | реєстр каналів; `/api/channels/state` дає останні значення |
| GET/POST/PUT/DELETE | `/api/alarms/rules…` | правила тривог (hi/hihi/lo/lolo/anomaly, гістерезис, затримки) |
| GET | `/api/alarms?active=true`, `/api/alarms/log` | тривоги і журнал |
| POST | `/api/alarms/{id}/ack` | квитування |
| GET/POST | `/api/models`, `/api/models/{id}/activate` | версії моделей |
| POST | `/api/analysis/preview` | прогін діагностики по архіву з іншими параметрами без збереження |
| POST, WS | `/api/ingest`, `/ws/ingest` | приймання пакетів |
| WS | `/ws/stream` | живі графіки: `{"op":"subscribe","channels":[…],"window_s":60,"width_px":1200}` |
| GET | `/api/health`, `/api/metrics`, `/api/config` | стан, метрики К1/К2/К6, параметри сітки для фронтенду |

Час у параметрах задається мікросекундами від епохи або рядком ISO 8601.

## Моделі й повторний аналіз

```bash
# навчання прогнозної моделі каналу (60/20/20 у часі, проміжки w, MAE проти наївного прогнозу)
python -m scripts.train_forecaster --file data/analog.csv --channel P1
python -m scripts.train_forecaster --questdb --channel P1 --from 2025-05-01 --to 2025-05-08 --register

# повторний аналіз з іншими параметрами, оцінка за розміткою (P, R, F1, затримка виявлення)
python -m scripts.reanalyze --file data/sample.csv --labels data/sample.labels.csv
python -m scripts.reanalyze --questdb --channels P1 --hampel-kappa 4 --run-id kappa4 --write

# точна перебудова агрегатів з сирих даних (після імпорту історії)
python -m scripts.rebuild_aggregates --clear-cache
```

Без навченої моделі використовується наївний прогноз xₜ₊₁ = xₜ, а σ_r оцінюється онлайн, робастно.

## Масштабування

- `analytics` і `aggregator` шардуються за каналами: `--shard i --shards N`, де канал потрапляє в
  шард `crc32(id) mod N`. Кожен шард читає потік своєю consumer group.
- `archiver` не має стану, тому можна запускати кілька копій.
- `/api/metrics` показує для кожного воркера λ (точок/с), τ (мкс на точку) і ρ_load = λτ,
  а також відставання consumer groups. Ціль ρ_load ≤ 0,7.

## Відповідність вимогам

| Вимога | Де реалізовано |
|---|---|
| ФВ-1.1 реєстр, динамічна реєстрація | `app/services/registry.py`, `app/db/models.py:Channel` |
| ФВ-1.2–1.5 приймання, t_src/t_ing, без дублікатів, коди якості | `app/services/ingest.py`, `DEDUP` у `app/db/questdb_schema.py` |
| ФВ-2.1 гаряче вікно і останні значення | `app/db/redis_store.py`, `app/services/hot.py` |
| ФВ-2.3 компоновані агрегати Δℓ = Δ₀bˡ | `tsa_core/aggregates.py`, `workers/aggregator.py` |
| ФВ-3.1–3.3 Гампель, ознаки, комбінована діагностика до проріджування | `tsa_core/hampel.py`, `features.py`, `detectors.py`, `pipeline.py`, `workers/analytics.py` |
| ФВ-3.4 життєвий цикл тривог | `tsa_core/alarms.py`, `workers/analytics.py:AlarmManager` |
| ФВ-3.5 повторний аналіз | `scripts/reanalyze.py`, `/api/analysis/preview` |
| ФВ-4.1–4.3 m, ℓ*, MinMax + LTTB, кеш тайлів | `tsa_core/levels.py`, `downsample.py`, `app/services/series.py`, `tiles.py` |
| ФВ-4.4 стабільний потоковий графік | `tsa_core/downsample.py:StreamingGridLTTB`, `app/services/live.py` |
| ФВ-4.5 окремий шар подій | `/api/series` (events, flags, alarms), Pub/Sub у `/ws/stream` |
| НФВ-7 без втрат підтверджених пакетів | ACK після XADD; ACK у consumer groups після запису; повторна обробка pending |

### Відхилення і уточнення відносно тексту

- **Вибір рівня.** Формула (1.8) уточнена:
  ℓ* = max(0, ⌊log_b(2(tₑ − tₛ)/(ρ m Δ₀))⌋). Кожен кошик дає дві точки (min, max), тому так
  LTTB завжди отримує не менше ρm кандидатів.
- **Прогнозна модель.** Ансамбль дерев прогнозує приріст xₜ₊₁ − xₜ, а не рівень, бо дерева не
  екстраполюють за межі діапазону навчальних цілей. σ_r оцінюється як 1,4826·MAD залишків на
  валідаційній частині.
- **Гаряче вікно.** Інтервали, що повністю лежать у гарячому вікні, обслуговуються з сирих точок
  Redis, а не з агрегатів: так свіжі дані не відстають на інтервал скидання агрегатора.
- **Ліміти тривог.** Граничні тривоги перевіряються на відфільтрованих значеннях. Поодинокі
  імпульси обробляє шар викидів і аномалій.
