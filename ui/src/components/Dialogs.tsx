import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { ApiClient } from "../api";
import type { Report, SchedulerState } from "../types";

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn small" onClick={onClose} type="button">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function SchedulerPanel({ api, onNotice }: { api: ApiClient; onNotice: (text: string) => void }) {
  const [state, setState] = useState<SchedulerState | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [interval, setInterval] = useState(60);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.get<SchedulerState>("/api/scheduler");
      setState(next);
      setEnabled(next.enabled);
      setInterval(next.interval_min);
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  }, [api, onNotice]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const save = async () => {
    setBusy(true);
    try {
      const next = await api.post<SchedulerState>("/api/scheduler", {
        enabled,
        interval_min: interval,
      });
      setState(next);
      onNotice(
        next.enabled
          ? `Планировщик включён, следующий запуск в ${next.next_run ?? "—"}`
          : "Планировщик выключен",
      );
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="toggle-row">
        <span>Запускать прогон автоматически</span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          className={enabled ? "switch on" : "switch"}
          onClick={() => setEnabled(!enabled)}
        >
          <i />
        </button>
      </div>
      <div className="field">
        <span className="field-label">Интервал, минут</span>
        <input
          type="number"
          min={5}
          max={1440}
          value={interval}
          onChange={(event) => setInterval(Number(event.target.value))}
        />
      </div>
      <button className="btn primary" onClick={save} disabled={busy} type="button">
        {busy ? "Сохраняем…" : "Применить"}
      </button>

      <div className="report-grid">
        <Row label="Состояние" value={state?.enabled ? "включён" : "выключен"} />
        <Row label="Последний запуск" value={state?.last_run ?? "—"} />
        <Row label="Следующий запуск" value={state?.next_run ?? "—"} />
        <Row label="Результат" value={state?.last_result ?? "—"} />
      </div>
      <div className="field-hint">
        Прогон стартует только если очередь не пуста и текущий прогон завершён. Первый запуск — через
        интервал после включения.
      </div>
    </div>
  );
}

export function ReportPanel({ api, onNotice }: { api: ApiClient; onNotice: (text: string) => void }) {
  const [report, setReport] = useState<Report | null>(null);

  const load = useCallback(async () => {
    try {
      setReport(await api.get<Report>("/api/report"));
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  }, [api, onNotice]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!report) return <div className="empty">Собираем данные…</div>;

  return (
    <div className="stack">
      <Section title="Задания">
        <Row label="В очереди" value={report.tasks.pending} />
        <Row label="Готово" value={report.tasks.done} />
        <Row label="С ошибкой" value={report.tasks.errors} />
        <Row label="Приостановлено (другой источник)" value={report.tasks.paused} />
        <Row label="Всего" value={report.tasks.total} />
      </Section>
      <Section title="Прогоны">
        <Row label="Сегодня" value={report.runs.today} />
        <Row label="Успешно" value={report.runs.ok} />
        <Row label="Ошибок" value={report.runs.errors} />
        <Row label="Всего" value={report.runs.total} />
      </Section>
      <Section title="Ресурсы">
        <Row label="Открытых потоков" value={report.sessions.threads} />
        <Row label="Ручных сессий" value={report.sessions.manual} />
        <Row label="Процессов Chromium" value={`${report.processes.chromium} · ${report.processes.chromium_mb} МБ`} />
        <Row label="Память ядра" value={`${report.processes.backend_mb} МБ`} />
        <Row label="База данных" value={`${report.disk.db_mb} МБ`} />
        <Row
          label="Артефакты ошибок"
          value={`${report.disk.artifacts_mb} МБ · ${report.disk.artifacts_files} файлов`}
        />
      </Section>
      <Section title="Профили и прокси">
        <Row label="Активные" value={report.profiles.active} />
        <Row label="Отработанные" value={report.profiles.done} />
        <Row label="Безакки" value={report.profiles.noauth} />
        <Row label="Прокси в файле" value={`${report.proxies.total} · невалидных ${report.proxies.invalid}`} />
      </Section>
      <button className="btn" onClick={() => void load()} type="button">
        Обновить
      </button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="report-title">{title}</div>
      <div className="report-grid">{children}</div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="report-row">
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}
