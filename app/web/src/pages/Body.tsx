import '../styles/body.css';

import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../components/Icon';
import type { IconName } from '../components/Icon';
import { KpiCard } from '../components/KpiCard';
import { formatDateLong, formatDateShort, formatNumber, parseIsoDate, pluralRu } from '../lib/format';
import type { Accent } from '../lib/ui';

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');
const DAYS = 90;
const WINDOW = 7;
const TIMEOUT_MS = 6000;
const DEFAULT_WINDOWS = [7, 30, 90];

interface WeightPoint {
  date: string;
  value: number;
  ema: number | null;
}

interface Forecast {
  status: string;
  target_kg: number | null;
  rate_per_week: number | null;
  date: string | null;
  weeks: number | null;
  current: number | null;
}

interface WeightPayload {
  days: number;
  window: number;
  unit: string;
  current: number | null;
  ema: number | null;
  delta: number | null;
  target_kg: number | null;
  rate_per_week: number | null;
  forecast: Forecast | null;
  points: WeightPoint[];
}

interface CompositionItem {
  key: string;
  label: string;
  unit: string;
  current: number | null;
  deltas: Record<string, number | null>;
  counts: Record<string, number>;
}

interface CompositionPayload {
  windows: number[];
  items: CompositionItem[];
}

interface SectionItem {
  id: number | string;
  date: string;
  time: string;
  kind: string;
  kindLabel: string;
  text: string;
  source?: string;
}

interface SectionPayload {
  name: string;
  title: string;
  note: string;
  days: number;
  since: string;
  today: string;
  items: SectionItem[];
  empty: boolean;
  count?: number;
  kpis?: unknown[];
  series?: unknown[];
}

type Tone = 'good' | 'warn' | 'neutral';

const COMPOSITION_META: Record<string, { label: string; unit: string; icon: IconName; accent: Accent }> = {
  fat: { label: 'Жир', unit: '%', icon: 'apple', accent: 'peach' },
  muscle: { label: 'Мышцы', unit: '%', icon: 'dumbbell', accent: 'green' },
  body_water: { label: 'Вода в теле', unit: '%', icon: 'droplet', accent: 'blue' },
};

const COMPOSITION_KEYS = ['fat', 'muscle', 'body_water'];

const FORECAST_MESSAGES: Record<string, { title: string; note: string; tone: Tone }> = {
  reached: {
    title: 'Цель достигнута',
    note: 'Вес на целевом уровне — дата достижения не нужна.',
    tone: 'good',
  },
  flat: {
    title: 'Вес стоит, прогноза нет',
    note: 'Темп меньше 0,05 кг в неделю: дата цели не рассчитывается.',
    tone: 'warn',
  },
  insufficient_data: {
    title: 'Мало данных для прогноза',
    note: 'Нужно минимум 5 дней с весом в последних 30 днях.',
    tone: 'warn',
  },
  no_target: {
    title: 'Цель по весу не задана',
    note: 'Добавьте цель в килограммах — появится расчётная дата.',
    tone: 'neutral',
  },
  away_from_target: {
    title: 'Движение идёт от цели',
    note: 'Текущий темп уводит вес дальше от целевого значения.',
    tone: 'warn',
  },
};

const FORECAST_BADGES: Record<string, string> = {
  ok: 'по тренду',
  reached: 'достигнуто',
  flat: 'без тренда',
  insufficient_data: 'мало данных',
  no_target: 'нет цели',
  away_from_target: 'от цели',
};

/** Число или `null` — защита от NaN/undefined в DOM. */
function finite(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function num(value: number | null | undefined, digits = 1): string {
  return finite(value) ? formatNumber(value, digits) : '—';
}

/** Знаковая запись: «−1,6», «+0,4», «0,0». Минус — типографский. */
function signed(value: number | null | undefined, digits = 1): string {
  if (!finite(value)) return '—';
  if (Math.abs(value) < 0.5 * 10 ** -digits) return formatNumber(0, digits);
  return `${value > 0 ? '+' : '−'}${formatNumber(Math.abs(value), digits)}`;
}

function firstFinite(...values: Array<number | null | undefined>): number | null {
  for (const value of values) {
    if (finite(value)) return value;
  }
  return null;
}

function isIsoDate(value: string | null | undefined): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function readError(reason: unknown): string {
  if (reason instanceof Error && reason.message) return reason.message;
  return 'нет ответа';
}

async function getJson<T>(path: string, outer: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  outer.addEventListener('abort', abort, { once: true });
  const timer = window.setTimeout(abort, TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return (await response.json()) as T;
  } catch (problem) {
    if (outer.aborted) throw problem;
    if (controller.signal.aborted) throw new Error(`нет ответа за ${TIMEOUT_MS / 1000} с`);
    throw problem;
  } finally {
    window.clearTimeout(timer);
    outer.removeEventListener('abort', abort);
  }
}

interface WindowChange {
  change: number;
  from: string;
  to: string;
}

/** Изменение веса внутри окна: от первого до последнего замера включительно. */
function changeForDays(points: readonly WeightPoint[], days: number): WindowChange | null {
  const valid = points.filter((point) => finite(point.value));
  if (valid.length < 2) return null;
  const last = valid[valid.length - 1];
  const start = parseIsoDate(last.date);
  start.setDate(start.getDate() - (days - 1));
  const subset = valid.filter((point) => parseIsoDate(point.date) >= start);
  if (subset.length < 2) return null;
  const first = subset[0];
  return {
    change: subset[subset.length - 1].value - first.value,
    from: first.date,
    to: last.date,
  };
}

function deltaTone(key: string, value: number | null | undefined): Tone {
  if (!finite(value) || Math.abs(value) < 0.05) return 'neutral';
  // Вода в теле колеблется без «хорошего» направления — не красим.
  if (key === 'body_water') return 'neutral';
  const grows = value > 0;
  if (key === 'muscle') return grows ? 'good' : 'warn';
  return grows ? 'warn' : 'good';
}

const CHART_WIDTH = 760;
const CHART_HEIGHT = 268;
const CHART_PAD = { top: 24, right: 18, bottom: 30, left: 48 };

function WeightChart({ points, unit }: { points: readonly WeightPoint[]; unit: string }) {
  const valid = points.filter((point) => finite(point.value));
  if (valid.length < 2) {
    return <p className="empty-note body-chart__empty">Мало данных для графика</p>;
  }

  const values: number[] = [];
  for (const point of valid) {
    values.push(point.value);
    const ema = point.ema;
    if (finite(ema)) values.push(ema);
  }

  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const pad = Math.max((rawMax - rawMin) * 0.12, 0.2);
  const low = rawMin - pad;
  const high = rawMax + pad;
  const innerWidth = CHART_WIDTH - CHART_PAD.left - CHART_PAD.right;
  const innerHeight = CHART_HEIGHT - CHART_PAD.top - CHART_PAD.bottom;
  const xAt = (index: number): number => CHART_PAD.left + (index * innerWidth) / (valid.length - 1);
  const yAt = (value: number): number =>
    CHART_PAD.top + (1 - (value - low) / (high - low)) * innerHeight;

  const weightPath = valid
    .map(
      (point, index) =>
        `${index === 0 ? 'M' : 'L'}${xAt(index).toFixed(1)},${yAt(point.value).toFixed(1)}`,
    )
    .join(' ');

  const emaCoords: Array<[number, number]> = [];
  valid.forEach((point, index) => {
    const ema = point.ema;
    if (finite(ema)) emaCoords.push([xAt(index), yAt(ema)]);
  });
  const emaPath = emaCoords
    .map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(' ');

  const first = valid[0];
  const last = valid[valid.length - 1];
  const ariaLabel = `График веса и EMA: ${valid.length} ${pluralRu(valid.length, [
    'точка',
    'точки',
    'точек',
  ])} с ${formatDateShort(first.date)} по ${formatDateShort(last.date)}, от ${num(
    rawMin,
    1,
  )} до ${num(rawMax, 1)} ${unit}`;

  return (
    <figure className="body-chart">
      <svg
        className="body-chart__svg"
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={ariaLabel}
      >
        <title>Вес и EMA за выбранный период</title>
        {[0, 1 / 3, 2 / 3, 1].map((fraction) => (
          <line
            key={fraction}
            className="body-chart__grid"
            x1={CHART_PAD.left}
            x2={CHART_WIDTH - CHART_PAD.right}
            y1={CHART_PAD.top + innerHeight * fraction}
            y2={CHART_PAD.top + innerHeight * fraction}
          />
        ))}
        <text className="body-chart__axis-label" x={CHART_PAD.left} y={CHART_PAD.top - 10}>
          макс {num(rawMax, 1)} {unit}
        </text>
        <text
          className="body-chart__axis-label"
          x={CHART_PAD.left}
          y={CHART_HEIGHT - 8}
        >
          мин {num(rawMin, 1)} {unit}
        </text>

        <path className="body-chart__line body-chart__line--weight" d={weightPath} />
        {emaPath ? (
          <path className="body-chart__line body-chart__line--ema" d={emaPath} />
        ) : null}

        {valid.map((point, index) => (
          <circle
            key={`${point.date}-${index}`}
            className="body-chart__dot"
            cx={xAt(index)}
            cy={yAt(point.value)}
            r={1.9}
          >
            <title>
              {`${formatDateShort(point.date)} · вес ${num(point.value, 1)} ${unit}${
                finite(point.ema) ? ` · EMA ${num(point.ema, 1)} ${unit}` : ''
              }`}
            </title>
          </circle>
        ))}
      </svg>

      <figcaption className="body-chart__caption">
        <span>
          {formatDateShort(first.date)} — {formatDateShort(last.date)}
        </span>
        <span>
          {valid.length} {pluralRu(valid.length, ['точка', 'точки', 'точек'])}
        </span>
      </figcaption>
    </figure>
  );
}

function CompositionCard({ item, windows }: { item: CompositionItem; windows: readonly number[] }) {
  const meta = COMPOSITION_META[item.key];
  const label = item.label || meta?.label || 'Показатель';
  const unit = item.unit || meta?.unit || '';
  const icon = meta?.icon ?? 'scale';
  const accent = meta?.accent ?? 'violet';
  const current = finite(item.current) ? item.current : null;

  return (
    <article className="card body-comp__card">
      <header className="body-comp__head">
        <span className="widget__icon" data-accent={accent} aria-hidden="true">
          <Icon name={icon} size={17} />
        </span>
        <h3 className="body-comp__label">{label}</h3>
      </header>

      {current === null ? (
        <p className="body-comp__empty">мало данных</p>
      ) : (
        <p className="body-comp__value">
          <strong>{formatNumber(current, 1)}</strong>
          {unit ? <span className="body-comp__unit">{unit}</span> : null}
        </p>
      )}

      <div className="body-comp__deltas">
        {windows.map((window) => {
          const delta = item.deltas[String(window)] ?? null;
          const count = item.counts[String(window)];
          const tone = deltaTone(item.key, delta);
          return (
            <span
              className={`body-delta body-delta--${tone}`}
              key={window}
              title={
                typeof count === 'number'
                  ? `${window} дн. · ${count} ${pluralRu(count, ['замер', 'замера', 'замеров'])}`
                  : `${window} дн.`
              }
            >
              <span className="body-delta__label">{window} дн.</span>
              <span className="body-delta__value">
                {finite(delta) ? signed(delta, 1) : 'мало данных'}
              </span>
            </span>
          );
        })}
      </div>
    </article>
  );
}

function ForecastCard({
  forecast,
  weight,
}: {
  forecast: Forecast | null;
  weight: WeightPayload | null;
}) {
  const target = firstFinite(forecast?.target_kg, weight?.target_kg);
  const rate = firstFinite(forecast?.rate_per_week, weight?.rate_per_week);
  const current = firstFinite(forecast?.current, weight?.current);
  const weeks = firstFinite(forecast?.weeks);
  const due = isIsoDate(forecast?.date) ? forecast.date : null;
  const status = forecast?.status ?? 'insufficient_data';
  const hasDate = status === 'ok' && due !== null;
  const message = hasDate
    ? {
        title: '',
        note: 'Ожидаемая дата по темпу последних 30 дней — прогноз, а не обещание.',
        tone: 'good' as Tone,
      }
    : FORECAST_MESSAGES[status] ?? FORECAST_MESSAGES.insufficient_data;
  const badge = hasDate ? 'по тренду' : FORECAST_BADGES[status] ?? 'нет прогноза';
  const remaining = finite(target) && finite(current) ? target - current : null;

  return (
    <section className="card body-forecast" aria-labelledby="body-forecast-title">
      <header className="card__head">
        <div>
          <h2 className="card__title" id="body-forecast-title">
            Прогноз цели
          </h2>
          <p className="card__sub">Расчёт по тренду последних 30 дней — без выдуманных дат.</p>
        </div>
        <span className={`body-forecast__badge body-forecast__badge--${message.tone}`}>{badge}</span>
      </header>

      <div className="body-forecast__body">
        <div className="body-forecast__main">
          <p className="body-forecast__value">
            {hasDate && due !== null ? (
              <>
                {formatDateShort(due)}
                {finite(weeks) ? (
                  <span className="body-forecast__weeks">
                    ≈ {formatNumber(weeks, 1)}{' '}
                    {pluralRu(Math.round(weeks), ['неделя', 'недели', 'недель'])}
                  </span>
                ) : null}
              </>
            ) : (
              message.title
            )}
          </p>
          <p className="body-forecast__note">{message.note}</p>
        </div>

        <dl className="body-forecast__facts">
          <div>
            <dt>Цель</dt>
            <dd>{finite(target) ? `${formatNumber(target, 1)} кг` : 'не задана'}</dd>
          </div>
          <div>
            <dt>Темп</dt>
            <dd>{finite(rate) ? `${signed(rate, 2)} кг/нед` : '—'}</dd>
          </div>
          <div>
            <dt>Текущий вес</dt>
            <dd>{finite(current) ? `${formatNumber(current, 1)} кг` : '—'}</dd>
          </div>
          {hasDate && finite(remaining) && Math.abs(remaining) >= 0.05 ? (
            <div>
              <dt>Осталось</dt>
              <dd>{formatNumber(Math.abs(remaining), 1)} кг</dd>
            </div>
          ) : null}
        </dl>
      </div>
    </section>
  );
}

export function Body() {
  const [weight, setWeight] = useState<WeightPayload | null>(null);
  const [composition, setComposition] = useState<CompositionPayload | null>(null);
  const [section, setSection] = useState<SectionPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    void (async () => {
      const [weightResult, compositionResult, sectionResult] = await Promise.allSettled([
        getJson<WeightPayload>(
          `/health/weight?days=${DAYS}&window=${WINDOW}`,
          controller.signal,
        ),
        getJson<CompositionPayload>('/health/composition', controller.signal),
        getJson<SectionPayload>(`/sections/body?days=${DAYS}&limit=50`, controller.signal),
      ] as const);
      if (controller.signal.aborted) return;

      const problems: string[] = [];
      if (weightResult.status === 'fulfilled') setWeight(weightResult.value);
      else problems.push(readError(weightResult.reason));
      if (compositionResult.status === 'fulfilled') setComposition(compositionResult.value);
      else problems.push(readError(compositionResult.reason));
      if (sectionResult.status === 'fulfilled') setSection(sectionResult.value);
      else problems.push(readError(sectionResult.reason));

      if (problems.length === 0) setError(null);
      else if (problems.length === 1) setError(problems[0]);
      else {
        const rest = problems.length - 1;
        setError(`${problems[0]} и ещё ${rest} ${pluralRu(rest, ['запрос', 'запроса', 'запросов'])}`);
      }
      setLoading(false);
    })();

    return () => controller.abort();
  }, [reloadToken]);

  const points = weight?.points ?? [];
  const unit = weight?.unit || 'кг';
  const change30 = changeForDays(points, 30);
  const windows = composition?.windows?.length ? composition.windows : DEFAULT_WINDOWS;
  const compositionItems: CompositionItem[] = composition?.items?.length
    ? composition.items
    : COMPOSITION_KEYS.map((key) => {
        const meta = COMPOSITION_META[key];
        return {
          key,
          label: meta.label,
          unit: meta.unit,
          current: null,
          deltas: {},
          counts: {},
        };
      });
  const items = section?.items ?? [];
  const initialLoading = loading && weight === null && composition === null && section === null;
  const hasData = weight !== null || composition !== null || section !== null;

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-head__text">
          <p className="page-head__kicker">Life OS · v3</p>
          <h1 className="page-head__title">Тело и весы</h1>
          <p className="page-head__sub">
            Вес, EMA и состав тела за {DAYS} дней. Сегодня — {formatDateLong()}.
          </p>
        </div>
        <div className="page-head__actions">
          <span className="status-pill">
            <span className="status-pill__dot" aria-hidden="true" />
            {loading
              ? 'Обновляю'
              : error
                ? 'Нет связи'
                : points.length > 0
                  ? `${points.length} ${pluralRu(points.length, ['день', 'дня', 'дней'])} с весом`
                  : 'Мало данных'}
          </span>
        </div>
      </header>

      {error ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>
            API не ответил ({error}).{' '}
            {hasData ? 'Показываю то, что уже загружено.' : 'Проверьте сервер и повторите.'}
          </span>
          <button className="btn btn--ghost" type="button" onClick={reload}>
            Повторить
          </button>
        </div>
      ) : null}

      {initialLoading ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>Загружаю тело…</span>
        </div>
      ) : null}

      {!initialLoading ? (
        <>
          <section className="bento body-kpis" aria-label="Показатели веса">
            <KpiCard
              icon="scale"
              accent="violet"
              label="Текущий вес"
              value={num(weight?.current, 1)}
              unit={unit}
              hint={points.length > 0 ? 'Последний замер в периоде' : 'Мало данных: нет замеров веса'}
            />
            <KpiCard
              icon="chart"
              accent="green"
              label={`EMA · окно ${weight?.window ?? WINDOW}`}
              value={num(weight?.ema, 1)}
              unit={unit}
              hint="Сглаженный тренд по дневным средним"
            />
            <article className="card kpi body-kpi">
              <header className="kpi__head">
                <span className="kpi__icon" aria-hidden="true">
                  <Icon name={change30 && change30.change > 0 ? 'trend-up' : 'trend-down'} size={18} />
                </span>
                <span className="kpi__label">Изменение за 30 дней</span>
              </header>
              <p
                className={`kpi__value body-kpi__value body-kpi__value--${
                  !change30 ? 'neutral' : change30.change > 0.05 ? 'warn' : change30.change < -0.05 ? 'good' : 'neutral'
                }`}
              >
                <strong>{change30 ? signed(change30.change, 1) : '—'}</strong>
                {change30 ? <span className="kpi__unit">{unit}</span> : null}
              </p>
              <p className="kpi__hint">
                {change30
                  ? `${formatDateShort(change30.from)} — ${formatDateShort(change30.to)} · по дневным средним`
                  : 'Мало данных для дельты за 30 дней'}
              </p>
            </article>
            <KpiCard
              icon="target"
              accent="blue"
              label="Цель по весу"
              value={num(weight?.target_kg, 1)}
              unit={weight?.target_kg === null || weight?.target_kg === undefined ? undefined : 'кг'}
              hint={finite(weight?.target_kg) ? 'Из активной цели в килограммах' : 'Цель по весу не задана'}
            />
            <KpiCard
              icon="chart"
              accent="peach"
              label="Прогноз темпа"
              value={signed(weight?.rate_per_week, 2)}
              unit="кг/нед"
              hint={finite(weight?.rate_per_week) ? 'По тренду последних 30 дней' : 'Мало данных для прогноза'}
            />
          </section>

          <ForecastCard forecast={weight?.forecast ?? null} weight={weight} />

          <section className="card card--chart" aria-labelledby="body-chart-title">
            <header className="card__head">
              <div>
                <h2 className="card__title" id="body-chart-title">
                  EMA-график веса
                </h2>
                <p className="card__sub">
                  Дневные замеры и сглаженная линия (окно {weight?.window ?? WINDOW} дней) за{' '}
                  {DAYS} дней.
                </p>
              </div>
              <div className="body-chart__legend">
                <span className="body-chart__legend-item">
                  <span className="body-chart__swatch body-chart__swatch--weight" aria-hidden="true" />
                  Вес
                </span>
                <span className="body-chart__legend-item">
                  <span className="body-chart__swatch body-chart__swatch--ema" aria-hidden="true" />
                  EMA
                </span>
              </div>
            </header>

            <WeightChart points={points} unit={unit} />

            {finite(weight?.delta) ? (
              <p className="body-chart__summary">
                Изменение за {DAYS} дней: {signed(weight?.delta, 1)} {unit}
              </p>
            ) : null}
          </section>

          <section className="body-block" aria-labelledby="body-composition-title">
            <header className="body-block__head">
              <h2 className="card__title" id="body-composition-title">
                Состав тела
              </h2>
              <p className="card__sub">Жир, мышцы и вода — по замерам из приложения и бота.</p>
            </header>

            <div className="body-comp">
              {compositionItems.map((item) => (
                <CompositionCard item={item} windows={windows} key={item.key} />
              ))}
            </div>

            <p className="body-notes">Дельты считаются по замерам внутри окна.</p>
          </section>

          <section className="card" aria-labelledby="body-records-title">
            <header className="card__head">
              <div>
                <h2 className="card__title" id="body-records-title">
                  Замеры и записи
                </h2>
                <p className="card__sub">
                  {items.length > 0
                    ? `${items.length} ${pluralRu(items.length, ['запись', 'записи', 'записей'])} за ${DAYS} дней`
                    : 'Записи о теле из бота и быстрых замеров.'}
                </p>
              </div>
            </header>

            {items.length === 0 ? (
              <p className="empty-note">
                Пока нет записей о теле — добавьте через бота или быструю запись
              </p>
            ) : (
              <ol className="item-list">
                {items.map((item) => (
                  <li className="item" key={item.id}>
                    <span className="item__date">
                      <strong>{formatDateShort(item.date)}</strong>
                      <small>{item.time || '—'}</small>
                    </span>
                    <span className="item__kind">{item.kindLabel || 'Тело'}</span>
                    <span className="item__text">{item.text || '—'}</span>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}
