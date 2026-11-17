import { useEffect, useState } from 'react';
import { Icon } from '../components/Icon';
import { ThemeSwitch } from '../components/ThemeSwitch';
import { writeOwnerToken } from '../api/client';
import { readRaw, STORAGE_KEYS } from '../lib/storage';
import { THEME_LABELS, useTheme } from '../lib/theme';

interface SettingsPayload {
  app: { name: string; version: string; timezone: string; data_dir: string };
  paths: Record<string, string>;
  vault: { path: string; daily_dir: string; template: string; enabled: boolean };
  telegram: { configured: boolean; chat_id: string; bot_username: string } | Record<string, unknown>;
  llm: { configured: boolean; model: string; base_url: string } | Record<string, unknown>;
  preferences: Record<string, unknown>;
  version: string;
}

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');

/** Заголовок токена для «сырых» fetch: cookie выставляется при сохранении. */
function authHeaders(): Record<string, string> {
  const token = readRaw(STORAGE_KEYS.token);
  return token ? { 'X-LifeOS-Token': token } : {};
}

function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? 'да' : 'нет';
  return String(value);
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="setting-row">
      <span className="setting-row__label">{label}</span>
      <span className="setting-row__value">{value}</span>
    </div>
  );
}

export function Settings() {
  const theme = useTheme();
  const [data, setData] = useState<SettingsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<string>('проверяю');
  const [token, setToken] = useState<string>(() => readRaw(STORAGE_KEYS.token) ?? '');
  const [tokenState, setTokenState] = useState<'idle' | 'saved' | 'rejected'>('idle');

  useEffect(() => {
    fetch(`${API_BASE}/settings`, { credentials: 'same-origin', headers: authHeaders() })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<SettingsPayload>;
      })
      .then(setData)
      .catch((problem: unknown) => setError(problem instanceof Error ? problem.message : 'нет ответа'));

    fetch(`${API_BASE}/health`, { credentials: 'same-origin', headers: authHeaders() })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then((payload: { status?: string }) => setHealth(payload.status ?? 'ok'))
      .catch(() => setHealth('нет связи'));
  }, []);

  function exportJson() {
    void (async () => {
      const parts = await Promise.all(
        ['today', 'journal', 'habits', 'goals'].map((name) =>
          fetch(`${API_BASE}/sections/${name}?days=120`, {
            credentials: 'same-origin',
            headers: authHeaders(),
          })
            .then((response) => (response.ok ? response.json() : null))
            .catch(() => null),
        ),
      );
      const payload = { exportedAt: new Date().toISOString(), sections: parts };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `lifeos-export-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
    })();
  }

  function saveToken(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    writeOwnerToken(token);
    // Проверяем токен защищённым запросом: health публичный и ничего не докажет.
    void fetch(`${API_BASE}/settings`, {
      credentials: 'same-origin',
      headers: { 'X-LifeOS-Token': token.trim() },
    })
      .then((response) => setTokenState(response.ok ? 'saved' : 'rejected'))
      .catch(() => setTokenState('rejected'));
  }

  function forgetToken() {
    setToken('');
    writeOwnerToken(null);
    setTokenState('idle');
  }

  const telegram = (data?.telegram ?? {}) as Record<string, unknown>;
  const llm = (data?.llm ?? {}) as Record<string, unknown>;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="page-head__kicker">Life OS · v3</p>
          <h1 className="page-head__title">Настройки</h1>
          <p className="page-head__sub">Оформление, пути, подключения и экспорт данных.</p>
        </div>
        <span className="status-pill">
          <span className="status-pill__dot" aria-hidden="true" />
          API: {health}
        </span>
      </header>

      {error ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>Настройки не загрузились ({error}). Проверьте, запущен ли сервер.</span>
        </div>
      ) : null}

      <section className="card" aria-labelledby="theme-title">
        <header className="card__head">
          <div>
            <h2 className="card__title" id="theme-title">
              Оформление
            </h2>
            <p className="card__sub">Сейчас: {THEME_LABELS[theme.mode]} · {theme.resolved === 'dark' ? 'тёмная' : 'светлая'} палитра</p>
          </div>
        </header>
        <ThemeSwitch mode={theme.mode} collapsed={false} onSelect={theme.setMode} />
      </section>

      <section className="card" aria-labelledby="app-title">
        <header className="card__head">
          <h2 className="card__title" id="app-title">
            Приложение
          </h2>
        </header>
        <Row label="Версия" value={text(data?.version ?? data?.app?.version)} />
        <Row label="Часовой пояс" value={text(data?.app?.timezone)} />
        <Row label="Каталог данных" value={text(data?.app?.data_dir)} />
        <Row label="База" value={text(data?.paths?.db)} />
        <Row label="Медиа" value={text(data?.paths?.media)} />
      </section>

      <section className="card" aria-labelledby="token-title">
        <header className="card__head">
          <div>
            <h2 className="card__title" id="token-title">
              Доступ
            </h2>
            <p className="card__sub">
              Если сервер запущен с токеном владельца, вставьте его здесь — он сохранится в
              cookie и не будет показан снова.
            </p>
          </div>
        </header>
        <form className="setting-actions" onSubmit={saveToken}>
          <label className="setting-row__label" htmlFor="owner-token">
            Токен владельца
          </label>
          <input
            id="owner-token"
            className="input"
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="X-LifeOS-Token"
            value={token}
            onChange={(event) => {
              setToken(event.target.value);
              setTokenState('idle');
            }}
          />
          <button className="btn" type="submit">
            <Icon name="check" size={16} />
            <span>Сохранить</span>
          </button>
          <button className="btn btn--ghost" type="button" onClick={forgetToken}>
            <Icon name="close" size={16} />
            <span>Забыть</span>
          </button>
        </form>
        {tokenState === 'saved' ? (
          <p className="card__sub" role="status">
            Токен принят сервером и сохранён в этом браузере.
          </p>
        ) : null}
        {tokenState === 'rejected' ? (
          <p className="card__sub" role="status">
            Сервер отклонил токен (401). Проверьте значение и повторите.
          </p>
        ) : null}
      </section>

      <section className="card" aria-labelledby="vault-title">
        <header className="card__head">
          <div>
            <h2 className="card__title" id="vault-title">
              Obsidian
            </h2>
            <p className="card__sub">Дневник пишется идемпотентно: одна запись — один блок.</p>
          </div>
        </header>
        <Row label="Включён" value={text(data?.vault?.enabled)} />
        <Row label="Хранилище" value={text(data?.vault?.path)} />
        <Row label="Дневные заметки" value={text(data?.vault?.daily_dir)} />
        <Row label="Шаблон" value={text(data?.vault?.template)} />
      </section>

      <section className="card" aria-labelledby="conn-title">
        <header className="card__head">
          <h2 className="card__title" id="conn-title">
            Подключения
          </h2>
        </header>
        <Row label="Telegram-бот" value={text(telegram.configured)} />
        <Row label="Чат" value={text(telegram.chat_id)} />
        <Row label="Имя бота" value={text(telegram.bot_username)} />
        <Row label="LLM" value={text(llm.configured)} />
        <Row label="Модель" value={text(llm.model)} />
        <Row label="Шлюз" value={text(llm.base_url)} />
      </section>

      <section className="card" aria-labelledby="export-title">
        <header className="card__head">
          <div>
            <h2 className="card__title" id="export-title">
              Данные
            </h2>
            <p className="card__sub">Экспорт за 120 дней — JSON, открывается в любом редакторе.</p>
          </div>
        </header>
        <div className="setting-actions">
          <button className="btn" type="button" onClick={exportJson}>
            <Icon name="book" size={16} />
            <span>Скачать экспорт</span>
          </button>
          <button className="btn btn--ghost" type="button" onClick={() => window.location.reload()}>
            <Icon name="sync" size={16} />
            <span>Обновить страницу</span>
          </button>
        </div>
      </section>

      <p className="page-foot">
        Токены и ключи здесь не показываются: сервер отдаёт только признак «настроено».
      </p>
    </div>
  );
}
