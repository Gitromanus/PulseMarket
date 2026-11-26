# Модель данных: SQLite + Google Sheets

## 1. Принцип

- SQLite — рабочее состояние (очередь, lease, журнал).
- Google Sheets — интерфейс оператора. Связь по `ID` (UUID), не по номеру строки.
- Один и тот же `ID` живёт в `tasks.sheet_id` и в колонке `ID` листа.

## 2. SQLite

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE tasks (
  id             INTEGER PRIMARY KEY,
  sheet_id       TEXT UNIQUE,             -- UUID, ключ связи с листом
  sheet_row      INTEGER,                 -- кэш номера строки (только для ускорения записи)
  marketplace    TEXT NOT NULL,           -- ozon | wb
  sku            TEXT NOT NULL,           -- артикул (для Ozon — цифры из ссылки)
  url            TEXT,                    -- исходная ссылка на товар, если была
  query          TEXT NOT NULL,           -- ключевой запрос
  geo            TEXT,                    -- URL города Ozon / адрес или координаты WB
  target_runs    INTEGER NOT NULL DEFAULT 1,
  done_runs      INTEGER NOT NULL DEFAULT 0,
  start_position INTEGER,
  position       INTEGER,
  cart_adds      INTEGER NOT NULL DEFAULT 0,
  likes          INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'pending',
                 -- pending|claimed|running|done|error|paused|cancelled
  source         TEXT,                    -- fake | excel:<путь> | google:<id>
  priority       INTEGER NOT NULL DEFAULT 0,
  attempts       INTEGER NOT NULL DEFAULT 0,  -- после 3 сбоев задание уходит в error
  claim_owner    TEXT,                    -- thread_id
  claim_at       TEXT,
  lease_until    TEXT,
  last_error     TEXT,
  operator_note  TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_tasks_queue ON tasks(status, priority DESC, id);
CREATE INDEX idx_tasks_sheet ON tasks(sheet_id);

CREATE TABLE profiles (
  id            INTEGER PRIMARY KEY,
  account_id    TEXT UNIQUE,              -- ID из листа «Аккаунты»
  phone         TEXT,
  folder        TEXT NOT NULL,            -- абсолютный путь к user_data_dir
  kind          TEXT NOT NULL,            -- noauth | active | done
  proxy_id      INTEGER REFERENCES proxies(id),
  state         TEXT NOT NULL DEFAULT 'free',  -- free|busy|banned|cooldown
  uses          INTEGER NOT NULL DEFAULT 0,
  cart_pending  INTEGER NOT NULL DEFAULT 0,    -- товар в корзине, ждёт выкупа
  last_used_at  TEXT,
  note          TEXT
);

CREATE TABLE proxies (
  id         INTEGER PRIMARY KEY,
  raw        TEXT NOT NULL UNIQUE,        -- socks5://user:pass@host:port
  kind       TEXT NOT NULL,               -- mobile | resident | dc
  state      TEXT NOT NULL DEFAULT 'free',
  fail_count INTEGER NOT NULL DEFAULT 0,
  cooldown_until TEXT,
  last_check_at  TEXT,
  geo        TEXT
);

CREATE TABLE runs (                       -- журнал выполнений, источник «Результатов»
  id           INTEGER PRIMARY KEY,
  task_id      INTEGER REFERENCES tasks(id),
  thread_id    INTEGER,
  profile_id   INTEGER REFERENCES profiles(id),
  started_at   TEXT, finished_at TEXT,
  result       TEXT,                      -- ok | error | skipped
  position     INTEGER,
  cart_added   INTEGER DEFAULT 0,
  liked        INTEGER DEFAULT 0,
  error_code   TEXT,
  error_msg    TEXT,
  steps_json   TEXT                       -- таймлайн шагов для разбора инцидента
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,                    -- JSON
  updated_at TEXT
);

CREATE TABLE sheet_state (               -- что уже отправлено в лист (антидубли)
  task_id   INTEGER PRIMARY KEY,
  payload   TEXT NOT NULL,               -- JSON значений G:N
  pushed_at TEXT
);
```

### Атомарный захват задания

```sql
UPDATE tasks
   SET status = 'claimed',
       claim_owner = :thread_id,
       claim_at = datetime('now'),
       lease_until = datetime('now', '+15 minutes'),
       updated_at = datetime('now')
 WHERE id = (
   SELECT id FROM tasks
    WHERE status = 'pending'
      AND done_runs < target_runs
    ORDER BY priority DESC, id
    LIMIT 1
 )
   AND status = 'pending'
RETURNING *;
```

`BEGIN IMMEDIATE` вокруг вызова; пустой результат = очередь пуста. Watchdog каждые 60 с:

```sql
UPDATE tasks SET status='pending', claim_owner=NULL
 WHERE status IN ('claimed','running') AND lease_until < datetime('now');
```

## 3. Google Sheets

Требования: сервисный аккаунт, таблица расшарена на его `client_email` с правом редактирования. Доступ через `gspread` (`scope: spreadsheets`), ключ `.json` хранится вне репозитория, в настройках — только путь.

### Лист «ПФ2» (задания)

| Кол. | Поле | Кто пишет | Примечание |
|---|---|---|---|
| A | ID | бот | UUID, ставится при импорте, не менять |
| B | Маркетплейс | оператор | `ozon` / `wb`, выпадающий список (data validation) |
| C | Артикул / SKU | оператор | артикул или полный URL |
| D | Ключевой запрос | оператор | |
| E | Гео | оператор | URL города Ozon / адрес ПВЗ WB |
| F | Требуется выполнений | оператор | целое ≥ 1 |
| G | Выполнено | бот | |
| H | Начальная позиция | бот | первый успешный замер |
| I | Фактическая позиция | бот | последний замер |
| J | В корзину | бот | счётчик |
| K | Лайков | бот | счётчик |
| L | Статус | оба | `Ожидает`/`В работе`/`Готово`/`Ошибка`/`Отмена` |
| M | Ошибка | бот | последняя ошибка, обрезается до 300 симв. |
| N | Обновлено | бот | ISO-время, по нему `push` находит дельту |

`Статус=Отмена` от оператора → `tasks.status='cancelled'` (приоритет у человека).
`Требуется выполнений` увеличено → задание снова `pending`.

### Лист «Аккаунты»

| Кол. | Поле |
|---|---|
| A | ID аккаунта |
| B | Телефон / логин |
| C | Папка профиля |
| D | Прокси |
| E | Состояние: `Свободен`/`Занят`/`Бан`/`Прогрев` |
| F | Выполнено задач |
| G | Последнее использование |
| H | Комментарий |

Импорт профилей в SQLite — по колонке `C` (путь) + `A` (ID). Папка должна существовать; иначе строка уходит в «Ошибки импорта».

### Лист «Выкупы»

| Кол. | Поле | Кто пишет |
|---|---|---|
| A | Дата | бот |
| B | ID задания | бот |
| C | SKU | бот |
| D | Аккаунт ID | бот |
| E | Телефон | бот |
| F | Папка профиля | бот |
| G | Прокси | бот |
| H | Позиция на момент добавления | бот |
| I | Статус выкупа | оператор: `Ждёт`/`Выкуплен`/`Отказ` |
| J | Комментарий | оператор |

Строка создаётся в момент `cart_added=1`. `profiles.cart_pending=1` до статуса `Выкуплен`.

## 4. Синхронизация

```
pull  (30–60 с): читать A2:N последних N строк → diff по (ID, Обновлено)
                 → upsert tasks; новые строки без ID получают UUID и запись ID в лист
push  (30–60 с): выбрать tasks WHERE updated_at > last_push AND sheet_id IS NOT NULL
                 → batch_update диапазонами G:N по одной строке
                 → записать last_push только при успехе
```

Правила:
- лимит: ≤ 1 `values.batchUpdate` на цикл, ≤ 20 диапазонов;
- 429/5xx → экспоненциальный backoff (1, 2, 4, 8 с, до 5 попыток), затем событие `log` уровня `warn`;
- запись счётчиков только при реальном изменении — не гонять «дельту» из одинаковых значений;
- `sheets_row` кэшируется, но при `push` диапазон вычисляется по повторному поиску ID (строки могли сдвинуть).

### Фактическая реализация (M1)

`app/sheets/base.py` — контракт `SheetBackend` (`probe/read/write_range/append`) и раскладка колонок.
`app/sheets/gspread_backend.py` — Google, троттлинг 1.05 с между записями (≤ ~55/мин), `batch` через диапазон `G{row}:N{row}`.
`app/sheets/fake.py` — тестовая таблица `%APPDATA%\pf-automation\fake_sheet.json` с той же структурой; включается автоматически, если нет кредов или ID таблицы.
`app/sheets/sync.py` — `pull`/`push`, `sheet_state` как антидубль, событие `sync_state` в UI.

Отличия от идеальной схемы (осознанные упрощения на M1):

- push идёт по одной строке за вызов, а не одним `batchUpdate` на 20 диапазонов: при 10–20 заданиях это укладывается в лимит, оптимизация — после M3;
- `updated_at` в `tasks` не меняется, если оператор не изменил ни одного поля (иначе каждая строка перезаписывалась бы каждые 30 с);
- `sheet_state.payload` — JSON значений `G:N`; совпал — записи нет.

### Excel-режим (локальный .xlsx)

Настройка `general.sheet_xlsx`. Приоритет: Google → Excel → тестовая таблица. Раскладка определяется по заголовкам (`app/sheets/layout.py`), поэтому чужие таблицы не требуют правки кода.

Проверено на файле `table_ozon.xlsx` (лист «Таблица»):

| Кол. | Заголовок | Логическое поле |
|---|---|---|
| A | Дата | `date` (не пишется) |
| B | SKU (ссылка Ozon) | `sku` + `url`: артикул извлекается из ссылки |
| C | Ключевой запрос | `query` |
| D | Использовать фильтр | `filter` (зарезервировано) |
| E | Необходимое кол заходов | `required_visits` (зарезервировано) |
| F | Город | `geo` |
| G | Кол заданий | `target_runs` |
| H | Позиция Начальная | `start_position` |
| I | Позиция Факт | `position` |
| J | Кол добавлений в корзину | `cart_adds` |
| K | Кол лайков | `likes` |
| L | Статус итог | `status_total` (пишется только в терминальном статусе) |
| M | Статус | `status` — команды оператора и текущий статус |

Правила Excel-режима:

- нет колонки `ID` → ключ задания `excel:<лист>:<строка>`; сортировка/вставка строк ломает связь, для устойчивости можно добавить колонку `ID` (бот выдаст UUID);
- перед первой записью создаётся `*.backup-ГГГГММДД-ЧЧММСС.xlsx`;
- файл открыт в Excel → запись падает с понятной ошибкой, БД и данные не страдают;
- при первом добавлении в корзину создаётся лист «Выкупы»; «Аккаунты» не создаётся;
- если имя листа в настройке не совпадает, берётся единственный лист кроме служебных («Аккаунты»/«Выкупы»);
- **изоляция источников:** у задания есть `tasks.source`; при смене таблицы задания прежнего источника получают `paused` и не пишутся в новый файл. Возврат источника снимает паузу.

### Тестовый режим

Пока нет сервисного аккаунта, вся цепочка проверяется без сети:

1. приложение создаёт `fake_sheet.json` с тремя заданиями (target 2–3);
2. `pull` переносит их в SQLite (ID уже проставлены);
3. прогон обновляет `Выполнено`, позиции, лайки, статус;
4. строка появляется в листе «Выкупы» при добавлении в корзину;
5. «Сбросить тестовые данные» пересоздаёт файл и чистит `tasks`/`runs`/`sheet_state`.

Проверка: `backend/tests/test_sync.py`.

## 5. Настройки (settings.json, схема)

```jsonc
{
  "general": {
    "fingerprint_key": "",              // только для совместимых провайдеров
    "google_credentials": "C:/app/creds/sa.json",
    "spreadsheet_id": "",
    "sheet_tasks": "ПФ2",
    "sheet_accounts": "Аккаунты",
    "sheet_orders": "Выкупы",
    "proxies_file": "C:/app/proxies.txt",
    "dir_active": "C:/profiles/active",
    "dir_done": "C:/profiles/done",
    "dir_noauth": "C:/profiles/noauth",
    "threads": 4,
    "repeat_limit": 0,                  // 0 = без лимита
    "error_limit": 10,                  // стоп прогона после N ошибок подряд
    "sync_enabled": true,
    "sync_interval_sec": 30,            // 10..600
    "schedule_enabled": false,
    "schedule_interval_min": 60,        // 5..1440
    "quick_actions": { "cart_from_search": false, "goto_cart": false,
                       "view_photos": true, "favorite": false }
  },
  "view": {
    "profile_use_pct": 60,
    "like_pct": 30,
    "cart_pct": 15,
    "other_sellers_pct": 10,
    "see_also_pct": 10,
    "compare_pct": 5,
    "review_like_pct": 10,
    "max_search_pages": 3,
    "card_time_sec": [180, 320],
    "reviews_time_sec": [40, 90]
  },
  "browser": {
    "provider": "cdp",                  // cdp | camoufox | vanilla
    "cdp_endpoint": "http://127.0.0.1:3001",
    "headless": false,
    "enabled": false,                   // потоки открывают реальный браузер
    "check_url": "https://example.com/",// проверочная страница M2
    "locale": "ru-RU",
    "timezone": "Europe/Moscow"
  },
  "license": { "key": "", "server": "https://lic.example/api" }
}
```

Проверки при сохранении: диапазоны `0..100`, `min <= max`, существование путей, доступность листов (по кнопке «Проверить»), парсинг `proxies.txt` (построчно, `socks5://`/`http://`, отбрасывать невалидные с отчётом).
