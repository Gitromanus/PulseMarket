import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api";
import type { LogEvent, RunRow, Stats, ThreadState } from "../types";
import { Modal, ReportPanel, SchedulerPanel } from "../components/Dialogs";

type Props = {
  api: ApiClient;
  stats: Stats | null;
  threads: ThreadState[];
  logs: LogEvent[];
  runs: RunRow[];
  running: boolean;
  onStart: () => void;
  onStop: () => void;
  onOpenTasks: () => void;
  onNotice: (text: string) => void;
  onToggleBrowser: (thread: ThreadState) => void;
  onShowBrowser: (thread: ThreadState) => void;
  onClearLogs: () => void;
};

export default function WorkerPage(props: Props) {
  const { stats, threads, logs, runs, running } = props;
  const [sub, setSub] = useState<"log" | "results">("log");
  const [dialog, setDialog] = useState<"scheduler" | "report" | null>(null);

  return (
    <div className="worker">
      <div className="toolbar">
        <button className="btn primary" onClick={props.onStart} disabled={running}>
          ▶ Старт
        </button>
        <button className="btn danger" onClick={props.onStop} disabled={!running}>
          ■ Остановить
        </button>
        <button className="btn ghost" onClick={props.onOpenTasks}>
          Очередь: {stats?.pending ?? 0}
        </button>
        <div className="spacer" />
        <button id="btn-scheduler" className="btn" onClick={() => setDialog("scheduler")}>
          Планировщик
        </button>
        <button id="btn-report" className="btn" onClick={() => setDialog("report")}>
          Отчёт о ресурсах
        </button>
        <div className="toolbar-note">
          {stats?.runs_target
            ? `Прогресс: ${stats.runs_done ?? 0} / ${stats.runs_target} прогонов · ` +
              `${stats.progress_percent ?? 0}%`
            : "Прогресс: очередь пуста"}
        </div>
      </div>

      <div className="metrics">
        <Metric label="Активные потоки" value={stats?.threads_active ?? 0} tone="blue" />
        <Metric label="В очереди" value={stats?.pending ?? 0} tone="amber" />
        <Metric label="Успешно" value={stats?.ok ?? 0} tone="green" />
        <Metric label="Ошибок" value={stats?.errors ?? 0} tone="red" />
        {(stats?.paused ?? 0) > 0 && (
          <Metric label="Приостановлено" value={stats?.paused ?? 0} tone="gray" />
        )}
        <Metric label="Всего заданий" value={stats?.total ?? 0} tone="gray" />
      </div>

      <section className="panel">
        <div className="panel-head">
          <h3>Потоки</h3>
          <span className="muted">{threads.length ? `${threads.length} шт.` : "не запущены"}</span>
        </div>
        {threads.length === 0 ? (
          <div className="empty">
            Нажмите «Старт», чтобы поднять потоки. Если очередь пуста — добавьте задания
            на вкладке «Задания».
          </div>
        ) : (
          <div className="threads">
            {threads.map((thread) => (
              <div className="thread-card" key={thread.id}>
                <div className="thread-head">
                  <span className="thread-title">Поток №{thread.id}</span>
                  <span className={`dot dot-${thread.status}`} />
                </div>
                <div className="thread-main">
                  <div className="thread-body">
                    <div className="thread-row">
                      <span>Запрос</span>
                      <b title={thread.query ?? thread.sku ?? ""}>
                        {thread.query || thread.sku || "—"}
                      </b>
                    </div>
                    {thread.profile && (
                      <div className="thread-row">
                        <span>Профиль</span>
                        <b title={thread.profile}>{thread.profile}</b>
                      </div>
                    )}
                    {thread.proxy_ip && (
                      <div className="thread-row">
                        <span>Внешний IP</span>
                        <b title={thread.proxy_ip}>{thread.proxy_ip}</b>
                      </div>
                    )}
                  </div>
                  <BrowserPreview
                    api={props.api}
                    thread={thread}
                    onOpen={props.onShowBrowser}
                    onHide={props.onToggleBrowser}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <div className="subtabs">
        <button className={sub === "log" ? "subtab active" : "subtab"} onClick={() => setSub("log")}>
          Лог <span className="count">{logs.length}</span>
        </button>
        <button className={sub === "results" ? "subtab active" : "subtab"} onClick={() => setSub("results")}>
          Результаты <span className="count">{runs.length}</span>
        </button>
        <button
          className="btn small ghost"
          style={{ marginLeft: "auto" }}
          onClick={props.onClearLogs}
          disabled={logs.length === 0}
          title="Очистить историю логов (журнал ядра тоже)"
        >
          Очистить логи
        </button>
      </div>

      {sub === "log" ? (
        <LogView logs={logs} />
      ) : (
        <div>
          <div className="subtabs results-toolbar">
            <span className="muted">Результаты прогонов: {runs.length}</span>
          </div>
          <ResultsTable runs={runs} />
        </div>
      )}

      {dialog && (
        <Modal
          title={dialog === "scheduler" ? "Планировщик" : "Отчёт о ресурсах"}
          onClose={() => setDialog(null)}
        >
          {dialog === "scheduler" ? (
            <SchedulerPanel api={props.api} onNotice={props.onNotice} />
          ) : (
            <ReportPanel api={props.api} onNotice={props.onNotice} />
          )}
        </Modal>
      )}
    </div>
  );
}

function BrowserPreview({
  api,
  thread,
  onOpen,
  onHide,
}: {
  api: ApiClient;
  thread: ThreadState;
  onOpen: (thread: ThreadState) => void;
  onHide: (thread: ThreadState) => void;
}) {
  const [tick, setTick] = useState(0);
  const [failed, setFailed] = useState(false);
  const active = Boolean(thread.hwnd) && thread.status === "running";
  // Скриншот заставляет Chrome перерисовываться, поэтому снимаем кадр только
  // когда окно спрятано. Открытое окно оператор и так видит.
  const polling = active && !thread.browser_visible;

  useEffect(() => {
    setFailed(false);
  }, [tick]);

  useEffect(() => {
    if (!polling) return;
    // 700 мс: курсор в превью успевает «двигаться», а не прыгать раз в 2.5 с.
    const timer = window.setInterval(() => setTick((value) => value + 1), 700);
    return () => window.clearInterval(timer);
  }, [polling]);

  if (!active) {
    return <div className="browser-preview browser-preview-empty">Браузер не запущен</div>;
  }

  if (thread.browser_visible) {
    return (
      <div
        className="browser-preview"
        onClick={() => onOpen(thread)}
        title="Показать браузер на переднем плане"
      >
        <div className="browser-preview-empty">Браузер открыт</div>
        <div className="browser-preview-bar">
          <span>Показать на переднем плане</span>
          <button
            className="preview-hide"
            onClick={(event) => {
              event.stopPropagation();
              onHide(thread);
            }}
          >
            Скрыть
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="browser-preview"
      onClick={() => onOpen(thread)}
      title="Открыть полноценный браузер"
    >
      {failed ? (
        <div className="browser-preview-empty">Ожидание браузера…</div>
      ) : (
        <img
          src={api.previewUrl(thread.id, tick)}
          alt={`Поток ${thread.id}`}
          draggable={false}
          onError={() => setFailed(true)}
        />
      )}
      <div className="browser-preview-bar">
        <span>Открыть браузер</span>
        {thread.browser_visible && (
          <button
            className="preview-hide"
            onClick={(event) => {
              event.stopPropagation();
              onHide(thread);
            }}
          >
            Скрыть
          </button>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`metric metric-${tone}`}>
      <div className="metric-value">{value}</div>
      <div className="metric-label">{label}</div>
    </div>
  );
}

const LOG_ROW = 18;

function LogView({ logs }: { logs: LogEvent[] }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(320);

  useEffect(() => {
    const element = boxRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setHeight(element.clientHeight));
    observer.observe(element);
    setHeight(element.clientHeight);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (stick && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [logs, stick]);

  // Окно рендера: в DOM живут только строки в видимой области + запас,
  // поэтому лог на 10k записей не тормозит.
  const start = Math.max(0, Math.floor(scrollTop / LOG_ROW) - 12);
  const end = Math.min(logs.length, start + Math.ceil(height / LOG_ROW) + 24);

  return (
    <div
      className="log"
      ref={boxRef}
      onScroll={(event) => {
        const element = event.currentTarget;
        setScrollTop(element.scrollTop);
        setStick(element.scrollHeight - element.scrollTop - element.clientHeight < 40);
      }}
    >
      {logs.length === 0 && <div className="empty">Лог пуст</div>}
      <div style={{ height: logs.length * LOG_ROW, position: "relative" }}>
        <div style={{ position: "absolute", top: start * LOG_ROW, left: 0, right: 0 }}>
          {logs.slice(start, end).map((entry, index) => (
            <div
              className={`log-row level-${entry.level}`}
              key={start + index}
              title={entry.msg}
              style={{ height: LOG_ROW }}
            >
              <span className="log-ts">[{entry.ts}]</span>
              <span className="log-thread">{entry.thread ? `Поток №${entry.thread}` : "Ядро"} :</span>
              <span className="log-msg">{entry.msg}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ResultsTable({ runs }: { runs: RunRow[] }) {
  if (runs.length === 0) return <div className="panel empty">Пока нет выполнений</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Время</th>
            <th>Поток</th>
            <th>SKU</th>
            <th>Запрос</th>
            <th>Позиция</th>
            <th>Корзина</th>
            <th>Лайк</th>
            <th>Итог</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td className="muted">{run.id}</td>
              <td>{run.started_at?.replace("T", " ")}</td>
              <td>{run.thread_id}</td>
              <td>{run.sku ?? "—"}</td>
              <td className="ellipsis" title={run.query ?? ""}>
                {run.query ?? "—"}
              </td>
              <td>{run.position ?? "—"}</td>
              <td>{run.cart_added ? "да" : "—"}</td>
              <td>{run.liked ? "да" : "—"}</td>
              <td>
                <span className={run.result === "ok" ? "tag ok" : "tag err"}>
                  {run.result === "ok" ? "успех" : "ошибка"}
                </span>
                {run.error_msg && (
                  <span className="err-text" title={run.error_msg}>
                    {run.error_msg}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
