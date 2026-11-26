# Архитектура: Python-движок + Node/Electron UI

## 1. Почему такое разделение

| Слой | Технология | Отвечает за |
|---|---|---|
| Движок | Python 3.12, asyncio, Playwright (async) | потоки, браузеры, профили, прокси, сценарии, очередь, Google Sheets, лицензия |
| UI | Electron + React + TypeScript (Vite) | окна настроек, рабочее окно, логи, результаты, управление потоками |
| Транспорт | localhost HTTP + WebSocket | команды UI → движок, события движок → UI |

Почему не PyQt6 в одном процессе:
- UI на скриншотах насыщенный (табы, таблицы, живые логи, панель потоков) — на React это в 3–4 раза быстрее;
- Electron умеет показывать real-time лог на 10k строк без тормозов, Qt-модели для этого требуют ручной работы;
- UI можно обновлять без пересборки Python-ядра.

Цена решения — IPC. Поэтому контракт между слоями фиксируется сразу (раздел 5) и не меняется.

**Важно:** Python — единственный владелец состояния. UI никогда не пишет в БД и Sheets напрямую, только через API. Это убирает гонки между процессами.

## 2. Компонентная схема

```
┌────────────────────────── Electron ──────────────────────────┐
│ main (Node)  spawn backend.exe / python -m app, lifecycle,    │
│              single-instance lock, автозапуск, трей           │
│ renderer (React)  Настройки(Основные|Просмотр) | Рабочее окно │
└───────────────▲───────────────────────────┬──────────────────┘
                │ WS events / REST          │
┌───────────────┴───────────────────────────▼──────────────────┐
│ FastAPI + uvicorn (127.0.0.1:8765, token в заголовке)        │
│  ├─ api/settings   чтение/запись настроек                    │
│  ├─ api/run        start / stop / pause / threads            │
│  ├─ api/manual     ручной режим (профиль + пауза)            │
│  ├─ api/sheets     тест подключения, принудительный синк      │
│  └─ ws             log, thread_state, task_update, metric    │
├──────────────────────────────────────────────────────────────┤
│ Supervisor (asyncio)                                          │
│  ├─ Semaphore(N)  ровно N одновременных потоков              │
│  ├─ TaskQueue     атомарный захват задания (SQLite)          │
│  ├─ SheetsSync    pull/push, батчи, ретраи                   │
│  └─ Watchdog      ошибки, лимит ошибок, рестарт потока       │
├──────────────────────────────────────────────────────────────┤
│ Worker (один на поток)                                        │
│  ├─ BrowserSession  provider → context → page                │
│  ├─ ProxyPool       выдача/возврат, cooldown, проверка       │
│  ├─ ProfileStore    active / done / noauth                   │
│  ├─ Humanizer       мышь, печать, скролл, паузы              │
│  └─ Scenario       mp_ozon / mp_wb (шаги)                    │
├──────────────────────────────────────────────────────────────┤
│ SQLite (WAL) — единственный источник истины по очереди       │
└──────────────────────────────────────────────────────────────┘
```

## 3. Конкурентность

- Один `asyncio` event loop на процесс. Поток = `asyncio.Task`, ограниченный `Semaphore(threads)`.
- **Не** использовать sync Playwright API в потоках: он конфликтует с asyncio и плохо дружит с большим числом контекстов.
- Один Chromium-процесс на профиль (`launch_persistent_context(user_data_dir=...)`), потому что профиль = папка. 8–10 профилей = 8–10 процессов, RAM ~250–400 МБ каждый — это норматив для планирования.
- Прокси задаётся на уровне контекста (`proxy={"server": ...}`), а не переменной окружения.
- Все внешние вызовы (Sheets, fingerprint API) — с таймаутом и ретраем (tenacity), никогда не блокируют поток дольше лимита.

## 4. Слой браузера: провайдер как интерфейс

Ключевой вывод по антидетекту: **лицензия FingerprintSwitcher привязана к BAS, переиспользовать её из Python нельзя.** Поэтому вводится абстракция:

```python
class BrowserProvider(Protocol):
    async def open(self, profile: Profile, proxy: Proxy | None) -> BrowserSession: ...
    async def close(self, session: BrowserSession) -> None: ...
```

Реализации (выбирается в настройках):

1. `AntidetectCdpProvider` — **рекомендуемый**. Локальный API Dolphin{anty} / AdsPower / Octo / GoLogin отдаёт `ws_endpoint`, дальше `playwright.chromium.connect_over_cdp(...)`. Плюсы: их движок даёт настоящий TLS/HTTP2-отпечаток Chrome, куки и fingerprint уже консистентны, «Показать браузер» работает бесплатно (окно их клиента).
2. `CamoufoxProvider` — open-source Firefox-сборка с подменой отпечатков, локально, без подписки. Дешевле, но Firefox на маркетплейсах выглядит подозрительнее Chrome.
3. `VanillaChromiumProvider` — Playwright Chromium + persistent context + свой набор init-скриптов. Только для отладки сценариев: TLS-отпечаток Python-сборки Chromium детектируется.

Правило: сценарии пишутся против `BrowserSession`, провайдер меняется одной строкой конфига.

### Что реализовано (M2)

- `app/browser/providers.py` — `VanillaProvider` (persistent context, локаль, **системный часовой пояс по умолчанию**, прокси, `--disable-blink-features=AutomationControlled`, `ignore_default_args=["--enable-automation"]`, **нативный UA и Google Chrome через `channel=auto`**), `CdpProvider` (`connect_over_cdp`, не закрывает чужой браузер), `CamoufoxProvider` (явная ошибка «не подключён»). Опционально `PF_ENGINE=patchright` — движок без CDP-следов.
- `app/browser/profiles.py` — `active`/`done`/`noauth`, выбор по проценту из настроек, эфемерные профили `auto-ММДД-ЧЧММСС-xxxxxx`, учёт занятых папок.
- `app/browser/proxies.py` — разбор `socks5://user:pass@host:port`, выдача/возврат, счётчик невалидных строк.
- `app/browser/session.py` — `refresh_window()`, `set_visible()`, фактическое состояние окна из Win32.
- Проверка: `POST /api/browser/check`, `GET /api/browser/status`, тест `tests/test_browser.py`.

## 5. Контракт UI ↔ движок

REST (все ответы JSON, ошибка = `{"error": {...}}`):

| Метод | Назначение |
|---|---|
| `GET/PUT /api/settings` | настройки, валидация, маскирование ключей в ответе |
| `POST /api/run/start` | запуск с текущими настройками |
| `POST /api/run/stop` | мягкая остановка (дождаться текущего шага) |
| `POST /api/run/restart` | stop + start |
| `POST /api/threads/{id}/browser` | `{"visible": true|false}` |
| `POST /api/threads/{id}/close` | закрыть поток |
| `POST /api/manual/start` | `{"profile_id": ..., "proxy": ...}` → браузер открыт, поток на паузе |
| `POST /api/sheets/test` | проверка доступа и листов |
| `POST /api/sheets/sync` | принудительный обмен |
| `GET /api/browser/status` | Chromium, профили, прокси, провайдер |
| `POST /api/browser/check` | проверочный запуск браузера без очереди |
| `GET /api/manual` | активные ручные сессии |
| `POST /api/manual/start` | `{profile_path?, proxy?, url?}` → сессия открыта |
| `POST /api/manual/{id}/browser` | показать/спрятать окно сессии |
| `POST /api/manual/{id}/navigate` | переход по URL внутри сессии |
| `POST /api/manual/{id}/close` | закрыть сессию |

WS-события (сервер → клиент, поле `type`):

| type | payload |
|---|---|
| `log` | `{ts, thread, level, msg}` — ровно формат `[09:38:44] Поток №1 : …` |
| `thread_state` | `{id, status, task_id, profile_id, proxy, last_action}` |
| `task_update` | `{task_id, done_runs, position, cart_adds, likes, status}` |
| `metric` | `{threads_active, tasks_pending, tasks_done, errors}` |
| `run_state` | `{state: idle|running|stopping|paused}` |

## 6. «Показать / Спрятать браузер»

Playwright не умеет встраивать Chromium в Electron. Рабочий приём для Windows:

1. Провайдер возвращает PID процесса браузера.
2. `EnumWindows` + `GetWindowThreadProcessId` → HWND верхнего окна этого PID.
3. `ShowWindow(hwnd, SW_HIDE)` / `SW_SHOW` + `SetForegroundWindow`.

Обёртка — `app/browser/win_window.py` (pywin32). Для antidetect-провайдеров вместо этого используется их API `show/hide` по профилю — надёжнее.

Реализация в M2: Playwright не отдаёт PID persistent-контекста, поэтому PID ищется через `psutil` по `--user-data-dir=<папка профиля>` среди дочерних процессов, затем `EnumWindows` выбирает самое крупное видимое окно класса `Chrome_WidgetWin_*`. `set_visible()` возвращает фактическое состояние из `IsWindowVisible`, а не предположение — это же значение отдаётся в `/api/run/state` как `window_visible` и проверяется тестом.

## 7. Очередь и Google Sheets

Самая частая ошибка таких проектов — использовать лист как очередь. Google Sheets: ~60 запросов записи/мин на пользователя, нет транзакций, задержка 1–3 с. При 10 потоках это гарантированные двойные захваты строк.

Схема:

- **SQLite — истина.** Захват задания = один `UPDATE ... WHERE status='pending' ... RETURNING` (атомарно, WAL).
- **Sheets — панель оператора.** `SheetsSync` раз в 30–60 с: `pull` (новые/изменённые строки по `updated_at`-колонке) и `push` (только изменённые ячейки, `batch_update`, не более 1 запроса на 20–30 изменений).
- Строка в листе получает `ID` (UUID) при первом импорте; дальше связь по ID, а не по номеру строки — строки можно сортировать и вставлять.
- Конфликт «оператор изменил, бот изменил» → приоритет у оператора для полей `target_runs`, `status=Отмена`; счётчики (`done_runs`, позиции, лайки) всегда пишет бот.
- Lease: захваченное задание живёт 15 мин; если поток умер, `Watchdog` возвращает его в `pending`. Это заменяет «ручную» блокировку статусом.

## 8. Humanizer

Модуль, который делает действия похожими на человека. Не пытается «обмануть антифрод сам по себе» — снижает шум поверх нормального отпечатка:

- мышь: кубическая кривая Безье между точками + jitter 1–3 px + микропаузы 20–80 мс, финальное замедление перед кликом 120–250 мс;
- печать: задержка по lognormal (медиана ~110 мс/символ), паузы на пробелах, 3–6% опечаток с исправлением через Backspace;
- скролл: инерция (серия wheel-событий с затуханием), случайные откаты вверх на 5–15%;
- реализовано в `app/human/__init__.py`: `move` (Безье, 10–18 шагов, jitter), `click` (точка в центральной трети элемента), `type_text` (4% опечаток, исправление Backspace), `scroll` (3–7 импульсов + откаты), `read` (пауза с микродвижениями), `pause`;
- паузы: не фиксированные, а `uniform`/`lognormal` в заданном диапазоне;
- сессия: 5–10% времени «просто читает» без действий.

Реализуется как тонкая обёртка над `page`, чтобы сценарий читался как обычный код.

## 8.1 Сценарий Ozon (M3)

- **Селекторы:** `app/flows/selectors/ozon.json` + `SelectorRegistry` с порядком вариантов (widget → css → aria → текст). Ненайденный обязательный элемент = `selector_not_found` с ключом и списком попыток, а не пустой результат.
- **Позиция:** выдача листается через `&page=N` (кнопки «Дальше» в DOM нет), карточки собираются накопительно при скролле, потому что `tileGridDesktop` виртуализирован. Позиция = сумма карточек предыдущих страниц + индекс на текущей. Защита от зацикливания: если набор SKU повторился, пагинация считается законченной.
- **Карточка:** открывается кликом по ссылке найденного SKU (fallback — прямой URL). Действия: фото, описание, отзывы (тайминг из настроек), лайк отзыва, избранное (в Ozon это `webAddToFavorite`), сравнение, «Смотрите также», «Другие продавцы», корзина. Необязательные шаги при отсутствии элемента логируют `warn` и продолжают; ключевые — падают.
- **Тайминг:** целевое время карточки берётся из `card_time_sec`, действия «съедают» его часть, остаток добивается чтением. Никаких фиксированных `sleep(300)`.
- **Ошибки и артефакты:** коды `ozon_blocked`, `search_empty`, `position_not_found`, `selector_not_found`, `ozon_crash`; при любой ошибке сохраняются `page.png`, `page.html`, `url.txt` в `%APPDATA%\pf-automation\artifacts`.
- **Проверка без сети:** `tools/capture_ozon.py` снимает реальные `search.html`/`card.html`, `tests/test_ozon_fixture.py` прогоняет сценарий через перехват маршрутов. Это позволяет отлаживать логику, когда площадка блокирует IP.
- **Антибот:** с датацентрового IP Ozon отвечает 403 на выдачу независимо от headless/UA — для работы нужны residential/mobile-прокси и прогретые профили.

## 8.2 Ручной режим (M6)

- `core/manual.py` — `ManualManager` с независимыми от прогона сессиями: профиль + прокси + стартовый URL.
- Сессии живут без таймаута, закрываются оператором или при выходе приложения (`finally` в lifespan).
- Профиль, занятый потоком, блокируется: `Supervisor.busy_profile_paths()` сравнивается с выбранной папкой.
- Show/hide — тот же Win32-механизм, что у потоков; HWND фиксируется при открытии и не переискивается (иначе можно показать служебное окно вместо браузера).
- UI: вкладка «Ручной режим» — выбор профиля из active/done/noauth, прокси, URL, карточки сессий с кнопками «Показать/Спрятать», «Перейти», «Закрыть».
- API: `GET /api/manual`, `POST /api/manual/start`, `/{id}/browser`, `/{id}/navigate`, `/{id}/close`, `GET /api/manual/profiles`.

## 8.3 Планировщик, отчёт, лог (M5)

- `core/scheduler.py`: `tick(now)` — чистая логика без `sleep` (тестируется с подменой часов): disabled → scheduled → waiting → started / busy / empty / error. На `busy` окно сдвигается, чтобы прогоны не наслаивались. `schedule_next()` считает ближайший запуск сразу после смены настроек, не дожидаясь тика.
- `api/report.py`: `/api/report` собирает задания, прогоны, сессии, профили, прокси, процессы (psutil: Chromium + ядро), размер БД и артефактов. Отдельный файл, чтобы не раздувать `system.py`.
- UI: модальные окна «Планировщик» и «Отчёт о ресурсах» (`components/Dialogs.tsx`), кнопки в панели рабочего окна.
- Лог: окно рендера по фиксированной высоте строки (18 px), в DOM только видимая область + запас. Полный текст — в `title`.

## 9. Лицензирование и сборка

- Ключ = строка `PF1.<payload>.<sig>`: payload JSON `{sub, hwids[], exp, features[], iat}`, подпись Ed25519. Проверка подписи локальная (`core/license.py`), поэтому приложение работает без сети.
- HWID: `MachineGuid` + серийник системного тома + MAC, SHA-256, формат `HWID-XXXX-…`; переопределяется `PF_HWID` (тесты, поддержка).
- Онлайн-проверка необязательна: если задан сервер, `POST /check` раз в 5 минут; при недоступности — офлайн-допуск 7 дней с последней успешной проверки. Без сервера достаточно подписи.
- Гейт: `Supervisor.start()` и `POST /api/manual/start` не стартуют при невалидном ключе (истёк, чужой HWID, подпись). Причина пишется в лог.
- Публичный ключ: `PF_LICENSE_PUBKEY` → `%APPDATA%/pf-automation/license_pubkey.txt` → `license_pubkey.txt` рядом с модулем. Пусто = режим разработки без лицензии.
- Выпуск: `backend/tools/make_license.py` (`init`, `hwid`, `issue --hwid … --days …`, `verify`); приватный ключ в `backend/license_keys/` (в git и установщик не попадает).
- Сборка: `scripts/build-backend.ps1` — Nuitka `--standalone` + smoke `/api/health`; ядро в `build/backend/pf-backend.dist`. `scripts/build-installer.ps1` — electron-builder NSIS, ядро в `resources/backend`, установщик в `ui/release`.
- В установленном приложении Electron запускает `resources/backend/pf-backend.exe`; в разработке — `backend/.venv` и `python -m app`.
- Обфускация Python как таковая не защищает; защита = серверная выдача ключевых данных (списки прокси, шаблоны) + лицензия + Nuitka.

## 10. Структура репозитория

```
backend/
  pyproject.toml
  app/
    main.py                 # uvicorn + lifespan
    api/                    # routers: settings, run, threads, manual, sheets
    ws.py                   # EventBus → WebSocket
    core/
      config.py             # pydantic-settings, settings.json
      supervisor.py         # запуск/остановка прогона, watchdog
      events.py
    db/
      models.py             # SQLModel
      migrations/           # alembic
      repo_tasks.py         # атомарный claim/lease
    sheets/
      client.py             # gspread + retry + rate limiter
      sync.py               # pull/push, mapping
    browser/
      provider.py           # Protocol + фабрика
      providers/{cdp,camoufox,vanilla}.py
      win_window.py
      proxy_pool.py
      profile_store.py
    human/
      mouse.py, typing.py, scroll.py, delays.py
    flows/
      base.py               # шаги, контекст, отчёт
      ozon/{search,card,reviews,cart}.py
      wb/{...}.py
    licensing/
    utils/
  tests/
ui/
  package.json
  electron/{main.ts, preload.ts}
  src/                      # React: pages/Settings, pages/Worker, components/*
  vite.config.ts
docs/
```

## 11. Риски, которые надо принять до старта

1. **Правила площадок.** Ozon/WB прямо запрещают автоматизированные действия; следствие — блокировки аккаунтов, обнуление бонусов, претензии. Это не технический, а операционный риск: закладывайте одноразовые аккаунты и не используйте личные.
2. **Антидетект — не гарантия.** Основные факторы решения антифрода: репутация ASN прокси, возраст/история аккаунта, номера для SMS, паттерн заказов. Код влияет меньше, чем инфраструктура.
3. **Селекторы.** Маркетплейсы меняют вёрстку и включают антибот (Ozon — `challenge`, WB — `geo_block`/captcha). Нужен слой `SelectorRegistry` с версионированием и алертом «селектор не найден», иначе бот будет молча «работать» с нулевым результатом.
4. **Экономика.** 180–320 с на карточку × 1 поток ≈ 12–18 действий/час. При 10 потоках — 120–180/час; это определяет, сколько прокси и профилей нужно, и считается до разработки, а не после.
5. **Юридически** накрутка поведенческих факторов и фиктивные заказы в РФ могут квалифицироваться как недобросовестная конкуренция/мошенничество при использовании бонусов площадки. Решение о запуске — на стороне заказчика.
