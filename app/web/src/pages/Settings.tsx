import { useEffect, useState } from 'react';
import { apiRequest, writeOwnerToken } from '../api/client';
import { Button, Card, Field, Input } from '../components/ui';
import { ThemeSwitch } from '../components/ThemeSwitch';
import { THEME_LABELS, useTheme } from '../lib/theme';

interface SettingsPayload {
  app?: { name?: string; version?: string; timezone?: string; data_dir?: string };
  paths?: Record<string, string>;
  vault?: { path?: string; daily_dir?: string; template?: string; enabled?: boolean };
  telegram?: Record<string, unknown>;
  llm?: Record<string, unknown>;
  preferences?: Record<string, unknown>;
  version?: string;
}

interface HealthPayload {
  status?: string;
}

const EXPORT_SECTIONS = ['today', 'journal', 'habits', 'goals'] as const;

function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? 'да' : 'нет';
  return String(value);
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="set-row">
      <span className="set-row__label">{label}</span>
      <span className="set-row__value">{value}</span>
    </div>
  );
}

/**
 * Настройки: единственное место, где показаны версия и «О программе»
 * (DESIGN-v4 §2a, HANDOFF §7.3). Токены и ключи не печатаются — только
 * честный признак «настроено/нет».
 */
export function Settings() {
  const theme = useTheme();
  const [data, setData] = useState<SettingsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<string>('проверяем');
  const [token, setToken] = useState('');
  const [tokenState, setTokenState] = useState<'idle' | 'saved' | 'rejected'>('idle');
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    let alive = true;
    apiRequest<SettingsPayload>('/settings', undefined, 4000)
      .then((payload) => {
        if (alive) setData(payload);
      })
      .catch((problem: unknown) => {
        if (alive) setError(problem instanceof Error ? problem.message : 'нет ответа');
      });

    apiRequest<HealthPayload>('/health', undefined, 3000)
      .then((payload) => {
        if (alive) setHealth(payload.status ?? 'ok');
      })
      .catch(() => {
        if (alive) setHealth('нет связи');
      });

    return () => {
      alive = false;
    };
  }, []);

  async function exportJson(): Promise<void> {
    setExporting(true);
    try {
      const parts = await Promise.all(
        EXPORT_SECTIONS.map((name) =>
          apiRequest<unknown>(`/sections/${name}?days=120`, undefined, 8000).catch(() => null),
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
    } finally {
      setExporting(false);
    }
  }

  function saveToken(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!token.trim()) {
      setTokenState('idle');
      return;
    }
    writeOwnerToken(token);
    // Health публичный и ничего не докажет — проверяем сам токен защищённым запросом.
    void apiRequest('/settings', undefined, 4000)
      .then(() => setTokenState('saved'))
      .catch(() => setTokenState('rejected'));
  }

  function forgetToken(): void {
    setToken('');
    writeOwnerToken(null);
    setTokenState('idle');
  }

  const telegram = data?.telegram ?? {};
  const llm = data?.llm ?? {};
  const version = text(data?.version ?? data?.app?.version);
  const apiTone = health === 'ok' ? 'ok' : health === 'проверяем' ? 'pending' : 'error';

  return (
    <div className="page set">
      <header className="page-head">
        <div className="page-head__text">
          <h1 className="page-head__title">Настройки</h1>
          <p className="page-head__sub">
            Оформление, подключения, данные и сведения о приложении.
          </p>
        </div>
        <p className="set-health" data-tone={apiTone}>
          <span className="set-health__dot" aria-hidden="true" />
          API: {health}
        </p>
      </header>

      {error ? (
        <Card>
          <p className="set-note">
            Настройки не загрузились ({error}). Проверьте, запущен ли сервер — остальные
            разделы работают независимо.
          </p>
        </Card>
      ) : null}

      <Card>
        <Card.Header
          title="Оформление"
          subtitle={`${THEME_LABELS[theme.mode]} · ${
            theme.resolved === 'dark' ? 'тёмная палитра' : 'светлая палитра'
          }`}
        />
        <div className="set-theme">
          <ThemeSwitch mode={theme.mode} collapsed={false} onSelect={theme.setMode} />
        </div>
      </Card>

      <Card>
        <Card.Header
          title="О программе"
          subtitle="Версия — только здесь, в интерфейсе она больше нигде не показывается"
        />
        <div className="set-rows">
          <Row label="Версия" value={version} />
          <Row label="Часовой пояс" value={text(data?.app?.timezone)} />
          <Row label="Каталог данных" value={text(data?.app?.data_dir)} />
          <Row label="База" value={text(data?.paths?.db)} />
          <Row label="Медиа" value={text(data?.paths?.media)} />
        </div>
      </Card>

      <Card>
        <Card.Header
          title="Доступ"
          subtitle="Если сервер запущен с токеном владельца, вставьте его — он сохранится в этом браузере."
        />
        <form className="set-form" onSubmit={saveToken}>
          <Field label="Токен владельца" htmlFor="owner-token" hint="Заголовок X-LifeOS-Token">
            <Input
              id="owner-token"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="Вставьте токен"
              value={token}
              onChange={(event) => {
                setToken(event.target.value);
                setTokenState('idle');
              }}
            />
          </Field>
          <div className="set-form__actions">
            <Button type="submit" variant="secondary" size="sm">
              Сохранить
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={forgetToken}>
              Забыть токен
            </Button>
          </div>
        </form>
        {tokenState === 'saved' ? (
          <p className="set-note" role="status">
            Токен принят сервером и сохранён в этом браузере.
          </p>
        ) : null}
        {tokenState === 'rejected' ? (
          <p className="set-note set-note--error" role="status">
            Сервер отклонил токен (401). Проверьте значение и повторите.
          </p>
        ) : null}
      </Card>

      <Card>
        <Card.Header title="Obsidian" subtitle="Дневник пишется идемпотентно: одна запись — один блок" />
        <div className="set-rows">
          <Row label="Включён" value={text(data?.vault?.enabled)} />
          <Row label="Хранилище" value={text(data?.vault?.path)} />
          <Row label="Дневные заметки" value={text(data?.vault?.daily_dir)} />
          <Row label="Шаблон" value={text(data?.vault?.template)} />
        </div>
      </Card>

      <Card>
        <Card.Header title="Подключения" subtitle="Только факты: секреты сервер не отдаёт" />
        <div className="set-rows">
          <Row label="Telegram-бот" value={text(telegram.configured)} />
          <Row label="Имя бота" value={text(telegram.bot_username)} />
          <Row label="Чат" value={text(telegram.chat_id)} />
          <Row label="LLM" value={text(llm.configured)} />
          <Row label="Модель" value={text(llm.model)} />
          <Row label="Шлюз" value={text(llm.base_url)} />
        </div>
      </Card>

      <Card>
        <Card.Header
          title="Данные"
          subtitle="Экспорт за 120 дней — JSON, открывается в любом редакторе"
        />
        <div className="set-form__actions">
          <Button
            variant="secondary"
            size="sm"
            leadingIcon="download"
            loading={exporting}
            onClick={() => {
              void exportJson();
            }}
          >
            Скачать экспорт
          </Button>
          <Button
            variant="ghost"
            size="sm"
            leadingIcon="sync"
            onClick={() => window.location.reload()}
          >
            Обновить страницу
          </Button>
        </div>
      </Card>

      <p className="set-foot">
        Токены и ключи здесь не печатаются: сервер отдаёт только признак «настроено».
      </p>
    </div>
  );
}
