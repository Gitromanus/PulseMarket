import { Fragment, useCallback, useEffect, useState, type MouseEvent } from "react";
import type { ApiClient } from "../api";

type TaskRow = {
  id: number;
  query: string;
  sku: string;
  url: string | null;
  geo: string | null;
  target_runs: number;
  done_runs: number;
  status: string;
  position: number | null;
  cart_adds: number;
  likes: number;
  priority: number;
  last_result: string | null;
  last_position: number | null;
  last_cart: number | null;
  last_liked: number | null;
  last_finished: string | null;
  last_error: string | null;
  last_error_msg: string | null;
  ok_runs: number;
  error_runs: number;
};

type TasksResponse = {
  items: TaskRow[];
  progress: { done: number; target: number; percent: number; tasks: number };
  counts: { pending: number; done: number; errors: number; paused: number; total: number };
};

type RunRow = {
  id: number;
  thread_id: number | null;
  result: string;
  position: number | null;
  cart_added: number;
  liked: number;
  error_msg: string | null;
  started_at: string;
  finished_at: string;
};

type Props = {
  api: ApiClient;
  onNotice: (text: string) => void;
  onChanged: () => void;
};

const STATUS: Record<string, string> = {
  pending: "в очереди",
  claimed: "в работе",
  running: "в работе",
  done: "выполнено",
  error: "ошибка",
  paused: "на паузе",
};

const svg = (path: JSX.Element) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
    {path}
  </svg>
);

const ICON = {
  edit: svg(<><path d="M4 20h4l10-10-4-4L4 16v4Z" /><path d="m13.5 6.5 4 4" /></>),
  queue: svg(<><path d="M4 12a8 8 0 1 0 2.3-5.6" /><path d="M4 4v5h5" /></>),
  pause: svg(<><path d="M9 5v14M15 5v14" /></>),
  play: svg(<path d="M8 5.5v13l10-6.5-10-6.5Z" />),
  trash: svg(<><path d="M4 7h16" /><path d="M9 7V5h6v2" /><path d="M6 7l1 13h10l1-13" /></>),
};

export default function TasksPage({ api, onNotice, onChanged }: Props) {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<"all" | "pending" | "done" | "error" | "paused">("all");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<number | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [form, setForm] = useState({ query: "", sku: "", url: "", geo: "", target_runs: 1 });

  const load = useCallback(async () => {
    try {
      const query = search.trim() ? `?search=${encodeURIComponent(search.trim())}` : "";
      setData(await api.get<TasksResponse>(`/api/tasks${query}`));
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  }, [api, onNotice, search]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await work();
      onNotice(done);
      await load();
      onChanged();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const openAdd = () => {
    setEditId(null);
    setForm({ query: "", sku: "", url: "", geo: "", target_runs: 1 });
    setFormOpen(true);
  };

  const openEdit = (row: TaskRow) => {
    setEditId(row.id);
    setForm({
      query: row.query,
      sku: row.sku,
      url: row.url ?? "",
      geo: row.geo ?? "",
      target_runs: row.target_runs,
    });
    setFormOpen(true);
  };

  const saveForm = () => {
    if (!form.query.trim()) {
      onNotice("Введите поисковый запрос");
      return;
    }
    if (!form.sku.trim() && !form.url.trim()) {
      onNotice("Укажите SKU или ссылку на товар Ozon");
      return;
    }
    const payload = {
      query: form.query.trim(),
      sku: form.sku.trim(),
      url: form.url.trim(),
      geo: form.geo.trim(),
      target_runs: Math.max(1, Number(form.target_runs) || 1),
    };
    void act(
      () =>
        editId
          ? api.put(`/api/tasks/${editId}`, payload)
          : api.post("/api/tasks", payload),
      editId ? "Задание сохранено" : "Задание добавлено",
    ).then(() => {
      setFormOpen(false);
      setEditId(null);
      setForm({ query: "", sku: "", url: "", geo: "", target_runs: 1 });
    });
  };

  const remove = (row: TaskRow) => {
    if (!window.confirm(`Удалить задание #${row.id} «${row.query}» вместе с историей?`)) return;
    void act(() => api.delete(`/api/tasks/${row.id}`), "Задание удалено");
  };

  const showHistory = async (row: TaskRow) => {
    if (history === row.id) {
      setHistory(null);
      return;
    }
    try {
      const result = await api.get<{ items: RunRow[] }>(`/api/tasks/${row.id}/runs`);
      setRuns(result.items);
      setHistory(row.id);
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const toggleHistoryByRow = (row: TaskRow, event: MouseEvent<HTMLTableRowElement>) => {
    if ((event.target as HTMLElement).closest("button, input, select, a, textarea")) return;
    void showHistory(row);
  };

  const progress = data?.progress;
  const counts = data?.counts;
  const items = (data?.items ?? []).filter((row) => {
    if (filter === "all") return true;
    if (filter === "pending") return row.status === "pending" || row.status === "claimed" || row.status === "running";
    return row.status === filter;
  });
  const clearQueue = () => {
    if (!window.confirm("Убрать из очереди все задания, которые ещё не взяты в работу?")) return;
    void act(() => api.post("/api/tasks/queue/clear"), "Очередь очищена — задания на паузе");
  };
  const resumeQueue = () => {
    void act(() => api.post("/api/tasks/queue/resume"), "Задания возвращены в очередь");
  };
  return (
    <div className="tasks-page">
      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="table-head">
          <div>
            <b>Таблица заданий</b>
            <div className="field-hint">
              Задания и результаты хранятся в приложении. Добавляйте, правьте и удаляйте
              запросы здесь — Excel больше не нужен. Приглушённые строки — не в очереди
              (пауза, ошибка или выполнено). Клик по строке открывает историю прогонов.
            </div>
          </div>
          <div className="head-metrics">
            <span className="head-metric">
              Всего <b>{data?.counts.total ?? 0}</b>
            </span>
            <span className="head-metric">
              В очереди <b>{data?.counts.pending ?? 0}</b>
            </span>
            <span className="head-metric">
              Выполнено <b>{data?.counts.done ?? 0}</b>
            </span>
            <span className="head-metric">
              Ошибки <b>{data?.counts.errors ?? 0}</b>
            </span>
          </div>
        </div>
        <div className="progress-line">
          <div className="progress">
            <div className="progress-fill" style={{ width: `${progress?.percent ?? 0}%` }} />
          </div>
          <span className="muted">
            {progress?.target
              ? `${progress.done} / ${progress.target} прогонов · ${progress.percent}%`
              : "В очереди нет заданий"}
          </span>
        </div>
        <div className="subtabs" style={{ marginTop: 10 }}>
          {(
            [
              ["all", "Все", counts?.total ?? 0],
              ["pending", "В очереди", counts?.pending ?? 0],
              ["paused", "На паузе", counts?.paused ?? 0],
              ["done", "Выполненные", counts?.done ?? 0],
              ["error", "Ошибки", counts?.errors ?? 0],
            ] as const
          ).map(([key, label, value]) => (
            <button
              key={key}
              className={filter === key ? "subtab active" : "subtab"}
              onClick={() => setFilter(key)}
            >
              {label} <span className="count">{value}</span>
            </button>
          ))}
          <div className="spacer" />
          <button
            className="btn"
            onClick={resumeQueue}
            disabled={busy || (counts?.paused ?? 0) + (counts?.errors ?? 0) === 0}
            title="Вернуть в очередь задания с паузы и с ошибками"
          >
            Вернуть всё
          </button>
          <button
            className="btn danger"
            onClick={clearQueue}
            disabled={busy || !(counts?.pending ?? 0)}
            title="Все задания, ещё не взятые в работу, уйдут на паузу"
          >
            Очистить очередь
          </button>
        </div>
        <div className="input-row" style={{ marginTop: 10 }}>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Поиск по запросу или SKU"
          />
          <button className="btn" onClick={() => void load()} disabled={busy}>
            Обновить
          </button>
          <div className="spacer" />
          <button className="btn primary" onClick={openAdd}>
            Добавить задание
          </button>
        </div>
      </div>

      <div className="table-wrap" style={{ maxHeight: "62vh" }}>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Запрос</th>
              <th>SKU</th>
              <th>Прогоны</th>
              <th>Позиция</th>
              <th>Корзина</th>
              <th>Лайки</th>
              <th>Статус</th>
              <th>Последний результат</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <Fragment key={row.id}>
                <tr
                  className={[
                    ["pending", "claimed", "running"].includes(row.status) ? "" : "row-muted",
                    history === row.id ? "row-open" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={(event) => toggleHistoryByRow(row, event)}
                  title={
                    history === row.id
                      ? "Скрыть историю прогонов"
                      : "Показать историю прогонов"
                  }
                >
                  <td className="muted">{row.id}</td>
                  <td>
                    <span title={row.query}>{row.query}</span>
                  </td>
                  <td>{row.sku || "—"}</td>
                  <td>{`${row.done_runs} / ${row.target_runs}`}</td>
                  <td>{row.last_position ?? row.position ?? "—"}</td>
                  <td>{row.cart_adds}</td>
                  <td>{row.likes}</td>
                  <td>{STATUS[row.status] ?? row.status}</td>
                  <td>
                    {row.last_result === "ok" ? (
                      <span className="ok-text">
                        ок · поз. {row.last_position ?? "—"} · корз. {row.last_cart ?? 0}
                      </span>
                    ) : row.last_result === "error" ? (
                      <span className="err-text" title={row.last_error_msg ?? ""}>
                        ошибка
                      </span>
                    ) : (
                      "—"
                    )}
                    {row.last_finished ? (
                      <span className="muted"> · {row.last_finished.slice(11, 19)}</span>
                    ) : null}
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="btn icon"
                        title="Начать заново: обнулить прогресс"
                        onClick={() => {
                          if (
                            !window.confirm(
                              `Начать задание «${row.query}» заново? Выполненные ` +
                                `прогоны (${row.done_runs} из ${row.target_runs}) ` +
                                "обнулятся.",
                            )
                          ) {
                            return;
                          }
                          void act(
                            () => api.post(`/api/tasks/${row.id}/requeue`),
                            "Задание начнёт заново с нуля",
                          );
                        }}
                        disabled={busy}
                      >
                        {ICON.queue}
                      </button>
                      {row.status === "paused" || row.status === "error" ? (
                        <button
                          className="btn icon"
                          title={`Продолжить с текущего прогресса (${row.done_runs} / ${row.target_runs})`}
                          onClick={() =>
                            void act(
                              () => api.post(`/api/tasks/${row.id}/resume`),
                              "Задание продолжит с текущего прогресса",
                            )
                          }
                          disabled={busy}
                        >
                          {ICON.play}
                        </button>
                      ) : (
                        <button
                          className="btn icon"
                          title="Убрать из очереди"
                          onClick={() =>
                            void act(
                              () =>
                                api.post(`/api/tasks/${row.id}/pause`, {
                                  paused: true,
                                }),
                              "Задание убрано из очереди",
                            )
                          }
                          disabled={busy || row.status === "done"}
                        >
                          {ICON.pause}
                        </button>
                      )}
                      <button
                        className="btn icon"
                        title="Изменить"
                        onClick={() => openEdit(row)}
                        disabled={busy}
                      >
                        {ICON.edit}
                      </button>
                      <button
                        className="btn icon danger"
                        title="Удалить"
                        onClick={() => remove(row)}
                        disabled={busy}
                      >
                        {ICON.trash}
                      </button>
                    </div>
                  </td>
                </tr>
                {history === row.id && (
                  <tr key={`h-${row.id}`}>
                    <td colSpan={10}>
                      <div className="history-block">
                        <table>
                          <thead>
                            <tr>
                              <th>Время</th>
                              <th>Поток</th>
                              <th>Результат</th>
                              <th>Позиция</th>
                              <th>Корзина</th>
                              <th>Лайки</th>
                              <th>Ошибка</th>
                            </tr>
                          </thead>
                          <tbody>
                            {runs.map((run) => (
                              <tr key={run.id}>
                                <td className="muted">{run.finished_at}</td>
                                <td>{run.thread_id ?? "—"}</td>
                                <td
                                  className={
                                    run.result === "ok"
                                      ? "ok-text"
                                      : run.result === "error"
                                        ? "err-text"
                                        : ""
                                  }
                                >
                                  {run.result}
                                </td>
                                <td>{run.position ?? "—"}</td>
                                <td>{run.cart_added}</td>
                                <td>{run.liked}</td>
                                <td
                                  title={run.error_msg ?? ""}
                                  className={run.error_msg ? "err-text" : "muted"}
                                >
                                  {run.error_msg || "—"}
                                </td>
                              </tr>
                            ))}
                            {!runs.length && (
                              <tr>
                                <td colSpan={7} className="muted">
                                  Прогонов ещё не было
                                </td>
                              </tr>
                            )}
                          </tbody>
                        </table>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {!items.length && (
              <tr>
                <td colSpan={10} className="muted">
                  {filter === "pending"
                    ? "Очередь пуста"
                    : filter === "paused"
                      ? "На паузе ничего нет"
                      : "Заданий нет — добавьте первое выше"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {formOpen && (
        <div className="modal-backdrop" onMouseDown={() => setFormOpen(false)}>
          <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <h3>{editId ? "Редактирование задания" : "Новое задание"}</h3>
              <button className="btn small" type="button" onClick={() => setFormOpen(false)}>
                Закрыть
              </button>
            </div>
            <div className="modal-body">
              <div className="field">
                <span className="field-label">Поисковый запрос</span>
                <input
                  autoFocus
                  value={form.query}
                  onChange={(e) => setForm({ ...form, query: e.target.value })}
                  placeholder="например «ночная лампа usb»"
                />
              </div>
              <div className="row3">
                <div className="field">
                  <span className="field-label">SKU</span>
                  <input
                    value={form.sku}
                    onChange={(e) => setForm({ ...form, sku: e.target.value })}
                    placeholder="6332598536"
                  />
                </div>
                <div className="field">
                  <span className="field-label">Прогонов</span>
                  <input
                    type="number"
                    min={1}
                    max={1000}
                    value={form.target_runs}
                    onChange={(e) => setForm({ ...form, target_runs: Number(e.target.value) })}
                  />
                </div>
                <div className="field">
                  <span className="field-label">Гео</span>
                  <input
                    value={form.geo}
                    onChange={(e) => setForm({ ...form, geo: e.target.value })}
                    placeholder="необязательно"
                  />
                </div>
              </div>
              <div className="field">
                <span className="field-label">Ссылка на товар Ozon</span>
                <input
                  value={form.url}
                  onChange={(e) => setForm({ ...form, url: e.target.value })}
                  placeholder="https://www.ozon.ru/product/…"
                />
                <span className="field-hint">
                  Достаточно ссылки — SKU подтянется автоматически.
                </span>
              </div>
              <div className="input-row">
                <button className="btn primary" onClick={saveForm} disabled={busy}>
                  {editId ? "Сохранить" : "Добавить"}
                </button>
                <button className="btn ghost" onClick={() => setFormOpen(false)}>
                  Отмена
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
