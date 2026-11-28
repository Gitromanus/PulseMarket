import { useCallback, useEffect, useRef, useState } from "react";
import { ApiClient } from "./api";
import type { AppInfo, LogEvent, RunRow, Settings, Stats, SyncState, ThreadState } from "./types";
import WorkerPage from "./pages/WorkerPage";
import SettingsPage from "./pages/SettingsPage";
import ManualPage from "./pages/ManualPage";
import LicensePage from "./pages/LicensePage";
import TasksPage from "./pages/TasksPage";

const MAX_LOGS = 2000;

type Tab = "worker" | "tasks" | "settings" | "manual" | "license";

const TITLES: Record<Tab, string> = {
  worker: "Рабочее окно",
  tasks: "Задания",
  manual: "Ручной режим",
  settings: "Настройки",
  license: "Лицензия",
};

const ICONS: Record<Tab, JSX.Element> = {
  worker: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <rect x="3" y="3" width="7" height="7" rx="2" />
      <rect x="14" y="3" width="7" height="7" rx="2" />
      <rect x="3" y="14" width="7" height="7" rx="2" />
      <rect x="14" y="14" width="7" height="7" rx="2" />
    </svg>
  ),
  tasks: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <path d="M4 7h16M4 12h16M4 17h10" />
    </svg>
  ),
  manual: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <path d="M4 12.5 8.5 17 20 5.5" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <path d="M4 8h16M4 16h16" />
      <circle cx="9" cy="8" r="2.2" fill="currentColor" stroke="none" />
      <circle cx="16" cy="16" r="2.2" fill="currentColor" stroke="none" />
    </svg>
  ),
  license: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <circle cx="12" cy="12" r="9" />
      <circle cx="9" cy="9.5" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="15" cy="9.5" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="12" cy="15" r="1.3" fill="currentColor" stroke="none" />
    </svg>
  ),
};

export default function App() {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [fatal, setFatal] = useState("");
  const [tab, setTab] = useState<Tab>("worker");
  const [logs, setLogs] = useState<LogEvent[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [threads, setThreads] = useState<ThreadState[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [notice, setNotice] = useState("");
  const apiRef = useRef<ApiClient | null>(null);
  const noticeTimer = useRef<number | undefined>(undefined);

  const showNotice = useCallback((text: string) => {
    setNotice(text);
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(""), 6000);
  }, []);

  const refresh = useCallback(async () => {
    const api = apiRef.current;
    if (!api) return;
    try {
      const [nextStats, state, nextRuns, nextSync] = await Promise.all([
        api.get<Stats>("/api/stats"),
        api.get<{ state: string; threads: ThreadState[] }>("/api/run/state"),
        api.get<{ items: RunRow[] }>("/api/runs?limit=200"),
        api.get<SyncState>("/api/sheets/status"),
      ]);
      setStats(nextStats);
      setThreads(state.threads);
      setRuns(nextRuns.items);
      setSync(nextSync);
    } catch (error) {
      showNotice(`Нет связи с ядром: ${String(error)}`);
    }
  }, [showNotice]);

  useEffect(() => {
    let alive = true;
    window.pf
      .info()
      .then(async (value) => {
        if (!alive) return;
        setInfo(value);
        const api = new ApiClient(value.apiBase, value.token);
        apiRef.current = api;
        setSettings(await api.get<Settings>("/api/settings"));
        await refresh();
      })
      .catch((error) => setFatal(String(error)));
    return () => {
      alive = false;
    };
  }, [refresh]);

  useEffect(() => {
    if (!info) return;
    let closed = false;
    let retry: number | undefined;

    const connect = () => {
      const ws = new WebSocket(info.wsUrl);
      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as { type: string; payload: unknown };
          if (message.type === "history") {
            const items = (message.payload as { type: string; payload: LogEvent }[])
              .filter((e) => e.type === "log")
              .map((e) => e.payload);
            setLogs(items.slice(-MAX_LOGS));
          } else if (message.type === "log") {
            setLogs((prev) => [...prev.slice(-MAX_LOGS + 1), message.payload as LogEvent]);
          } else if (message.type === "thread_state") {
            const thread = message.payload as ThreadState;
            setThreads((prev) => {
              const next = prev.filter((t) => t.id !== thread.id);
              return [...next, thread].sort((a, b) => a.id - b.id);
            });
          } else if (message.type === "run_state" || message.type === "task_update") {
            void refresh();
          } else if (message.type === "sync_state") {
            setSync(message.payload as SyncState);
          } else if (message.type === "log_cleared") {
            setLogs([]);
          }
        } catch {
          /* некорректный кадр игнорируем */
        }
      };
      ws.onclose = () => {
        if (closed) return;
        window.pf
          .info()
          .then((fresh) => {
            if (closed) return;
            if (
              fresh.token !== info.token ||
              fresh.apiBase !== info.apiBase ||
              fresh.wsUrl !== info.wsUrl
            ) {
              apiRef.current = new ApiClient(fresh.apiBase, fresh.token);
              setInfo(fresh);
            } else {
              retry = window.setTimeout(connect, 1500);
            }
          })
          .catch(() => {
            if (!closed) retry = window.setTimeout(connect, 1500);
          });
      };
      ws.onopen = () => void refresh();
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
    };
  }, [info, refresh]);

  useEffect(() => {
    if (!info) return;
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [info, refresh]);

  const lastSyncError = useRef<string | null>(null);
  useEffect(() => {
    const error = sync?.last_error ?? null;
    if (error && error !== lastSyncError.current) {
      showNotice(`Синхронизация: ${error}`);
      lastSyncError.current = error;
    }
    if (!error) lastSyncError.current = null;
  }, [sync?.last_error, showNotice]);

  const call = async (work: () => Promise<{ ok?: boolean; error?: string }>) => {
    try {
      const result = await work();
      if (result?.ok === false) showNotice(result.error ?? "Операция не выполнена");
      await refresh();
    } catch (error) {
      showNotice(String(error).replace("Error: ", ""));
    }
  };

  const running = stats?.run_state === "running" || stats?.run_state === "stopping";
  const clearLogs = async () => {
    try {
      await apiRef.current!.post("/api/logs/clear");
      setLogs([]);
    } catch (error) {
      showNotice(String(error).replace("Error: ", ""));
    }
  };

  if (fatal) {
    return (
      <div className="boot-error">
        <h2>Ядро не запустилось</h2>
        <p>{fatal}</p>
        <p className="muted">Проверьте, что backend/.venv существует и установлены зависимости.</p>
      </div>
    );
  }

  if (!info || !settings) {
    return <div className="boot">Подключение к ядру…</div>;
  }

  const stateLabel =
    stats?.run_state === "running"
      ? "В работе"
      : stats?.run_state === "stopping"
        ? "Остановка"
        : "Остановлен";
  const percent = stats?.progress_percent ?? 0;

  return (
    <div className="app app--shell">
      <div className="bg" />
      <div className="grain" />

      <aside className="sidebar">
        <div className="brand">
          <div className="brand__mark">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3 3 8.5 12 14l9-5.5L12 3Z" />
              <path d="M3 15.5 12 21l9-5.5" />
            </svg>
          </div>
          <div>
            <div className="brand__name">PULSEMARKET</div>
            <div className="brand__sub">Ozon · поведение</div>
          </div>
        </div>

        <nav className="nav nav--side">
          <div className="nav__cap">Работа</div>
          {(["worker", "tasks", "manual"] as Tab[]).map((key) => (
            <button
              key={key}
              className={tab === key ? "nav-btn active" : "nav-btn"}
              onClick={() => setTab(key)}
            >
              {ICONS[key]}
              <span>{TITLES[key]}</span>
            </button>
          ))}
          <div className="nav__cap">Система</div>
          {(["settings", "license"] as Tab[]).map((key) => (
            <button
              key={key}
              className={tab === key ? "nav-btn active" : "nav-btn"}
              onClick={() => setTab(key)}
            >
              {ICONS[key]}
              <span>{TITLES[key]}</span>
            </button>
          ))}
        </nav>

        <div className="side__card">
          <b>{stateLabel}</b>
          <span>
            Потоки <b>{stats?.threads_active ?? 0}</b> · очередь <b>{stats?.pending ?? 0}</b>
          </span>
          <div className="side__progress">
            <i style={{ width: `${percent}%` }} />
          </div>
          <div className="side__pct">
            {stats?.runs_target
              ? `${stats.runs_done ?? 0} / ${stats.runs_target} прогонов · ${percent}%`
              : "очередь пуста"}
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="crumbs">
            <span>PulseMarket</span>
            <span className="sep">/</span>
            <b>{TITLES[tab]}</b>
          </div>
          <div className="topbar__sp" />
          <span className={`state-pill state-${stats?.run_state ?? "idle"}`}>{stateLabel}</span>
          <span className="head-metric">
            Потоки <b>{stats?.threads_active ?? 0}</b>
          </span>
          <span className="head-metric">
            Очередь <b>{stats?.pending ?? 0}</b>
          </span>
          <div className="top-progress" title={`${stats?.runs_done ?? 0} из ${stats?.runs_target ?? 0} прогонов`}>
            <div className="top-progress-fill" style={{ width: `${percent}%` }} />
          </div>
          <span className="head-metric">{percent}%</span>
        </header>

        {notice && <div className="notice">{notice}</div>}

        <main className={tab === "worker" ? "content content--full" : "content"}>
          {tab === "worker" ? (
            <WorkerPage
              api={apiRef.current!}
              stats={stats}
              threads={threads}
              logs={logs}
              runs={runs}
              running={Boolean(running)}
              onStart={() => call(() => apiRef.current!.post("/api/run/start", { threads: settings.general.threads }))}
              onStop={() => call(() => apiRef.current!.post("/api/run/stop"))}
              onOpenTasks={() => setTab("tasks")}
              onNotice={showNotice}
              onClearLogs={clearLogs}
              onToggleBrowser={(thread) =>
                call(() =>
                  apiRef.current!.post(`/api/threads/${thread.id}/browser`, {
                    visible: !thread.browser_visible,
                  }),
                )
              }
              onShowBrowser={(thread) =>
                call(() =>
                  apiRef.current!.post(`/api/threads/${thread.id}/browser`, { visible: true }),
                )
              }
            />
          ) : tab === "tasks" ? (
            <TasksPage
              api={apiRef.current!}
              onNotice={showNotice}
              onChanged={() => void refresh()}
            />
          ) : tab === "manual" ? (
            <ManualPage api={apiRef.current!} onNotice={showNotice} />
          ) : tab === "license" ? (
            <LicensePage
              api={apiRef.current!}
              settings={settings}
              onChange={setSettings}
              onNotice={showNotice}
            />
          ) : (
            <SettingsPage
              api={apiRef.current!}
              settings={settings}
              onChange={setSettings}
              onNotice={showNotice}
            />
          )}
        </main>
      </div>
    </div>
  );
}
