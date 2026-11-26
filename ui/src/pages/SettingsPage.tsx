import { useEffect, useState, type ReactNode } from "react";
import type { ApiClient } from "../api";
import type { Settings, SyncState } from "../types";

type ProfileRow = {
  name: string;
  new: boolean;
  proxy: string | null;
  proxy_status: string | null;
  proxy_missing: boolean;
};
type ProfileStats = {
  active: number;
  new: number;
  done: number;
  noauth: number;
  busy: number;
  items?: ProfileRow[];
};
type ProxyRow = {
  index: number;
  server: string;
  masked: string;
  valid: boolean;
  status: string | null;
  profiles: number;
};
type ProxyList = { file: string; items: ProxyRow[]; total: number; invalid: number };
type WarmupStatus = {
  running: boolean;
  total: number;
  done: number;
  failed: number;
  current: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
};
type ProxyCheckItem = {
  masked: string;
  server: string;
  status: "ok" | "blocked" | "challenge" | "error";
  http: number | null;
  error: string | null;
  checked_at: string;
};
type ProxyCheckStatus = {
  running: boolean;
  total: number;
  checked: number;
  ok: number;
  blocked: number;
  challenge: number;
  failed: number;
  current: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  items: ProxyCheckItem[];
};

type Props = {
  api: ApiClient;
  settings: Settings;
  sync: SyncState | null;
  onChange: (next: Settings) => void;
  onNotice: (text: string) => void;
  onSync: () => void;
};

export default function SettingsPage({ api, settings, sync, onChange, onNotice, onSync }: Props) {
  const [section, setSection] = useState<"general" | "view">("general");
  const [busy, setBusy] = useState(false);
  const [profileStats, setProfileStats] = useState<ProfileStats | null>(null);
  const [genCount, setGenCount] = useState(20);
  const [genBusy, setGenBusy] = useState(false);
  const [warmup, setWarmup] = useState<WarmupStatus | null>(null);
  const [warmCount, setWarmCount] = useState(10);
  const [proxyCheck, setProxyCheck] = useState<ProxyCheckStatus | null>(null);
  const [proxyList, setProxyList] = useState<ProxyList | null>(null);
  const [proxyLine, setProxyLine] = useState("");
  const [proxyBusy, setProxyBusy] = useState(false);

  const loadProfileStats = async () => {
    try {
      setProfileStats(await api.get<ProfileStats>("/api/browser/profiles"));
    } catch {
      /* счётчик не критичен для работы настроек */
    }
  };

  const loadWarmup = async () => {
    try {
      setWarmup(await api.get<WarmupStatus>("/api/browser/warmup"));
    } catch {
      /* статус прогрева не критичен */
    }
  };

  const loadProxyCheck = async () => {
    try {
      setProxyCheck(await api.get<ProxyCheckStatus>("/api/browser/proxies/check"));
    } catch {
      /* проверка прокси не критична */
    }
  };

  const loadProxies = async () => {
    try {
      setProxyList(await api.get<ProxyList>("/api/browser/proxies"));
    } catch {
      /* список прокси не критичен */
    }
  };

  useEffect(() => {
    void loadProfileStats();
    void loadWarmup();
    void loadProxyCheck();
    void loadProxies();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  useEffect(() => {
    if (!warmup?.running) return;
    let alive = true;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.get<WarmupStatus>("/api/browser/warmup");
        if (!alive) return;
        setWarmup(next);
        if (!next.running) {
          void loadProfileStats();
          void loadProxies();
        }
      } catch {
        /* ядро перезапускается — попробуем на следующем тике */
      }
    }, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [warmup?.running, api]);

  useEffect(() => {
    if (!proxyCheck?.running) return;
    let alive = true;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.get<ProxyCheckStatus>("/api/browser/proxies/check");
        if (!alive) return;
        setProxyCheck(next);
        if (!next.running) {
          void loadProxies();
          void loadProfileStats();
        }
      } catch {
        /* ядро перезапускается — попробуем на следующем тике */
      }
    }, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proxyCheck?.running, api]);

  const generateProfiles = async () => {
    const count = Math.max(1, Math.min(500, Number(genCount) || 0));
    setGenBusy(true);
    try {
      const result = await api.post<{
        ok: boolean;
        error?: string;
        created?: number;
        names?: string[];
        profiles?: ProfileStats;
      }>("/api/browser/profiles/generate", { count });
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось создать профили");
        return;
      }
      await loadProfileStats();
      const names = result.names ?? [];
      onNotice(
        `Создано профилей: ${result.created ?? count}` +
          (names.length ? ` (${names[0]} … ${names[names.length - 1]})` : ""),
      );
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setGenBusy(false);
    }
  };

  const setGeneral = <K extends keyof Settings["general"]>(key: K, value: Settings["general"][K]) =>
    onChange({ ...settings, general: { ...settings.general, [key]: value } });
  const setView = <K extends keyof Settings["view"]>(key: K, value: Settings["view"][K]) =>
    onChange({ ...settings, view: { ...settings.view, [key]: value } });
  const setBrowser = <K extends keyof Settings["browser"]>(key: K, value: Settings["browser"][K]) =>
    onChange({ ...settings, browser: { ...settings.browser, [key]: value } });
  const setQuick = <K extends keyof Settings["general"]["quick_actions"]>(
    key: K,
    value: Settings["general"]["quick_actions"][K],
  ) => setGeneral("quick_actions", { ...settings.general.quick_actions, [key]: value });

  const pickExcel = async () => {
    try {
      const path = await window.pf?.pickExcel?.();
      if (!path) return;
      setGeneral("sheet_xlsx", path);
      onNotice("Excel выбран. Нажмите «Сохранить», чтобы применить.");
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const startWarmup = async () => {
    const count = Math.max(1, Math.min(500, Number(warmCount) || 0));
    try {
      const result = await api.post<{
        ok: boolean;
        error?: string;
        total?: number;
        status?: WarmupStatus;
      }>("/api/browser/warmup/start", { count });
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось запустить прогрев");
        return;
      }
      if (result.status) setWarmup(result.status);
      onNotice(`Прогрев запущен: ${result.total ?? count} профилей`);
      await loadProfileStats();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const stopWarmup = async () => {
    try {
      const result = await api.post<{ ok: boolean; error?: string; status?: WarmupStatus }>(
        "/api/browser/warmup/stop",
      );
      if (result.status) setWarmup(result.status);
      onNotice(result.ok ? "Прогрев остановлен" : (result.error ?? "Прогрев не запущен"));
      await loadProfileStats();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const startProxyCheck = async () => {
    try {
      const result = await api.post<{ ok: boolean; error?: string; total?: number; status?: ProxyCheckStatus }>(
        "/api/browser/proxies/check/start",
      );
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось запустить проверку");
        return;
      }
      if (result.status) setProxyCheck(result.status);
      onNotice(`Проверка прокси запущена: ${result.total ?? 0} шт.`);
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const stopProxyCheck = async () => {
    try {
      const result = await api.post<{ ok: boolean; error?: string; status?: ProxyCheckStatus }>(
        "/api/browser/proxies/check/stop",
      );
      if (result.status) setProxyCheck(result.status);
      onNotice(result.ok ? "Проверка прокси остановлена" : (result.error ?? "Проверка не запущена"));
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const pruneProxies = async (includeErrors: boolean) => {
    const what = includeErrors ? "нерабочие и заблокированные" : "заблокированные";
    if (!window.confirm(`Удалить из proxies.txt ${what} прокси? Старый файл сохранится в .bak.`)) {
      return;
    }
    try {
      const result = await api.post<{
        ok: boolean;
        error?: string;
        removed?: number;
        left?: number;
        status?: ProxyCheckStatus;
      }>("/api/browser/proxies/prune", { include_errors: includeErrors });
      if (result.status) setProxyCheck(result.status);
      onNotice(
        result.ok
          ? `Удалено прокси: ${result.removed} · осталось: ${result.left}`
          : (result.error ?? "Не удалось почистить прокси"),
      );
      if (result.ok) {
        // У профилей, привязанных к удалённым адресам, сразу выбираем новые.
        await api.post("/api/browser/proxies/assign");
        await loadProxies();
        await loadProfileStats();
      }
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const addProxy = async () => {
    const line = proxyLine.trim();
    if (!line) return;
    setProxyBusy(true);
    try {
      const result = await api.post<ProxyList & { ok: boolean; error?: string }>("/api/browser/proxies", {
        line,
      });
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось добавить прокси");
        return;
      }
      setProxyLine("");
      setProxyList(result);
      onNotice("Прокси добавлен");
      await loadProfileStats();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setProxyBusy(false);
    }
  };

  const deleteProxy = async (index: number) => {
    if (!window.confirm("Удалить этот прокси из файла?")) return;
    setProxyBusy(true);
    try {
      const result = await api.delete<ProxyList & { ok: boolean; error?: string }>(
        `/api/browser/proxies/${index}`,
      );
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось удалить прокси");
        return;
      }
      setProxyList(result);
      onNotice("Прокси удалён");
      await loadProfileStats();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setProxyBusy(false);
    }
  };

  const assignProxies = async () => {
    setProxyBusy(true);
    try {
      const result = await api.post<ProxyList & { ok: boolean; error?: string; assigned?: number }>(
        "/api/browser/proxies/assign",
      );
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось закрепить прокси");
        return;
      }
      setProxyList(result);
      onNotice(`Закреплено прокси за профилями: ${result.assigned ?? 0}`);
      await loadProfileStats();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setProxyBusy(false);
    }
  };

  const rebalanceProxies = async () => {
    if (
      !window.confirm(
        "Пересчитать привязки всех профилей по всему пулу? У профилей сменится IP.",
      )
    ) {
      return;
    }
    setProxyBusy(true);
    try {
      const result = await api.post<ProxyList & { ok: boolean; error?: string; assigned?: number }>(
        "/api/browser/proxies/rebalance",
      );
      if (!result.ok) {
        onNotice(result.error ?? "Не удалось перераспределить прокси");
        return;
      }
      setProxyList(result);
      onNotice(`Перераспределено профилей: ${result.assigned ?? 0}`);
      await loadProfileStats();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setProxyBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      await api.put("/api/settings", settings);
      const check = await api.post<{ ok: boolean; warnings: string[] }>("/api/settings/validate", settings);
      await loadProfileStats();
      await loadProxies();
      onNotice(
        check.warnings.length
          ? `Сохранено, но есть замечания: ${check.warnings.join(" · ")}`
          : "Настройки сохранены",
      );
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const testBrowser = async () => {
    onNotice("Запускаем браузер…");
    try {
      const result = await api.post<{
        ok: boolean;
        error?: string;
        title?: string;
        http?: number;
        kind?: string;
        profile?: string;
        visible?: boolean;
        hwnd?: number | null;
      }>("/api/browser/check");
      if (!result.ok) {
        onNotice(result.error ?? "Браузер не запустился");
        return;
      }
      onNotice(
        `Браузер ок: «${result.title}» · HTTP ${result.http} · ${result.kind} · ` +
          `профиль ${result.profile}` +
          (result.visible ? ` · окно ${result.hwnd ?? ""}` : " · headless"),
      );
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const resetDemo = async () => {
    if (!window.confirm("Удалить задания, результаты и пересоздать тестовую таблицу?")) return;
    try {
      const result = await api.post<{ ok: boolean; error?: string }>("/api/dev/reset");
      onNotice(result.ok ? "Тестовые данные сброшены" : (result.error ?? "Не удалось сбросить"));
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const testSheets = async () => {
    try {
      const result = await api.post<{
        ok: boolean;
        error?: string;
        mode?: string;
        title?: string;
        path?: string;
        sheets?: string[];
        missing?: string[];
      }>("/api/sheets/test");
      if (!result.ok) onNotice(result.error ?? `Нет листов: ${result.missing?.join(", ")}`);
      else {
        const label =
          result.mode === "fake" ? "Тестовая таблица" : result.mode === "excel" ? "Excel" : "Google Sheets";
        onNotice(`${label}: ${result.title}. Листы: ${result.sheets?.join(", ")}`);
      }
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const proxyItems = proxyCheck?.items ?? [];
  const badProxies = proxyItems.filter(
    (item) => item.status === "blocked" || item.status === "error",
  );
  const problemProxies = proxyItems.filter((item) => item.status !== "ok");
  const statusText: Record<ProxyCheckItem["status"], string> = {
    ok: "ок",
    blocked: "блок Ozon",
    challenge: "капча",
    error: "ошибка",
  };
  const proxyStatusText: Record<string, string> = {
    ok: "ок",
    blocked: "блок Ozon",
    challenge: "капча",
    error: "ошибка",
    invalid: "не распознан",
  };
  const proxyStatusLabel = (status: string | null | undefined) =>
    status ? (proxyStatusText[status] ?? status) : "не проверен";
  const profileRows = profileStats?.items ?? [];

  return (
    <div className="settings">
      <div className="subtabs">
        <button
          className={section === "general" ? "subtab active" : "subtab"}
          onClick={() => setSection("general")}
        >
          Основные
        </button>
        <button className={section === "view" ? "subtab active" : "subtab"} onClick={() => setSection("view")}>
          Настройки просмотра
        </button>
        <div className="spacer" />
        <button className="btn ghost" onClick={testSheets} disabled={busy}>
          Проверить Google Sheets
        </button>
        <button className="btn primary" onClick={save} disabled={busy}>
          {busy ? "Сохранение…" : "Сохранить"}
        </button>
      </div>

      {section === "general" ? (
        <div className="grid">
          <fieldset className="panel">
            <legend>Доступы и файлы</legend>
            <Field label="Ключ FingerPrint">
              <input
                value={settings.general.fingerprint_key}
                onChange={(e) => setGeneral("fingerprint_key", e.target.value)}
                placeholder="используется только CDP-провайдерами"
              />
            </Field>
            <Field label="Google API (.json)" hint="файл сервисного аккаунта">
              <input
                value={settings.general.google_credentials}
                onChange={(e) => setGeneral("google_credentials", e.target.value)}
                placeholder="C:\app\creds\service-account.json"
              />
            </Field>
            <Field label="ID Google-таблицы">
              <input
                value={settings.general.spreadsheet_id}
                onChange={(e) => setGeneral("spreadsheet_id", e.target.value)}
                placeholder="1AbC…"
              />
            </Field>
            <div className="row3">
              <Field label="Лист заданий">
                <input value={settings.general.sheet_tasks} onChange={(e) => setGeneral("sheet_tasks", e.target.value)} />
              </Field>
              <Field label="Лист аккаунтов">
                <input
                  value={settings.general.sheet_accounts}
                  onChange={(e) => setGeneral("sheet_accounts", e.target.value)}
                />
              </Field>
              <Field label="Лист выкупов">
                <input
                  value={settings.general.sheet_orders}
                  onChange={(e) => setGeneral("sheet_orders", e.target.value)}
                />
              </Field>
            </div>
            <Field label="Файл прокси (.txt)" hint="построчно: socks5://user:pass@ip:port">
              <input
                value={settings.general.proxies_file}
                onChange={(e) => setGeneral("proxies_file", e.target.value)}
                placeholder="C:\app\proxies.txt"
              />
            </Field>
            <div className="field">
              <span className="field-label">Список прокси</span>
              <div className="table-wrap" style={{ maxHeight: 240 }}>
                <table>
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Прокси</th>
                      <th>Статус</th>
                      <th>Профилей</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {(proxyList?.items ?? []).map((row) => (
                      <tr key={row.index}>
                        <td className="muted">{row.index + 1}</td>
                        <td title={row.server}>{row.masked}</td>
                        <td>{proxyStatusLabel(row.status)}</td>
                        <td>{row.profiles}</td>
                        <td>
                          <button
                            className="btn danger"
                            type="button"
                            onClick={() => deleteProxy(row.index)}
                            disabled={proxyBusy}
                          >
                            Удалить
                          </button>
                        </td>
                      </tr>
                    ))}
                    {!proxyList?.items.length && (
                      <tr>
                        <td colSpan={5} className="muted">
                          Прокси пока нет
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <span className="field-hint">
                Файл: {proxyList?.file || "не задан"}. Пароли скрыты, в файле они сохраняются как есть.
              </span>
              <div className="input-row">
                <input
                  value={proxyLine}
                  onChange={(e) => setProxyLine(e.target.value)}
                  placeholder="http://user:pass@ip:port"
                />
                <button className="btn" type="button" onClick={addProxy} disabled={proxyBusy || !proxyLine.trim()}>
                  Добавить
                </button>
                <button
                  className="btn"
                  type="button"
                  onClick={assignProxies}
                  disabled={proxyBusy || !(proxyList?.total ?? 0)}
                >
                  Закрепить за профилями
                </button>
              </div>
              <div className="input-row">
                <button
                  className="btn danger"
                  type="button"
                  onClick={rebalanceProxies}
                  disabled={proxyBusy || !(proxyList?.total ?? 0)}
                >
                  Перераспределить все
                </button>
              </div>
              <span className="field-hint">
                «Закрепить» добавляет привязки только новым профилям, старые не трогает.
                «Перераспределить все» заново раскладывает все активные профили по всему пулу —
                у них сменится IP.
              </span>
            </div>
            <div className="field">
              <span className="field-label">Проверка прокси на Ozon</span>
              <div className="input-row">
                <button
                  className="btn"
                  type="button"
                  onClick={startProxyCheck}
                  disabled={Boolean(proxyCheck?.running)}
                >
                  {proxyCheck?.running ? "Проверяем…" : "Проверить прокси"}
                </button>
                {proxyCheck?.running && (
                  <button className="btn danger" type="button" onClick={stopProxyCheck}>
                    Стоп
                  </button>
                )}
                {!proxyCheck?.running && badProxies.length > 0 && (
                  <button className="btn danger" type="button" onClick={() => pruneProxies(true)}>
                    Убрать нерабочие ({badProxies.length})
                  </button>
                )}
              </div>
              <span className="field-hint">
                Открывает выдачу Ozon через каждый прокси и отмечает блок-страницу «Похоже, нет
                соединения». Идёт в фоне, прогон должен быть остановлен.
              </span>
              {proxyCheck && proxyCheck.total > 0 && (
                <div className="hint-block">
                  {proxyCheck.running ? "Идёт проверка" : "Проверено"}: {proxyCheck.checked}/
                  {proxyCheck.total} · ок {proxyCheck.ok} · блок {proxyCheck.blocked} · капча{" "}
                  {proxyCheck.challenge} · ошибок {proxyCheck.failed}
                  {proxyCheck.current ? ` · ${proxyCheck.current}` : ""}
                  {proxyCheck.error ? ` · ${proxyCheck.error}` : ""}
                </div>
              )}
              {problemProxies.length > 0 && (
                <div className="hint-block">
                  {problemProxies.slice(0, 12).map((item) => (
                    <div key={item.server}>
                      {item.masked} — {statusText[item.status]}
                      {item.http ? ` (HTTP ${item.http})` : ""}
                      {item.error ? `: ${item.error}` : ""}
                    </div>
                  ))}
                  {problemProxies.length > 12 ? <div>…и ещё {problemProxies.length - 12}</div> : null}
                </div>
              )}
            </div>
            <div className="field">
              <span className="field-label">Файл Excel (.xlsx)</span>
              <div className="input-row">
                <input
                  value={settings.general.sheet_xlsx}
                  onChange={(e) => setGeneral("sheet_xlsx", e.target.value)}
                  placeholder="C:\Users\…\table_ozon.xlsx"
                />
                <button
                  className="btn"
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    void pickExcel();
                  }}
                >
                  Выбрать…
                </button>
              </div>
              <span className="field-hint">локальная таблица, если Google не настроен</span>
            </div>
          </fieldset>

          <fieldset className="panel">
            <legend>Директории профилей</legend>
            <Field label="Активные профили">
              <input value={settings.general.dir_active} onChange={(e) => setGeneral("dir_active", e.target.value)} />
            </Field>
            <Field label="Отработанные профили">
              <input value={settings.general.dir_done} onChange={(e) => setGeneral("dir_done", e.target.value)} />
            </Field>
            <Field label="Без авторизации («безакк»)">
              <input value={settings.general.dir_noauth} onChange={(e) => setGeneral("dir_noauth", e.target.value)} />
            </Field>

            <div className="field">
              <span className="field-label">Сейчас в «Активных»</span>
              <div className="hint-block">
                {profileStats
                  ? `${profileStats.active} профилей · новых ${profileStats.new} · занято ${profileStats.busy} · ` +
                    `отработано ${profileStats.done} · безакк ${profileStats.noauth}`
                  : "…"}
              </div>
            </div>
            <div className="field">
              <span className="field-label">Генератор профилей</span>
              <div className="input-row">
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={genCount}
                  onChange={(e) => setGenCount(Number(e.target.value))}
                />
                <button className="btn" type="button" onClick={generateProfiles} disabled={genBusy}>
                  {genBusy ? "Создаём…" : "Создать"}
                </button>
              </div>
              <span className="field-hint">
                Создаёт пустые папки в «Активные профили» и выдаёт каждой свой отпечаток. Перед
                созданием сохраните пути выше.
              </span>
            </div>
            <div className="field">
              <span className="field-label">Прогрев новых профилей</span>
              <div className="input-row">
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={warmCount}
                  onChange={(e) => setWarmCount(Number(e.target.value))}
                />
                <button
                  className="btn"
                  type="button"
                  onClick={startWarmup}
                  disabled={Boolean(warmup?.running) || profileStats?.new === 0}
                >
                  Прогреть
                </button>
                {warmup?.running && (
                  <button className="btn danger" type="button" onClick={stopWarmup}>
                    Стоп
                  </button>
                )}
              </div>
              <span className="field-hint">
                По очереди открывает новые профили на Ozon (главная + поиск), чтобы появились
                куки. Каждый профиль идёт через свой закреплённый прокси. Идёт только при
                остановленном прогоне, примерно 20–40 сек на профиль.
              </span>
              {warmup && (warmup.running || warmup.done + warmup.failed > 0) && (
                <div className="hint-block">
                  {warmup.running ? "Идёт прогрев" : "Прогрев завершён"}:{" "}
                  {warmup.done + warmup.failed}/{warmup.total}
                  {warmup.current ? ` · ${warmup.current}` : ""}
                  {warmup.failed ? ` · ошибок ${warmup.failed}` : ""}
                  {warmup.error ? ` · ${warmup.error}` : ""}
                </div>
              )}
            </div>
            <div className="field">
              <span className="field-label">Профили и закреплённые прокси</span>
              <div className="table-wrap" style={{ maxHeight: 280 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Профиль</th>
                      <th>Состояние</th>
                      <th>Прокси</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profileRows.map((row) => (
                      <tr key={row.name}>
                        <td>{row.name}</td>
                        <td>{row.new ? "новый" : "прогрет"}</td>
                        <td>
                          {row.proxy
                            ? `${row.proxy} · ${proxyStatusLabel(row.proxy_status)}`
                            : row.proxy_missing
                              ? "прокси удалён из пула"
                              : "не закреплён"}
                        </td>
                      </tr>
                    ))}
                    {!profileRows.length && (
                      <tr>
                        <td colSpan={3} className="muted">
                          Профилей пока нет
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <legend className="mt">Параметры запуска</legend>
            <div className="row3">
              <Field label="Потоков">
                <input
                  type="number"
                  min={1}
                  max={64}
                  value={settings.general.threads}
                  onChange={(e) => setGeneral("threads", Number(e.target.value))}
                />
              </Field>
              <Field label="Лимит повторений" hint="0 = без лимита">
                <input
                  type="number"
                  min={0}
                  value={settings.general.repeat_limit}
                  onChange={(e) => setGeneral("repeat_limit", Number(e.target.value))}
                />
              </Field>
              <Field label="Лимит ошибок" hint="стоп после N подряд">
                <input
                  type="number"
                  min={0}
                  value={settings.general.error_limit}
                  onChange={(e) => setGeneral("error_limit", Number(e.target.value))}
                />
              </Field>
            </div>
          </fieldset>

          <fieldset className="panel">
            <legend>Синхронизация с таблицей</legend>
            <div className="hint-block">
              Режим: {sync?.mode ?? "—"} · push {sync?.last_push ?? "—"} · из листа {sync?.pulled ?? 0},
              в лист {sync?.pushed ?? 0}
              {sync?.last_error ? ` · ошибка: ${sync.last_error}` : ""}
            </div>
            <Toggle
              label="Автосинхронизация"
              value={settings.general.sync_enabled}
              onChange={(v) => setGeneral("sync_enabled", v)}
            />
            <Field label="Интервал, сек" hint="10–600">
              <input
                type="number"
                min={10}
                max={600}
                value={settings.general.sync_interval_sec}
                onChange={(e) => setGeneral("sync_interval_sec", Number(e.target.value))}
              />
            </Field>
            <button className="btn" onClick={onSync} type="button">
              Синхронизировать сейчас
            </button>
            <button className="btn danger" onClick={resetDemo} type="button">
              Сбросить тестовые данные
            </button>
            <div className="field-hint">
              Без Google-кредов работает тестовая таблица: %APPDATA%\pf-automation\fake_sheet.json
              (листы ПФ2 / Аккаунты / Выкупы).
            </div>
          </fieldset>

          <fieldset className="panel">
            <legend>Быстрые действия из выдачи / каталога</legend>
            <Toggle
              label="Добавлять в корзину из поиска"
              value={settings.general.quick_actions.cart_from_search}
              onChange={(v) => setQuick("cart_from_search", v)}
            />
            <Toggle
              label="Переходить в корзину"
              value={settings.general.quick_actions.goto_cart}
              onChange={(v) => setQuick("goto_cart", v)}
            />
            <Toggle
              label="Просматривать фото в выдаче"
              value={settings.general.quick_actions.view_photos}
              onChange={(v) => setQuick("view_photos", v)}
            />
            <Toggle
              label="Добавлять в избранное"
              value={settings.general.quick_actions.favorite}
              onChange={(v) => setQuick("favorite", v)}
            />
          </fieldset>

          <fieldset className="panel">
            <legend>Браузер</legend>
            <Field label="Провайдер">
              <select
                value={settings.browser.provider}
                onChange={(e) => setBrowser("provider", e.target.value as Settings["browser"]["provider"])}
              >
                <option value="vanilla">Playwright Chromium (отладка)</option>
                <option value="camoufox">Camoufox (open-source антидетект)</option>
                <option value="cdp">Antidetect по CDP (Dolphin/AdsPower/Octo)</option>
              </select>
            </Field>
            <Field label="CDP endpoint" hint="для провайдера cdp">
              <input value={settings.browser.cdp_endpoint} onChange={(e) => setBrowser("cdp_endpoint", e.target.value)} />
            </Field>
            <Field
              label="Движок браузера"
              hint="auto — установленный Chrome, иначе Chromium (меньше отпечатков)"
            >
              <select
                value={settings.browser.channel}
                onChange={(e) => setBrowser("channel", e.target.value as Settings["browser"]["channel"])}
              >
                <option value="auto">Авто (Chrome, затем Edge)</option>
                <option value="chrome">Google Chrome</option>
                <option value="edge">Microsoft Edge</option>
                <option value="chromium">Chromium из Playwright</option>
              </select>
            </Field>
            <div className="row3">
              <Field label="Локаль">
                <input value={settings.browser.locale} onChange={(e) => setBrowser("locale", e.target.value)} />
              </Field>
              <Field label="Часовой пояс" hint="пусто — системный (как у ОС и IP)">
                <input
                  value={settings.browser.timezone}
                  placeholder="системный"
                  onChange={(e) => setBrowser("timezone", e.target.value)}
                />
              </Field>
              <Field label="Headless">
                <select
                  value={settings.browser.headless ? "yes" : "no"}
                  onChange={(e) => setBrowser("headless", e.target.value === "yes")}
                >
                  <option value="no">Нет</option>
                  <option value="yes">Да</option>
                </select>
              </Field>
            </div>
            <Toggle
              label="Открывать браузер в потоках"
              value={settings.browser.enabled}
              onChange={(v) => setBrowser("enabled", v)}
            />
            <Field label="Проверочная страница" hint="M2: сюда ходит поток при включённом браузере">
              <input
                value={settings.browser.check_url}
                onChange={(e) => setBrowser("check_url", e.target.value)}
              />
            </Field>
            <button className="btn" onClick={testBrowser} type="button">
              Проверить браузер
            </button>
          </fieldset>
        </div>
      ) : (
        <div className="grid">
          <fieldset className="panel">
            <legend>Вероятности действий, %</legend>
            <Percent
              label="Использовать готовый профиль"
              hint="100% — всегда прогретый профиль; 0% — всегда новый"
              value={settings.view.profile_use_pct}
              onChange={(v) => setView("profile_use_pct", v)}
            />
            <Percent label="Лайк товара" value={settings.view.like_pct} onChange={(v) => setView("like_pct", v)} />
            <Percent
              label="Добавление в корзину"
              value={settings.view.cart_pct}
              onChange={(v) => setView("cart_pct", v)}
            />
            <Percent
              label="«Предложения других продавцов»"
              value={settings.view.other_sellers_pct}
              onChange={(v) => setView("other_sellers_pct", v)}
            />
            <Percent
              label="«Смотрите также»"
              value={settings.view.see_also_pct}
              onChange={(v) => setView("see_also_pct", v)}
            />
            <Percent
              label="Добавить в сравнение"
              value={settings.view.compare_pct}
              onChange={(v) => setView("compare_pct", v)}
            />
            <Percent
              label="Лайк отзыва"
              value={settings.view.review_like_pct}
              onChange={(v) => setView("review_like_pct", v)}
            />
          </fieldset>

          <fieldset className="panel">
            <legend>Глубина и тайминги</legend>
            <Field label="Максимум страниц поиска">
              <input
                type="number"
                min={1}
                max={20}
                value={settings.view.max_search_pages}
                onChange={(e) => setView("max_search_pages", Number(e.target.value))}
              />
            </Field>

            <Range
              label="Время на карточке товара, сек"
              value={settings.view.card_time_sec}
              onChange={(v) => setView("card_time_sec", v)}
            />
            <Range
              label="Время в отзывах, сек"
              value={settings.view.reviews_time_sec}
              onChange={(v) => setView("reviews_time_sec", v)}
            />

            <div className="hint-block">
              Сценарий M0 использует ускоренные тайминги, чтобы проверять очередь и отчётность. Реальные
              значения подключаются вместе с браузерным слоем (M2) и не хардкодятся в коде.
            </div>
          </fieldset>
        </div>
      )}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

function Percent({ label, hint, value, onChange }: { label: string; hint?: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className="percent-row">
      <div>
        <div className="field-label">{label}</div>
        {hint && <div className="field-hint">{hint}</div>}
      </div>
      <div className="percent-controls">
        <input type="range" min={0} max={100} value={value} onChange={(e) => onChange(Number(e.target.value))} />
        <input
          className="percent-input"
          type="number"
          min={0}
          max={100}
          value={value}
          onChange={(e) => onChange(Math.max(0, Math.min(100, Number(e.target.value))))}
        />
        <span className="pct">%</span>
      </div>
    </div>
  );
}

function Range({ label, value, onChange }: { label: string; value: [number, number]; onChange: (v: [number, number]) => void }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <div className="range-row">
        <input
          type="number"
          min={1}
          value={value[0]}
          onChange={(e) => onChange([Number(e.target.value), value[1]])}
        />
        <span className="muted">—</span>
        <input
          type="number"
          min={1}
          value={value[1]}
          onChange={(e) => onChange([value[0], Number(e.target.value)])}
        />
      </div>
    </div>
  );
}

function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="toggle-row">
      <span>{label}</span>
      <select value={value ? "yes" : "no"} onChange={(e) => onChange(e.target.value === "yes")}>
        <option value="yes">Да</option>
        <option value="no">Нет</option>
      </select>
    </div>
  );
}
