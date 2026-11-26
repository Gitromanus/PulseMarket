import { useCallback, useEffect, useRef, useState } from "react";
import { ApiClient } from "./api";
import type { AppInfo, LogEvent, RunRow, Settings, Stats, SyncState, ThreadState } from "./types";
import WorkerPage from "./pages/WorkerPage";
import SettingsPage from "./pages/SettingsPage";
import ManualPage from "./pages/ManualPage";
import LicensePage from "./pages/LicensePage";

const MAX_LOGS = 2000;

export default function App() {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [fatal, setFatal] = useState("");
  const [tab, setTab] = useState<"worker" | "settings" | "manual" | "license">("worker");
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
          const message = JSON.parse(event.data) as {
            type: string;
            payload: unknown;
          };
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
        // Ядро могло перезапуститься с новым портом/токеном — перечитываем info.
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
      ws.onopen = () => {
        // После рестарта ядра состояние могло устареть — синхронизируемся.
        void refresh();
      };
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
  const syncNow = () => call(() => apiRef.current!.post("/api/sheets/sync"));
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

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">PM</span>
          <div>
            <div className="brand-title">PULSEMARKET</div>
            <div className="brand-sub">Ozon · поведенческие факторы</div>
          </div>
        </div>

        <nav className="nav">
          <button className={tab === "worker" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("worker")}>
            Рабочее окно
          </button>
          <button className={tab === "manual" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("manual")}>
            Ручной режим
          </button>
          <button className={tab === "settings" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("settings")}>
            Настройки
          </button>
          <button className={tab === "license" ? "nav-btn active" : "nav-btn"} onClick={() => setTab("license")}>
            Лицензия
          </button>
        </nav>

        <div className="head-metrics">
          <span className={`state-pill state-${stats?.run_state ?? "idle"}`}>
            {stats?.run_state === "running" ? "В работе" : stats?.run_state === "stopping" ? "Остановка" : "Остановлен"}
          </span>
          <span className="head-metric">
            Потоки <b>{stats?.threads_active ?? 0}</b>
          </span>
          <span className="head-metric">
            Очередь <b>{stats?.pending ?? 0}</b>
          </span>
        </div>
      </header>

      {notice && <div className="notice">{notice}</div>}

      <main>
        {tab === "worker" ? (
          <WorkerPage
            api={apiRef.current!}
            stats={stats}
            sync={sync}
            threads={threads}
            logs={logs}
            runs={runs}
            running={Boolean(running)}
            onStart={() => call(() => apiRef.current!.post("/api/run/start", { threads: settings.general.threads }))}
            onStop={() => call(() => apiRef.current!.post("/api/run/stop"))}
            onRestart={() => call(() => apiRef.current!.post("/api/run/restart"))}
            onSeed={() => call(() => apiRef.current!.post("/api/dev/seed"))}
            onSync={syncNow}
            onRequeue={() => call(() => apiRef.current!.post("/api/run/requeue"))}
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
            sync={sync}
            onChange={setSettings}
            onNotice={showNotice}
            onSync={syncNow}
          />
        )}
      </main>
    </div>
  );
}
