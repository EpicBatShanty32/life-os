import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../components/Icon';
import { formatNumber, pluralRu } from '../lib/format';
import { STORAGE_KEYS, readRaw } from '../lib/storage';
import '../styles/analytics.css';

/** Статусы пары из GET /api/analytics/correlations (MASTER_SPEC §2.5). */
type PairStatus = 'ok' | 'weak' | 'insufficient_data' | 'no_variance';

interface CorrelationPair {
  left: string;
  right: string;
  label: string;
  leftLabel: string;
  rightLabel: string;
  n: number;
  r: number | null;
  status: PairStatus;
  text: string;
  sentence: string;
  enoughData: boolean;
}

interface CorrelationsPayload {
  days: number;
  minDays: number;
  minR: number;
  pairs: CorrelationPair[];
}

const PERIODS = [30, 90, 180] as const;
const PAIR_STATUSES: readonly PairStatus[] = ['ok', 'weak', 'insufficient_data', 'no_variance'];
const STATUS_LABELS: Record<PairStatus, string> = {
  ok: 'связь есть',
  weak: 'слабая связь',
  insufficient_data: 'мало данных',
  no_variance: 'нет разброса',
};
const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');
const REQUEST_TIMEOUT_MS = 6000;
const DEFAULT_MIN_DAYS = 14;
const DEFAULT_MIN_R = 0.3;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function readString(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.trim() !== '' ? value : fallback;
}

function readStatus(value: unknown): PairStatus {
  return typeof value === 'string' && (PAIR_STATUSES as readonly string[]).includes(value)
    ? (value as PairStatus)
    : 'insufficient_data';
}

function normalizePair(raw: unknown, index: number): CorrelationPair | null {
  if (!isRecord(raw)) return null;
  const left = readString(raw.left, `left-${index}`);
  const right = readString(raw.right, `right-${index}`);
  const coefficient = raw.r;
  return {
    left,
    right,
    label: readString(raw.label, `${left} ↔ ${right}`),
    leftLabel: readString(raw.left_label, left),
    rightLabel: readString(raw.right_label, right),
    n: Math.max(0, Math.round(readNumber(raw.n, 0))),
    r: typeof coefficient === 'number' && Number.isFinite(coefficient) ? coefficient : null,
    status: readStatus(raw.status),
    text: readString(raw.text, ''),
    sentence: readString(raw.sentence, ''),
    enoughData: raw.enough_data === true,
  };
}

/** Терпимая к контракту нормализация: сервер может отдать не все поля. */
function normalizePayload(raw: unknown, requestedDays: number): CorrelationsPayload {
  if (!isRecord(raw)) throw new Error('ответ не является объектом');
  const pairsRaw = Array.isArray(raw.pairs) ? raw.pairs : [];
  const pairs = pairsRaw.flatMap((item, index) => {
    const pair = normalizePair(item, index);
    return pair ? [pair] : [];
  });
  return {
    days: Math.max(1, Math.round(readNumber(raw.days, requestedDays))),
    minDays: Math.max(1, Math.round(readNumber(raw.min_days, DEFAULT_MIN_DAYS))),
    minR: Math.abs(readNumber(raw.min_r, DEFAULT_MIN_R)),
    pairs,
  };
}

async function fetchCorrelations(days: number, signal: AbortSignal): Promise<CorrelationsPayload> {
  const headers = new Headers({ Accept: 'application/json' });
  const token = readRaw(STORAGE_KEYS.token);
  if (token) headers.set('X-LifeOS-Token', token);
  const response = await fetch(`${API_BASE}/analytics/correlations?days=${days}`, {
    headers,
    credentials: 'same-origin',
    signal,
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) throw new Error('ответ не JSON');
  return normalizePayload((await response.json()) as unknown, days);
}

function daysWord(count: number): string {
  return `${count} ${pluralRu(count, ['день', 'дня', 'дней'])}`;
}

/** Порог показываем как коэффициент (0.3), а не как русскую дробь. */
function thresholdText(minR: number): string {
  return String(Number(minR.toFixed(2)));
}

function describePair(pair: CorrelationPair, minDays: number, minR: number): string {
  switch (pair.status) {
    case 'ok':
      return pair.text || pair.sentence || 'Связь выше порога.';
    case 'weak':
      return pair.r === null
        ? `Слабая связь, ниже порога ${thresholdText(minR)}`
        : `Слабая связь (r = ${formatNumber(pair.r, 2)}), ниже порога ${thresholdText(minR)}`;
    case 'insufficient_data':
      return `Мало данных: ${pair.n} из ${minDays} дней`;
    case 'no_variance':
      return 'Нет разброса значений';
  }
}

function coefficientWidth(r: number | null): number {
  if (r === null) return 0;
  return Math.min(100, Math.round(Math.abs(r) * 100));
}

export function Analytics() {
  const [days, setDays] = useState<number>(90);
  const [data, setData] = useState<CorrelationsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  const retry = useCallback(() => setReloadTick((tick) => tick + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);

    setLoading(true);
    setError(null);
    setData(null);

    fetchCorrelations(days, controller.signal)
      .then((payload) => {
        if (!alive) return;
        setData(payload);
        setLoading(false);
      })
      .catch((problem: unknown) => {
        if (!alive) return;
        setError(
          timedOut ? 'таймаут запроса' : problem instanceof Error ? problem.message : 'нет ответа',
        );
        setLoading(false);
      })
      .finally(() => window.clearTimeout(timer));

    return () => {
      alive = false;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [days, reloadTick]);

  const pairs = data?.pairs ?? [];
  const minDays = data?.minDays ?? DEFAULT_MIN_DAYS;
  const minR = data?.minR ?? DEFAULT_MIN_R;
  const strongPairs = pairs.filter((pair) => pair.status === 'ok').length;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="page-head__kicker">Life OS · v3</p>
          <h1 className="page-head__title">Аналитика</h1>
          <p className="page-head__sub">Связи между сном, водой, тренировками и весом</p>
        </div>
        <div className="page-head__actions">
          <div className="analytics-period" role="group" aria-label="Период корреляций">
            {PERIODS.map((value) => (
              <button
                className="analytics-period__btn"
                key={value}
                type="button"
                onClick={() => setDays(value)}
                aria-pressed={value === days}
              >
                {value} дней
              </button>
            ))}
          </div>
        </div>
      </header>

      {loading ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>Считаю корреляции за {daysWord(days)}…</span>
        </div>
      ) : null}

      {!loading && error ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>API не ответил ({error}) — корреляции не загрузились.</span>
          <button className="btn btn--ghost" type="button" onClick={retry}>
            <Icon name="sync" size={16} />
            <span>Повторить</span>
          </button>
        </div>
      ) : null}

      {!loading && !error && data && pairs.length === 0 ? (
        <div className="card empty-note" role="status">
          <Icon name="chart" size={20} />
          <span>
            За {daysWord(days)} нет пар для расчёта: в базе пока нет дней со сном, настроением, водой,
            тренировками или весом.
          </span>
          <button className="btn btn--ghost" type="button" onClick={retry}>
            <Icon name="sync" size={16} />
            <span>Повторить</span>
          </button>
        </div>
      ) : null}

      {!loading && !error && pairs.length > 0 ? (
        <section className="analytics" aria-labelledby="correlations-title">
          <header className="analytics__head">
            <div>
              <h2 className="analytics__title" id="correlations-title">
                Корреляции
              </h2>
              <p className="analytics__sub">
                За {daysWord(data?.days ?? days)}: считается только при n ≥ {minDays} дней с данными
                и |r| ≥ {thresholdText(minR)}.
              </p>
            </div>
            <span className="status-pill">
              <span className="status-pill__dot" aria-hidden="true" />
              {strongPairs} из {pairs.length} выше порога
            </span>
          </header>

          <div className="analytics__grid">
            {pairs.map((pair) => (
              <article
                className="card corr"
                data-status={pair.status}
                key={`${pair.left}-${pair.right}`}
              >
                <header className="corr__head">
                  <h3 className="corr__title">{pair.label}</h3>
                  <span className={`corr__badge corr__badge--${pair.status}`}>
                    {STATUS_LABELS[pair.status]}
                  </span>
                </header>

                <div className="corr__metrics">
                  <span className={pair.r === null ? 'corr__r corr__r--empty' : 'corr__r'}>
                    <span className="corr__r-label">r</span>
                    <strong>{pair.r === null ? '—' : formatNumber(pair.r, 2)}</strong>
                  </span>
                  <span className="corr__n">{daysWord(pair.n)} с данными</span>
                </div>

                <div className="corr__meter" aria-hidden="true">
                  <span className="corr__bar" style={{ width: `${coefficientWidth(pair.r)}%` }} />
                </div>

                <p className="corr__text">{describePair(pair, minDays, minR)}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <p className="analytics__foot">Корреляция не доказывает причину.</p>

      <p className="page-foot">
        <a className="btn btn--ghost" href="#/body">
          <Icon name="scale" size={16} />
          <span>Смотреть вес и состав тела</span>
        </a>
      </p>
    </div>
  );
}
