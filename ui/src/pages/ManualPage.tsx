import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { ApiClient } from "../api";
import type { ManualItem, ProfileLists } from "../types";

type Props = {
  api: ApiClient;
  onNotice: (text: string) => void;
};

const KIND_LABEL: Record<string, string> = {
  active: "Активные (аккаунты)",
  done: "Отработанные",
  noauth: "Без авторизации",
};

export default function ManualPage({ api, onNotice }: Props) {
  const [profiles, setProfiles] = useState<ProfileLists | null>(null);
  const [items, setItems] = useState<ManualItem[]>([]);
  const [profilePath, setProfilePath] = useState("");
  const [proxy, setProxy] = useState("");
  const [url, setUrl] = useState("https://www.ozon.ru/cart");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [nextProfiles, nextItems] = await Promise.all([
        api.get<ProfileLists>("/api/manual/profiles"),
        api.get<{ items: ManualItem[] }>("/api/manual"),
      ]);
      setProfiles(nextProfiles);
      setItems(nextItems.items);
    } catch {
      /* панель обновится на следующем тике */
    }
  }, [api]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const start = async () => {
    setBusy(true);
    try {
      const result = await api.post<{ ok: boolean; error?: string }>("/api/manual/start", {
        profile_path: profilePath || null,
        proxy: proxy || null,
        url: url || null,
      });
      onNotice(result.ok ? "Браузер открыт — можно оформлять заказ" : (result.error ?? "Не удалось открыть"));
      await refresh();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (item: ManualItem) => {
    try {
      const result = await api.post<{ ok: boolean; error?: string }>(
        `/api/manual/${item.id}/browser`,
        { visible: !item.visible },
      );
      if (!result.ok) onNotice(result.error ?? "Окно недоступно");
      await refresh();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const close = async (item: ManualItem) => {
    try {
      await api.post(`/api/manual/${item.id}/close`);
      onNotice(`Сессия ${item.profile} закрыта`);
      await refresh();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  const navigate = async (item: ManualItem) => {
    const target = window.prompt("Куда перейти?", item.url || "https://www.ozon.ru/cart");
    if (!target) return;
    try {
      const result = await api.post<{ ok: boolean; error?: string }>(
        `/api/manual/${item.id}/navigate`,
        { url: target },
      );
      if (!result.ok) onNotice(result.error ?? "Переход не удался");
      await refresh();
    } catch (error) {
      onNotice(String(error).replace("Error: ", ""));
    }
  };

  return (
    <div className="worker">
      <div className="grid">
        <fieldset className="panel">
          <legend>Открыть браузер для выкупа</legend>
          <Field label="Профиль">
            <select value={profilePath} onChange={(event) => setProfilePath(event.target.value)}>
              <option value="">Новый безакк (временная папка)</option>
              {profiles &&
                (Object.keys(KIND_LABEL) as Array<keyof ProfileLists>).map((kind) =>
                  profiles[kind].length ? (
                    <optgroup key={kind} label={KIND_LABEL[kind]}>
                      {profiles[kind].map((entry) => (
                        <option key={entry.path} value={entry.path}>
                          {entry.name}
                        </option>
                      ))}
                    </optgroup>
                  ) : null,
                )}
            </select>
          </Field>
          <Field label="Прокси" hint="пусто = напрямую; socks5://user:pass@host:port">
            <input value={proxy} onChange={(event) => setProxy(event.target.value)} placeholder="socks5://…" />
          </Field>
          <Field label="Стартовая страница">
            <input value={url} onChange={(event) => setUrl(event.target.value)} />
          </Field>
          <button className="btn primary" onClick={start} disabled={busy} type="button">
            {busy ? "Открываем…" : "Открыть браузер"}
          </button>
          <div className="field-hint">
            Откройте корзину профиля, оформите заказ вручную и закройте сессию. Профиль, занятый
            потоком прогона, открыть нельзя.
          </div>
        </fieldset>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h3>Открытые сессии</h3>
          <span className="muted">{items.length ? `${items.length} шт.` : "нет"}</span>
        </div>
        {items.length === 0 ? (
          <div className="empty">Ручных сессий пока нет</div>
        ) : (
          <div className="threads">
            {items.map((item) => (
              <div className="thread-card" key={item.id}>
                <div className="thread-head">
                  <span className="thread-title">{item.profile}</span>
                  <span className="mono muted">#{item.id}</span>
                </div>
                <div className="thread-body">
                  <div className="thread-row">
                    <span>Профиль</span>
                    <b title={item.profile_kind}>{item.profile_kind}</b>
                  </div>
                  <div className="thread-row">
                    <span>Прокси</span>
                    <b title={item.proxy}>{item.proxy}</b>
                  </div>
                  <div className="thread-row">
                    <span>Окно</span>
                    <b>{item.hwnd ? `${item.hwnd} · ${item.visible ? "видно" : "скрыто"}` : "headless"}</b>
                  </div>
                  <div className="thread-row">
                    <span>Страница</span>
                    <b title={item.url}>{item.url}</b>
                  </div>
                </div>
                <div className="manual-actions">
                  <button className="btn small" onClick={() => void toggle(item)} type="button">
                    {item.visible ? "Спрятать" : "Показать"}
                  </button>
                  <button className="btn small" onClick={() => void navigate(item)} type="button">
                    Перейти
                  </button>
                  <button className="btn small danger" onClick={() => void close(item)} type="button">
                    Закрыть
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
