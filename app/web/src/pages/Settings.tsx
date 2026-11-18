import '../styles/settings.css';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { apiRequest, writeOwnerToken } from '../api/client';
import {
  addWeightPoint,
  checkWeightDraft,
  downloadCsvTemplate,
  fetchWeightSources,
  formatLastSync,
  importSummary,
  importWeightCsv,
  manualSummary,
  setWeightSourceEnabled,
} from '../api/integrations';
import type { WeightSource } from '../api/integrations';
import { Button, Card, DateInput, Field, Icon, Input, Skeleton } from '../components/ui';
import { ThemeSwitch } from '../components/ThemeSwitch';
import { toLocalIso } from '../lib/format';
import { THEME_LABELS, useTheme } from '../lib/theme';
import { toast } from '../lib/toast';

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

interface SourceCopy {
  name: string;
  hint: string;
}

/**
 * Названия и пояснения источников — по одному предложению, без воды.
 * Неизвестный ключ показываем как есть: список приходит с сервера, и экран
 * не должен ломаться, если источников станет больше.
 */
const SOURCE_COPY: Record<string, SourceCopy> = {
  scale: {
    name: 'Весы (импорт файла)',
    hint: 'CSV с двумя колонками: дата и вес в килограммах.',
  },
  'apple-health': {
    name: 'Apple Health (экспорт .csv)',
    hint: 'Файл «Экспорт всех данных»: из него нужны только даты и вес.',
  },
  manual: {
    name: 'Ручной ввод',
    hint: 'Дата и вес руками — файл и приложение не нужны.',
  },
};

const EXPORT_SECTIONS = ['today', 'journal', 'habits', 'goals'] as const;

function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? 'да' : 'нет';
  return String(value);
}

function errorText(problem: unknown): string {
  if (problem instanceof Error && problem.message) return problem.message;
  return 'нет ответа';
}

function sourceCopy(source: WeightSource): SourceCopy {
  const known = SOURCE_COPY[source.key];
  if (known) return known;
  const first = source.notes.split('. ')[0]?.trim() ?? '';
  return {
    name: source.title,
    hint: first ? `${first.replace(/\.$/, '')}.` : 'Источник из списка сервера.',
  };
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
 * «Подключения»: как вес попадает в базу. Автоматических OAuth-интеграций
 * нет — есть файл, который выбирает человек, и ручной ввод. Это и написано
 * на экране: обещаний «подключим за вас» здесь нет.
 */
function ConnectionsCard() {
  const today = toLocalIso();
  const [sources, setSources] = useState<WeightSource[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [importingKey, setImportingKey] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState({ date: today, weight: '' });
  const [draftError, setDraftError] = useState<string | null>(null);
  const [savingDraft, setSavingDraft] = useState(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const list = await fetchWeightSources();
      if (!alive.current) return;
      setSources(list);
      setLoadError(null);
    } catch (problem) {
      if (!alive.current) return;
      setLoadError(errorText(problem));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function putRowError(key: string, message: string | null): void {
    setRowErrors((prev) => {
      const next = { ...prev };
      if (message) next[key] = message;
      else delete next[key];
      return next;
    });
  }

  async function toggleSource(source: WeightSource): Promise<void> {
    if (pendingKey) return;
    const next = !source.enabled;
    setPendingKey(source.key);
    putRowError(source.key, null);
    setSources((prev) =>
      prev ? prev.map((item) => (item.key === source.key ? { ...item, enabled: next } : item)) : prev,
    );
    try {
      const updated = await setWeightSourceEnabled(source.key, next);
      if (!alive.current) return;
      setSources((prev) =>
        prev ? prev.map((item) => (item.key === updated.key ? updated : item)) : prev,
      );
    } catch (problem) {
      if (!alive.current) return;
      setSources((prev) =>
        prev
          ? prev.map((item) =>
              item.key === source.key ? { ...item, enabled: source.enabled } : item,
            )
          : prev,
      );
      putRowError(source.key, `Не переключилось: ${errorText(problem)}`);
    } finally {
      if (alive.current) setPendingKey(null);
    }
  }

  async function importFile(source: WeightSource, file: File): Promise<void> {
    setImportingKey(source.key);
    putRowError(source.key, null);
    try {
      const result = await importWeightCsv(source.key, file);
      if (!alive.current) return;
      const summary = importSummary(result);
      toast(summary.text, summary.tone);
      await load();
    } catch (problem) {
      if (!alive.current) return;
      putRowError(source.key, `Файл «${file.name}»: ${errorText(problem)}`);
    } finally {
      if (alive.current) setImportingKey(null);
    }
  }

  async function submitDraft(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const check = checkWeightDraft(draft, today);
    if (!check.ok || check.weight === null) {
      setDraftError(check.error);
      return;
    }
    setDraftError(null);
    setSavingDraft(true);
    try {
      const result = await addWeightPoint(draft.date, check.weight);
      if (!alive.current) return;
      const summary = manualSummary(draft.date, check.weight, result);
      toast(summary.text, summary.tone);
      setDraft((prev) => ({ ...prev, weight: '' }));
      await load();
    } catch (problem) {
      if (!alive.current) return;
      setDraftError(errorText(problem));
    } finally {
      if (alive.current) setSavingDraft(false);
    }
  }

  function saveTemplate(): void {
    const name = downloadCsvTemplate();
    toast(`Шаблон сохранён: ${name}`);
  }

  const manualOff = sources?.some((source) => source.key === 'manual' && !source.enabled) ?? false;

  return (
    <Card>
      <Card.Header
        title="Подключения"
        subtitle="Вес попадает в базу файлом из приложения весов или руками"
        actions={
          <Button size="sm" variant="ghost" leadingIcon="download" onClick={saveTemplate}>
            Скачать шаблон
          </Button>
        }
      />

      {sources === null && !loadError ? (
        <div className="set-srcs__loading" aria-busy="true">
          <Skeleton variant="row" label="Загружаем источники веса" />
          <Skeleton variant="row" label="Загружаем источники веса" />
        </div>
      ) : null}

      {loadError ? (
        <>
          <p className="set-note">
            Источники не загрузились ({loadError}). Проверьте, запущен ли сервер.
          </p>
          <div className="set-form__actions">
            <Button
              size="sm"
              variant="secondary"
              leadingIcon="sync"
              onClick={() => {
                void load();
              }}
            >
              Повторить
            </Button>
          </div>
        </>
      ) : null}

      {sources !== null && sources.length === 0 ? (
        <p className="set-note">Сервер не отдал ни одного источника веса.</p>
      ) : null}

      {sources !== null && sources.length > 0 ? (
        <div className="set-srcs">
          {sources.map((source) => {
            const copy = sourceCopy(source);
            const nameId = `set-src-${source.key}-name`;
            const hintId = `set-src-${source.key}-hint`;
            const isImport = source.kind === 'import';
            const busy = pendingKey === source.key;
            return (
              <div
                key={source.key}
                className="set-src"
                data-enabled={source.enabled ? 'true' : 'false'}
              >
                <button
                  type="button"
                  role="switch"
                  className="set-sw"
                  aria-checked={source.enabled}
                  aria-labelledby={nameId}
                  aria-describedby={hintId}
                  data-pending={busy ? 'true' : undefined}
                  aria-busy={busy || undefined}
                  onClick={() => {
                    void toggleSource(source);
                  }}
                >
                  <span className="set-sw__thumb" aria-hidden="true" />
                </button>

                <div className="set-src__main">
                  <span className="set-src__name" id={nameId}>
                    {copy.name}
                  </span>
                  <span className="set-src__hint" id={hintId}>
                    {copy.hint}
                  </span>
                  <span className="set-src__sync">
                    Последняя синхронизация:{' '}
                    <span className="set-src__sync-value">{formatLastSync(source.lastSync)}</span>
                  </span>

                  {source.kind === 'manual' ? (
                    <>
                      <form
                        className="set-manual"
                        noValidate
                        onSubmit={(event) => {
                          void submitDraft(event);
                        }}
                      >
                        <Field label="Дата" htmlFor="set-weight-date">
                          <DateInput
                            id="set-weight-date"
                            max={today}
                            value={draft.date}
                            disabled={manualOff}
                            onChange={(event) => {
                              const value = event.target.value;
                              setDraft((prev) => ({ ...prev, date: value }));
                              setDraftError(null);
                            }}
                          />
                        </Field>
                        <Field
                          label="Вес, кг"
                          htmlFor="set-weight-value"
                          className="set-manual__weight"
                        >
                          <Input
                            id="set-weight-value"
                            inputMode="decimal"
                            autoComplete="off"
                            placeholder="79,4"
                            value={draft.weight}
                            disabled={manualOff}
                            onChange={(event) => {
                              const value = event.target.value;
                              setDraft((prev) => ({ ...prev, weight: value }));
                              setDraftError(null);
                            }}
                          />
                        </Field>
                        <div className="set-manual__actions">
                          <Button
                            type="submit"
                            variant="secondary"
                            size="sm"
                            loading={savingDraft}
                            disabled={manualOff}
                          >
                            Добавить
                          </Button>
                        </div>
                      </form>
                      {draftError ? (
                        <p className="set-src__error" role="alert">
                          {draftError}
                        </p>
                      ) : null}
                      {manualOff ? (
                        <p className="set-src__hint">
                          Приём выключен — включите тумблер, чтобы записывать вес.
                        </p>
                      ) : null}
                    </>
                  ) : null}

                  {rowErrors[source.key] ? (
                    <p className="set-src__error" role="alert">
                      {rowErrors[source.key]}
                    </p>
                  ) : null}

                  {isImport && !source.enabled ? (
                    <p className="set-src__hint">Приём выключен — включите тумблер, чтобы загрузить файл.</p>
                  ) : null}
                </div>

                {isImport ? (
                  <div className="set-src__side">
                    {importingKey === source.key ? (
                      <Button size="sm" loading>
                        Импортируем
                      </Button>
                    ) : (
                      <label
                        className="ui-btn ui-btn--secondary ui-btn--sm set-src__file"
                        data-disabled={source.enabled ? undefined : 'true'}
                      >
                        <Icon name="upload" size={14} />
                        <span className="ui-btn__label">Импортировать файл</span>
                        <input
                          type="file"
                          className="set-src__input"
                          accept=".csv,text/csv"
                          disabled={!source.enabled}
                          onChange={(event) => {
                            const file = event.target.files?.[0] ?? null;
                            event.target.value = '';
                            if (file) void importFile(source, file);
                          }}
                        />
                      </label>
                    )}
                  </div>
                ) : null}
              </div>
            );
          })}
          <p className="set-srcs__note">
            Пока это импорт файла: экспортируйте вес из приложения весов в CSV и загрузите сюда.
            Автоматической синхронизации нет — файл выбираете вы, дальше браузера он не уходит.
            Повторная загрузка того же файла новых записей не создаёт.
          </p>
        </div>
      ) : null}
    </Card>
  );
}

/**
 * Настройки: оформление, подключения, данные и «О программе»
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
        if (alive) setError(errorText(problem));
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

  function saveToken(event: FormEvent<HTMLFormElement>): void {
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

      <ConnectionsCard />

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
        <Card.Header title="Сервисы" subtitle="Только факты: секреты сервер не отдаёт" />
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

      <p className="set-foot">
        Токены и ключи здесь не печатаются: сервер отдаёт только признак «настроено».
      </p>
    </div>
  );
}
