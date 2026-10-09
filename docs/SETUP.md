# Запуск проєкту на іншому комп'ютері

Інструкція для Linux. На Windows використовуйте WSL 2: `run_dev.sh` — це bash-скрипт.

## 1. Що встановити

| Програма | Версія | Перевірка |
|---|---|---|
| Git | будь-яка | `git --version` |
| Docker + Docker Compose | Compose v2 | `docker compose version` |
| Python | 3.11 або новіший | `python3 --version` |
| Node.js + npm | 20.19+ або 22.12+ | `node --version` |

Користувач має бути в групі `docker` (`sudo usermod -aG docker $USER`, потім перелогінитися).

## 2. Код

```bash
git clone https://github.com/Terlok/ts-analysis-subsystem.git
cd ts-analysis-subsystem
git checkout feature/live-history     # або main, якщо всі pull request уже злиті
```

Гілки злиті не всі: остання версія коду — у `feature/live-history`.

## 3. Файли, яких немає в git

Їх треба перенести вручну (флешка, `scp`, хмара):

| Що | Куди | Навіщо |
|---|---|---|
| файли з сигналами (`*.csv`) | `backend/data/` | джерело даних для відтворювача |
| `task_conditions/` | корінь проєкту | дисертація, стаття, скріншот |
| `backend/models/` (необов'язково) | `backend/models/` | файли навчених моделей; мають сенс лише разом із базою (див. п. 8) |

## 4. Бази даних (Docker)

```bash
docker compose up -d          # QuestDB :9000/:8812, PostgreSQL :5433, Redis :6379
docker compose ps             # три контейнери у стані running
```

Образи (~900 МБ) завантажуються один раз.

- **Корпоративний проксі** (як на робочому ПК): демону Docker потрібен проксі, див. п. 9.
- **Помилка `failed to add the host <=> sandbox pair interfaces: operation not supported`**: оновилося ядро, а система не перезавантажена. Перезавантажте комп'ютер.

## 5. Бекенд

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[scripts,ml,dev]"
cp .env.example .env                       # підключення до баз; за замовчуванням підходить для docker compose
.venv/bin/python -m scripts.init_db        # створює таблиці (повторний запуск безпечний)
.venv/bin/python -m pytest                 # необов'язково: 47 тестів, бази не потрібні
```

## 6. Фронтенд

```bash
cd frontend
npm install
```

## 7. Щоденний запуск (три термінали)

```bash
# 1 — бекенд: API + воркери (Ctrl+C зупиняє все)
cd backend && ./run_dev.sh

# 2 — дані: 14 днів одразу в архів, решта наживо (~7,8 доби)
cd backend && .venv/bin/python -m scripts.replay data/*.csv --history 14d

# 3 — інтерфейс
cd frontend && npm run dev
```

Відкрити в браузері:

- http://127.0.0.1:5173 — інтерфейс;
- http://127.0.0.1:8000/docs — документація API;
- http://127.0.0.1:9000 — консоль QuestDB.

Відтворювач можна зупиняти й запускати знову. Повторно надіслані точки не дублюються: кожна точка з тими самими каналом і часом записується один раз. Але `--history` щоразу зсуває час на нове «зараз». Тому, щоб почати з чистого аркуша, перед повторним запуском очистіть бази:

```bash
cd backend && .venv/bin/python -m scripts.reset_data --yes   # дані видаляються, схема лишається
# потім перезапустити ./run_dev.sh
```

## 8. Перенесення накопичених даних (необов'язково)

Вміст баз лежить у томах Docker і з git не переноситься. Є два варіанти.

**А. Простіше:** на новому ПК знову запустити відтворювач (п. 7). Моделі перенавчити на вкладці «Моделі».

**Б. Перенести томи.** На старому ПК:

```bash
docker compose stop
for v in questdb-data postgres-data redis-data; do
  docker run --rm -v ts-analysis-subsystem_$v:/v -v "$PWD":/b alpine tar czf /b/$v.tgz -C /v .
done
docker compose start
```

На новому ПК, з тієї самої папки проєкту, після `docker compose up -d && docker compose stop`:

```bash
for v in questdb-data postgres-data redis-data; do
  docker run --rm -v ts-analysis-subsystem_$v:/v -v "$PWD":/b alpine sh -c "rm -rf /v/* && tar xzf /b/$v.tgz -C /v"
done
docker compose start
```

Разом із томами скопіюйте `backend/models/`: реєстр моделей у PostgreSQL посилається на ці файли. Ім'я тому починається з назви папки проєкту (`ts-analysis-subsystem_…`). Якщо на новому ПК папка називається інакше, перевірте імена командою `docker volume ls`.

## 9. Лише за корпоративним проксі

**Docker** (одноразово, з вашими логіном і паролем):

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://LOGIN:PASSWORD@..."
Environment="HTTPS_PROXY=http://LOGIN:PASSWORD@..."
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
sudo systemctl daemon-reload && sudo systemctl restart docker
```

**Локальні адреси повз проксі.** Додайте в `~/.zshrc` або `~/.bashrc`:

```bash
export NO_PROXY=localhost,127.0.0.1,::1
export no_proxy=$NO_PROXY
```

Бекенд сам обходить проксі для своїх баз, а це налаштування потрібне для решти програм.

**Git.** SSH (порт 22) за таким проксі не працює. Використовуйте HTTPS і персональний токен GitHub.

Вдома без проксі цей пункт не потрібен.

## 10. Продовжити роботу з Claude Code (необов'язково)

Скопіюйте файл сесії `~/.claude/projects/-home-alex-Projects-ts-analysis-subsystem/3b76cb2a-8a4a-4a4f-9450-489ac62d38e1.jsonl` у `~/.claude/projects/<шлях-до-проєкту, де "/" замінено на "-">/` на новому ПК. Потім у папці проєкту виконайте `claude --resume`. Стан роботи описано в `docs/PROGRESS.md`.
