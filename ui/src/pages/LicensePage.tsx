import { useCallback, useEffect, useState } from "react";
import type { ApiClient } from "../api";
import type { LicenseStatus, Settings } from "../types";

type Props = {
  api: ApiClient;
  settings: Settings;
  onChange: (next: Settings) => void;
  onNotice: (text: string) => void;
};

const MODES: Record<string, { label: string; tone: string }> = {
  dev: { label: "Демо-режим (без ключа)", tone: "gray" },
  key: { label: "Активна", tone: "ok" },
  online: { label: "Активна (онлайн)", tone: "ok" },
  grace: { label: "Активна (офлайн-допуск)", tone: "warn" },
  none: { label: "Ключ не активирован", tone: "err" },
  invalid: { label: "Не активна", tone: "err" },
};

export default function LicensePage({ api, settings, onChange, onNotice }: Props) {
  const [status, setStatus] = useState<LicenseStatus | null>(null);
  const [key, setKey] = useState(settings.license.key);
  const [server, setServer] = useState(settings.license.server);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<LicenseStatus>("/api/license"));
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  }, [api, onNotice]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setKey(settings.license.key);
    setServer(settings.license.server);
  }, [settings.license.key, settings.license.server]);

  const activate = async () => {
    if (!key.trim()) {
      onNotice("Вставьте ключ лицензии");
      return;
    }
    setBusy(true);
    try {
      const result = await api.post<LicenseStatus & { ok: boolean; error?: string }>(
        "/api/license/activate",
        { key: key.trim(), server: server.trim() },
      );
      if (!result.ok) {
        onNotice(result.error ?? "Ключ не принят");
        setStatus(result);
        return;
      }
      onChange({ ...settings, license: { key: key.trim(), server: server.trim() } });
      setStatus(result);
      onNotice(`Лицензия активирована: ${result.subject ?? "без имени"}`);
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setBusy(true);
    try {
      const result = await api.post<LicenseStatus & { error?: string }>("/api/license/check");
      setStatus(result);
      onNotice(
        result.valid
          ? `Лицензия в порядке${result.warning ? `: ${result.warning}` : ""}`
          : `Лицензия: ${result.error ?? "не активна"}`,
      );
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const copyHwid = async () => {
    if (!status) return;
    try {
      await navigator.clipboard.writeText(status.hwid);
      onNotice("HWID скопирован");
    } catch {
      onNotice(`HWID: ${status.hwid}`);
    }
  };

  const mode = status ? (MODES[status.mode] ?? { label: status.mode, tone: "gray" }) : null;

  return (
    <div className="grid">
      <fieldset className="panel">
        <legend>Состояние лицензии</legend>
        {!status ? (
          <div className="muted">Проверяем…</div>
        ) : (
          <>
            <div className="panel-head">
              <h3>{status.subject ?? "—"}</h3>
              <span className={`tag ${mode?.tone === "ok" ? "ok" : mode?.tone === "err" ? "err" : ""}`}>
                {mode?.label}
              </span>
            </div>

            <div className="field">
              <span className="field-label">HWID этого компьютера</span>
              <div style={{ display: "flex", gap: 8 }}>
                <input readOnly value={status.hwid} style={{ fontFamily: "var(--mono)" }} />
                <button className="btn small" type="button" onClick={copyHwid}>
                  Копировать
                </button>
              </div>
              <span className="field-hint">
                Ключ привязан к HWID. Для переноса на другую машину выпустите новый ключ.
              </span>
            </div>

            <div className="thread-row">
              <span>Срок действия</span>
              <b>{status.expires ?? "бессрочно"}</b>
            </div>
            <div className="thread-row">
              <span>Осталось</span>
              <b>{status.days_left === null ? "—" : `${status.days_left} дн.`}</b>
            </div>
            <div className="thread-row">
              <span>Возможности</span>
              <b>{status.features?.length ? status.features.join(", ") : "—"}</b>
            </div>
            <div className="thread-row">
              <span>Сервер проверки</span>
              <b>{status.server || "не задан (локальная подпись)"}</b>
            </div>
            <div className="thread-row">
              <span>Последняя проверка</span>
              <b>{status.last_check?.replace("T", " ") ?? "—"}</b>
            </div>

            {status.error && <div className="err-text">{status.error}</div>}
            {status.warning && <div className="muted">{status.warning}</div>}

            <button className="btn" type="button" onClick={check} disabled={busy || !status.enabled}>
              Проверить снова
            </button>
          </>
        )}
      </fieldset>

      <fieldset className="panel">
        <legend>Активация</legend>
        <div className="field">
          <span className="field-label">Ключ лицензии</span>
          <textarea
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="PF1.…"
            rows={5}
            spellCheck={false}
            style={{
              width: "100%",
              background: "#0f141b",
              border: "1px solid var(--border)",
              borderRadius: 6,
              color: "var(--text)",
              padding: "6px 8px",
              fontFamily: "var(--mono)",
              fontSize: 11,
              resize: "vertical",
            }}
          />
          <span className="field-hint">Вставьте ключ целиком, одной строкой — формат PF1.…</span>
        </div>

        <div className="field">
          <span className="field-label">Сервер лицензий</span>
          <input
            value={server}
            onChange={(e) => setServer(e.target.value)}
            placeholder="https://license.example.com"
          />
          <span className="field-hint">
            Необязательно. Без сервера ключ проверяется локально по подписи. С сервером действует
            офлайн-допуск 7 дней.
          </span>
        </div>

        <button className="btn primary" type="button" onClick={activate} disabled={busy}>
          {busy ? "Проверяем…" : "Активировать"}
        </button>

        <div className="muted">
          Ключ подписан Ed25519 и содержит имя клиента, список HWID и срок. Приватный ключ хранится
          только у поставщика: подделать срок или HWID в ключе нельзя.
        </div>
      </fieldset>
    </div>
  );
}
