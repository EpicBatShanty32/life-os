/**
 * «Аналитика» — сводная страница (DESIGN-v4 §4, handoff §10.6).
 *
 * Два запроса: /api/analytics/summary на блоки окна и
 * /api/analytics/findings на связи. Находки — не таблица с порогом |r|:
 * показываем до пяти карточек с лагом, r, ρ, n, q и человеческим текстом,
 * а когда связей нет — честный список того, чего не хватает.
 * Пончиков нет: линии, бары и строки.
 */
import { useMemo, useState } from 'react';
import { useAnalyticsFindings, useAnalyticsSummary } from '../api/analytics_v4';
import type {
  AnalyticsFinding,
  AnalyticsFindings,
  AnalyticsSummary,
  EnergyPoint,
  FinanceDay,
  HabitDay,
  NotReadyPair,
} from '../api/analytics_v4';
import { seriesTitle } from '../api/analytics_v4';
import { dataColor, formatPeriodRange, progressTone } from '../api/personal';
import { Button, EmptyState, ProgressBar, Section, Segmented, Skeleton } from '../components/ui';
import { clamp, formatDateShort, formatNumber, pluralRu, toLocalIso } from '../lib/format';
import '../styles/pages-insight.css';

const PERIODS = [30, 90, 180] as const;

/* ── Ряды: сутки или недели, если окно большое ───────────── */

interface Bucket {
  start: number;
  size: number;
}

function buckets(length: number, days: number): Bucket[] {
  if (days <= 90 || length === 0) {
    return Array.from({ length }, (_, index) => ({ start: index, size: 1 }));
  }
  const result: Bucket[] = [];
  for (let index = 0; index < length; index += 7) {
    result.push({ start: index, size: Math.min(7, length - index) });
  }
  return result;
}

type HabitBucket = HabitDay;
type MoneyBucket = FinanceDay;

function aggregateHabits(series: HabitDay[], days: number): HabitBucket[] {
  return buckets(series.length, days).flatMap((bucket): HabitBucket[] => {
    const chunk = series.slice(bucket.start, bucket.start + bucket.size);
    const head = chunk[0];
    if (!head) return [];
    const sum = chunk.reduce((total, item) => total + item.pct, 0);
    return [
      {
        date: head.date,
        marks: Math.round(chunk.reduce((total, item) => total + item.marks, 0) / chunk.length),
        pct: sum / chunk.length,
      },
    ];
  });
}

function aggregateEnergy(series: EnergyPoint[], days: number): EnergyPoint[] {
  return buckets(series.length, days).flatMap((bucket): EnergyPoint[] => {
    const chunk = series.slice(bucket.start, bucket.start + bucket.size);
    const head = chunk[0];
    if (!head) return [];
    const values = chunk.flatMap((item) => (item.value === null ? [] : [item.value]));
    const sum = values.reduce((total, value) => total + value, 0);
    return [{ date: head.date, value: values.length ? sum / values.length : null }];
  });
}

function aggregateMoney(series: FinanceDay[], days: number): MoneyBucket[] {
  return buckets(series.length, days).flatMap((bucket): MoneyBucket[] => {
    const chunk = series.slice(bucket.start, bucket.start + bucket.size);
    const head = chunk[0];
    if (!head) return [];
    return [
      {
        date: head.date,
        income: chunk.reduce((total, item) => total + item.income, 0),
        expense: chunk.reduce((total, item) => total + item.expense, 0),
      },
    ];
  });
}

/* ── Энергия: тонкая линия 1.5 px с разрывами ────────────── */

const CHART_W = 720;
const CHART_H = 132;
const PAD_X = 10;
const TOP_Y = 10;
const BASE_Y = 112;

function energyY(value: number): number {
  return BASE_Y - ((clamp(value, 1, 5) - 1) / 4) * (BASE_Y - TOP_Y);
}

interface Dot {
  x: number;
  y: number;
  point: EnergyPoint;
}

function buildEnergy(points: readonly EnergyPoint[]): { paths: string[]; dots: Dot[] } {
  const step = points.length > 1 ? (CHART_W - PAD_X * 2) / (points.length - 1) : 0;
  const paths: string[] = [];
  const dots: Dot[] = [];
  let current: string[] = [];

  points.forEach((point, index) => {
    const x = PAD_X + index * step;
    if (point.value === null) {
      if (current.length > 1) paths.push(current.join(' '));
      current = [];
      return;
    }
    const y = energyY(point.value);
    current.push(`${current.length === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`);
    dots.push({ x, y, point });
  });
  if (current.length > 1) paths.push(current.join(' '));
  return { paths, dots };
}

function EnergyChart({
  summary,
  days,
}: {
  summary: AnalyticsSummary;
  days: number;
}) {
  const raw = summary.energy.series;
  const points = useMemo(() => aggregateEnergy(raw, days), [raw, days]);
  const { paths, dots } = useMemo(() => buildEnergy(points), [points]);
  const withData = summary.energy.daysWithData;
  const avg = summary.energy.avg;
  const perWeek = days > 90;
  const showDots = points.length <= 90;

  if (points.length === 0 || withData === 0) {
    return (
      <EmptyState
        title="Энергии пока нет"
        description="Линия появится, когда будут чек-ины: пять уровней на экране «Личное»."
        action={
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              window.location.hash = '#/personal';
            }}
          >
            Записать чек-ин
          </Button>
        }
      />
    );
  }

  return (
    <figure className="pi-chart">
      <svg
        className="pi-chart__svg"
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        role="img"
        aria-label={`Энергия по дням за ${days} ${pluralRu(days, ['день', 'дня', 'дней'])}: ${withData} дней с чек-инами, среднее ${
          avg === null ? 'не посчитать' : formatNumber(avg, 1)
        } из 5`}
      >
        {[1, 2, 3, 4, 5].map((level) => (
          <line
            key={level}
            x1={PAD_X}
            x2={CHART_W - PAD_X}
            y1={energyY(level)}
            y2={energyY(level)}
            className="pi-chart__grid"
            data-base={level === 1}
          />
        ))}
        {avg !== null ? (
          <line
            x1={PAD_X}
            x2={CHART_W - PAD_X}
            y1={energyY(avg)}
            y2={energyY(avg)}
            className="pi-chart__avg"
          />
        ) : null}
        {paths.map((path) => (
          <path key={path} d={path} className="pi-chart__line" />
        ))}
        {showDots
          ? dots.map((dot) => (
              <circle
                key={dot.point.date}
                cx={dot.x}
                cy={dot.y}
                r={2.4}
                className="pi-chart__dot"
              >
                <title>{`${formatDateShort(dot.point.date)}: ${dot.point.value} из 5`}</title>
              </circle>
            ))
          : null}
      </svg>
      <figcaption className="pi-chart__foot">
        <span>{formatDateShort(summary.from)}</span>
        <span className="pi-chart__hint">
          {perWeek
            ? 'среднее по неделям, вертикаль 1–5'
            : 'вертикаль 1–5, линия рвётся там, где чек-ина не было'}
        </span>
        <span>{formatDateShort(summary.to)}</span>
      </figcaption>
      <p className="pi-chart__caption">
        {withData} {pluralRu(withData, ['день', 'дня', 'дней'])} с чек-инами из {summary.days}
        {avg === null ? '' : `, средняя ${formatNumber(avg, 1)}`}
      </p>
    </figure>
  );
}

/* ── Деньги: два ряда баров в одной шкале ─────────────────── */

function MoneyBars({ summary, days }: { summary: AnalyticsSummary; days: number }) {
  const rows = useMemo(
    () => aggregateMoney(summary.finance.series, days),
    [summary.finance.series, days],
  );
  const max = Math.max(1, ...rows.flatMap((row) => [row.income, row.expense]));
  const perWeek = days > 90;
  if (rows.length === 0) {
    return (
      <EmptyState
        title="Транзакций в окне нет"
        description="Добавьте доход или расход — здесь появятся дневные бары."
        action={
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              window.location.hash = '#/finance';
            }}
          >
            Открыть финансы
          </Button>
        }
      />
    );
  }

  return (
    <div className="pi-money-chart">
      <div
        className="pi-bars"
        role="img"
        aria-label={`Доход и расход ${perWeek ? 'по неделям' : 'по дням'}: доход ${formatNumber(
          Math.round(summary.finance.income),
        )} рублей, расход ${formatNumber(Math.round(summary.finance.expense))} рублей`}
      >
        {rows.map((row) => (
          <span
            className="pi-bars__col"
            key={row.date}
            title={`${formatDateShort(row.date)}: доход ${formatNumber(
              Math.round(row.income),
            )} ₽, расход ${formatNumber(Math.round(row.expense))} ₽`}
          >
            <span
              className="pi-bars__bar"
              data-kind="income"
              style={{ height: `${Math.max(row.income > 0 ? 2 : 0, (row.income / max) * 100)}%` }}
            />
            <span
              className="pi-bars__bar"
              data-kind="expense"
              style={{
                height: `${Math.max(row.expense > 0 ? 2 : 0, (row.expense / max) * 100)}%`,
              }}
            />
          </span>
        ))}
      </div>
      <div className="pi-bars__axis">
        <span>{formatDateShort(summary.from)}</span>
        <span className="pi-legend">
          <span className="pi-legend__item">
            <i data-kind="income" />
            доход
          </span>
          <span className="pi-legend__item">
            <i data-kind="expense" />
            расход
          </span>
        </span>
        <span>{formatDateShort(summary.to)}</span>
      </div>
      <p className="pi-chart__caption">
        рубли, {perWeek ? 'по неделям (сумма)' : 'по дням'}; одна шкала для обоих рядов
      </p>
    </div>
  );
}

/* ── Привычки: доля отметок по дням ───────────────────────── */

function HabitBars({ series, days }: { series: readonly HabitDay[]; days: number }) {
  const rows = useMemo(() => aggregateHabits([...series], days), [series, days]);
  if (rows.length === 0) {
    return (
      <p className="pi-quiet" role="status">
        Отметок привычек в окне нет.
      </p>
    );
  }
  return (
    <div className="pi-habitchart">
      <div
        className="pi-habitchart__bars"
        role="img"
        aria-label={`Доля выполненных привычек ${days > 90 ? 'по неделям' : 'по дням'}: в среднем ${Math.round(
          series.reduce((total, item) => total + item.pct, 0) / Math.max(1, series.length),
        )}%`}
      >
        {rows.map((row) => (
          <span
            className="pi-habitchart__bar"
            key={row.date}
            style={{ height: `${Math.max(row.pct > 0 ? 3 : 0, row.pct)}%` }}
            title={`${formatDateShort(row.date)}: ${Math.round(row.pct)}%`}
          />
        ))}
      </div>
      <p className="pi-chart__caption">
        доля отметок от нормы, {days > 90 ? 'по неделям (среднее)' : 'по дням'}
      </p>
    </div>
  );
}

/* ── Страница ─────────────────────────────────────────────── */

export function Analytics() {
  // Окно по умолчанию совпадает с Обзором (90 дней): иначе на экранах
  // висели бы разные наборы находок при одинаковых данных.
  const [days, setDays] = useState<number>(90);
  const { data, loading, error, reload } = useAnalyticsSummary(days);
  const findings = useAnalyticsFindings(days);

  return (
    <div className="pi">
      <header className="pi-head">
        <div className="pi-head__text">
          <p className="pi-head__date">
            {data ? formatPeriodRange(data.from, data.to) : formatPeriodRange(toLocalIso(), toLocalIso())}
          </p>
          <h1 className="pi-head__title">Аналитика</h1>
          <p className="pi-head__sub">
            Энергия, сферы, чтение, деньги и привычки в одном окне — с числом дней и силой связи
          </p>
        </div>
        <div className="pi-head__actions">
          <Segmented
            ariaLabel="Окно аналитики"
            value={String(days)}
            onChange={(value) => setDays(Number(value))}
            options={PERIODS.map((value) => ({ value: String(value), label: `${value} дней` }))}
          />
        </div>
      </header>

      {loading ? (
        <div className="pi-blocks">
          <Skeleton variant="block" label="Считаю сводку" />
          <Skeleton variant="text" lines={3} label="Считаю сводку" />
        </div>
      ) : null}

      {!loading && error ? (
        <p className="pi-quiet" role="status">
          Не удалось загрузить сводку: {error}
          <button type="button" className="pi-link" onClick={() => void reload()}>
            Повторить
          </button>
        </p>
      ) : null}

      {!loading && !error && data ? (
        <>
          <div className="pi-strip" role="group" aria-label="Ключевые числа окна">
            <div className="pi-strip__cell">
              <span className="pi-strip__label">Энергия</span>
              <p className="pi-strip__value">
                {data.energy.avg === null ? '—' : formatNumber(data.energy.avg, 1)}
                {data.energy.avg === null ? null : <span className="pi-strip__unit">/5</span>}
              </p>
              <span className="pi-strip__hint">
                {data.energy.daysWithData} {pluralRu(data.energy.daysWithData, ['день', 'дня', 'дней'])} с
                чек-инами
              </span>
            </div>
            <div className="pi-strip__cell">
              <span className="pi-strip__label">Задачи</span>
              <p className="pi-strip__value">
                {formatNumber(data.tasks.done)}
                <span className="pi-strip__unit">/{formatNumber(data.tasks.total)}</span>
              </p>
              <span className="pi-strip__hint">{Math.round(data.tasks.pct)}% закрыто</span>
            </div>
            <div className="pi-strip__cell">
              <span className="pi-strip__label">Привычки</span>
              <p className="pi-strip__value">{Math.round(data.habits.pct)}%</p>
              <span className="pi-strip__hint">
                {data.habits.marks} отметок за {data.days}{' '}
                {pluralRu(data.days, ['день', 'дня', 'дней'])}
              </span>
            </div>
            <div className="pi-strip__cell">
              <span className="pi-strip__label">Чтение</span>
              <p className="pi-strip__value">{formatNumber(data.reading.pages)}</p>
              <span className="pi-strip__hint">
                страниц, книг завершено: {data.reading.booksDone}
              </span>
            </div>
            <div className="pi-strip__cell">
              <span className="pi-strip__label">Деньги</span>
              <p className="pi-strip__value">
                {formatNumber(Math.round(data.finance.balance))}
                <span className="pi-strip__unit">₽</span>
              </p>
              <span className="pi-strip__hint">доход минус расход за окно</span>
            </div>
          </div>

          <div className="pi-blocks">
            <Section
              title="Энергия"
              description={`Окно ${data.days} ${pluralRu(data.days, ['день', 'дня', 'дней'])}`}
            >
              <EnergyChart summary={data} days={days} />
            </Section>

            <Section
              title="Баланс сфер"
              description="Все задачи окна: закрытые из всех, доля сферы в общем числе"
            >
              {data.spheres.length === 0 ? (
                <EmptyState
                  title="Сфер пока нет"
                  description="Сферы задаются в «Компасе» — без них задачи некуда относить."
                  action={
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => {
                        window.location.hash = '#/compass';
                      }}
                    >
                      Открыть компас
                    </Button>
                  }
                />
              ) : (
                <div className="pi-spheres">
                  {data.spheres.map((sphere) => (
                    <div className="pi-sphere" key={sphere.id}>
                      <span
                        className="pi-sphere__dot"
                        style={{ background: dataColor(sphere.color) }}
                        aria-hidden="true"
                      />
                      <span className="pi-sphere__name" title={sphere.name}>
                        {sphere.name}
                      </span>
                      <span className="pi-sphere__count pi-num">
                        {formatNumber(sphere.done)}/{formatNumber(sphere.total)}
                      </span>
                      <ProgressBar
                        className="pi-sphere__bar"
                        value={sphere.pct}
                        tone={progressTone(sphere.color)}
                        ariaLabel={`${sphere.name}: ${Math.round(sphere.pct)}% задач закрыто`}
                      />
                      <span className="pi-sphere__pct pi-num">{Math.round(sphere.pct)}%</span>
                    </div>
                  ))}
                </div>
              )}
            </Section>

            <Section
              title="Чтение"
              description="Страницы считаются по книгам, завершённым в окне: журнала чтения в схеме нет"
            >
              <div className="pi-rows">
                <div className="pi-row">
                  <span className="pi-row__label">Страниц в окне</span>
                  <span className="pi-row__value pi-num">{formatNumber(data.reading.pages)}</span>
                </div>
                <div className="pi-row">
                  <span className="pi-row__label">Книг завершено</span>
                  <span className="pi-row__value pi-num">
                    {formatNumber(data.reading.booksDone)} в окне,{' '}
                    {formatNumber(data.reading.booksDoneAll)} всего
                  </span>
                </div>
                <div className="pi-row">
                  <span className="pi-row__label">Сейчас читается</span>
                  <span className="pi-row__value pi-num">
                    {formatNumber(data.reading.booksReading)}{' '}
                    {pluralRu(data.reading.booksReading, ['книга', 'книги', 'книг'])}
                  </span>
                </div>
              </div>
              <ProgressBar
                value={data.reading.pages}
                max={Math.max(1, data.reading.pagesAll)}
                tone="cat-4"
                label="страниц окна из всех страниц библиотеки"
                showValue
              />
            </Section>

            <Section
              title="Деньги"
              description={`Доход и расход за окно: ${formatNumber(
                Math.round(data.finance.income),
              )} и ${formatNumber(Math.round(data.finance.expense))} ₽`}
            >
              <div className="pi-rows pi-rows--tight">
                <div className="pi-row">
                  <span className="pi-row__label">Доход</span>
                  <span className="pi-row__value pi-num">
                    {formatNumber(Math.round(data.finance.income))} ₽
                  </span>
                </div>
                <div className="pi-row">
                  <span className="pi-row__label">Расход</span>
                  <span className="pi-row__value pi-num">
                    {formatNumber(Math.round(data.finance.expense))} ₽
                  </span>
                </div>
                <div className="pi-row">
                  <span className="pi-row__label">Разница</span>
                  <span className="pi-row__value pi-num">
                    {formatNumber(Math.round(data.finance.balance))} ₽
                  </span>
                </div>
              </div>
              <MoneyBars summary={data} days={days} />
            </Section>

            <Section
              title="Привычки"
              description={`${data.habits.habits} активных, ${data.habits.marks} отметок за окно`}
            >
              <p className="pi-habitpct">
                <span className="pi-habitpct__value pi-num">
                  {Math.round(data.habits.pct)}%
                </span>
                <span className="pi-habitpct__hint">
                  отметок от нормы «привычки × дни окна»
                </span>
              </p>
              <ProgressBar
                value={data.habits.pct}
                tone="cat-2"
                ariaLabel={`Привычки: ${Math.round(data.habits.pct)}% отметок от нормы`}
              />
              <HabitBars series={data.habits.series} days={days} />
            </Section>

            <Section
              title="Что нашлось"
              description={
                findings.data
                  ? `Проверено ${formatNumber(findings.data.tested)} ${pluralRu(
                      findings.data.tested,
                      ['сочетание', 'сочетания', 'сочетаний'],
                    )} пар и сдвигов за ${findings.data.window.days} ${pluralRu(
                      findings.data.window.days,
                      ['день', 'дня', 'дней'],
                    )}. Связь не доказывает причину.`
                  : 'Пары показателей со сдвигом 0–2 дня, с поправкой на случайные совпадения'
              }
            >
              <FindingsBlock
                state={findings}
                onRetry={() => {
                  void findings.reload();
                }}
              />
            </Section>
          </div>
        </>
      ) : null}
    </div>
  );
}

/* ── Находки: карточки вместо таблицы с порогом ──────────── */

interface FindingsBlockProps {
  state: {
    data: AnalyticsFindings | null;
    loading: boolean;
    error: string | null;
  };
  onRetry: () => void;
}

function FindingsBlock({ state, onRetry }: FindingsBlockProps) {
  if (state.loading) {
    return <Skeleton variant="text" lines={3} label="Считаю связи" />;
  }
  if (state.error) {
    return (
      <p className="pi-quiet" role="status">
        Не удалось посчитать связи: {state.error}
        <button type="button" className="pi-link" onClick={onRetry}>
          Повторить
        </button>
      </p>
    );
  }
  if (!state.data) return null;

  const { found, notReady } = state.data;
  if (found.length === 0) {
    return (
      <div className="pi-findings">
        <p className="pi-findings__empty">
          Пока уверенных связей нет: мало дней или разброса.
        </p>
        {notReady.length > 0 ? <NotReadyList items={notReady} /> : null}
      </div>
    );
  }

  return (
    <div className="pi-findings">
      <ul className="pi-findings__list">
        {found.map((finding) => (
          <FindingCard
            key={`${finding.left}-${finding.right}-${finding.lag}`}
            finding={finding}
          />
        ))}
      </ul>
      {notReady.length > 0 ? (
        <details className="pi-findings__rest">
          <summary>
            Ещё {formatNumber(notReady.length)}{' '}
            {pluralRu(notReady.length, ['пара', 'пары', 'пар'])} без уверенной связи
          </summary>
          <NotReadyList items={notReady} />
        </details>
      ) : null}
    </div>
  );
}

function FindingCard({ finding }: { finding: AnalyticsFinding }) {
  const effect = finding.effectPct === null ? null : Math.round(finding.effectPct);

  return (
    <li className="pi-finding">
      <div className="pi-finding__score">
        <span className="pi-finding__r pi-num">{formatNumber(finding.r, 2)}</span>
        <span className="pi-finding__score-label">r</span>
        <span className="pi-finding__rho pi-num">
          ρ {finding.rho === null ? '—' : formatNumber(finding.rho, 2)}
        </span>
      </div>
      <div className="pi-finding__body">
        <h3 className="pi-finding__title">
          {finding.title ? (
            finding.title
          ) : (
            <>
              {finding.leftLabel} ↔ {finding.rightLabel}
              <span className="pi-finding__lag"> · {lagTitle(finding.lag)}</span>
            </>
          )}
        </h3>
        <p className="pi-finding__meta pi-num">
          {`n ${formatNumber(finding.n)} · q ${formatQ(finding.q)} · связь ${strengthWord(finding)}${
            effect === null ? '' : ` · объясняет ${effect}% разброса`
          }`}
        </p>
        {finding.text ? (
          <p className="pi-finding__text">
            <span className="pi-finding__lead">Что это значит.</span> {finding.text}
          </p>
        ) : null}
        {finding.advice ? (
          <p className="pi-finding__advice">
            <span className="pi-finding__lead">Что проверить.</span> {finding.advice}
          </p>
        ) : null}
      </div>
    </li>
  );
}

/** Сдвиг правого ряда: «тот же день», «на следующий день», «через 2 дня». */
function lagTitle(lag: number): string {
  if (lag === 0) return 'тот же день';
  if (lag === 1) return 'на следующий день';
  return `через ${formatNumber(lag)} ${pluralRu(lag, ['день', 'дня', 'дней'])}`;
}

/** q-value маленькое, поэтому три знака; «0,000» читалось бы как ноль. */
function formatQ(q: number | null): string {
  if (q === null) return '—';
  if (q < 0.001) return '< 0,001';
  return formatNumber(q, 3);
}

/** Сила словами; q у верхней границы 10 % добавляет честное «на грани». */
function strengthWord(finding: AnalyticsFinding): string {
  const base =
    finding.strength === 'strong'
      ? 'сильная'
      : finding.strength === 'moderate'
        ? 'заметная'
        : 'слабая';
  return finding.q !== null && finding.q > 0.05 ? `${base}, на грани` : base;
}

const REASON_TITLES: Readonly<Record<NotReadyPair['reason'], string>> = {
  insufficient_data: 'Не хватает дней',
  no_variance: 'Значения не меняются',
  fdr: 'Данных хватает, но связь не прошла проверку на случайность',
};

function pairTitle(pair: NotReadyPair): string {
  return `${seriesTitle(pair.left)} ↔ ${seriesTitle(pair.right)}`;
}

/**
 * Честный список пар, которые не стали находками. Дни показываем там, где
 * их правда не хватает: «есть 7 дней из 14» — это конкретное действие,
 * а не «порог не достигнут». Пять ближайших пар и одна строка про остальные:
 * простыня из 30 почти одинаковых строк — тот же шум, что и старый порог.
 */
function NotReadyList({ items }: { items: readonly NotReadyPair[] }) {
  const groups = useMemo(() => {
    const sorted = [...items].sort(
      (left, right) =>
        right.daysHave - left.daysHave ||
        pairTitle(left).localeCompare(pairTitle(right), 'ru'),
    );
    const insufficient = sorted.filter((pair) => pair.reason === 'insufficient_data');
    const lacking = insufficient.filter((pair) => pair.daysHave > 0);
    const blank = insufficient.filter((pair) => pair.daysHave === 0);

    // Ближайшие к расчёту пары: пять строк с конкретным числом дней. Полный
    // список из 20–30 почти одинаковых строк — тот же шум, что и старый порог.
    const representatives = lacking.slice(0, 5);

    // «Нет общих дней» перечисляем только теми рядами, которых нет у всех
    // таких пар: иначе подпись превращается в список всех показателей.
    const blankLabels = blank
      .reduce<string[] | null>((common, pair) => {
        const labels = [seriesTitle(pair.left), seriesTitle(pair.right)];
        return common === null ? labels : common.filter((label) => labels.includes(label));
      }, null)
      ?.filter((label, index, all) => all.indexOf(label) === index) ?? [];

    return {
      representatives,
      waiting: Math.max(0, lacking.length - representatives.length),
      blankSeries: blankLabels,
      blank,
      fdr: sorted.filter((pair) => pair.reason === 'fdr'),
      flat: sorted.filter((pair) => pair.reason === 'no_variance'),
      need: sorted[0]?.needDays ?? 14,
    };
  }, [items]);

  return (
    <div className="pi-reasons">
      {groups.representatives.length > 0 ? (
        <section className="pi-reasons__group" aria-label={REASON_TITLES.insufficient_data}>
          <p className="pi-reasons__title">Не хватает дней</p>
          <ul className="pi-reasons__list">
            {groups.representatives.map((pair) => (
              <li className="pi-reasons__row" key={`${pair.left}-${pair.right}`}>
                <span className="pi-reasons__pair">{pairTitle(pair)}</span>
                <span className="pi-num pi-reasons__value">
                  есть {formatNumber(pair.daysHave)}{' '}
                  {pluralRu(pair.daysHave, ['день', 'дня', 'дней'])} из{' '}
                  {formatNumber(pair.needDays || groups.need)}
                </span>
              </li>
            ))}
          </ul>
          {groups.waiting > 0 ? (
            <p className="pi-reasons__foot">
              Ещё {formatNumber(groups.waiting)}{' '}
              {pluralRu(groups.waiting, ['пара', 'пары', 'пар'])} — с меньшим числом
              общих дней.
            </p>
          ) : null}
        </section>
      ) : null}

      {groups.flat.length > 0 ? (
        <section className="pi-reasons__group" aria-label={REASON_TITLES.no_variance}>
          <p className="pi-reasons__title">
            Значения не меняются
            <span className="pi-num">{formatNumber(groups.flat.length)}</span>
          </p>
          <ul className="pi-reasons__list">
            {groups.flat.map((pair) => (
              <li className="pi-reasons__row" key={`${pair.left}-${pair.right}`}>
                <span className="pi-reasons__pair">{pairTitle(pair)}</span>
                <span className="pi-reasons__value">сравнивать нечего</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {groups.fdr.length > 0 ? (
        <details className="pi-reasons__group pi-reasons__group--fdr">
          <summary className="pi-reasons__summary">
            Данных хватает, но связь не прошла проверку на случайность
            <span className="pi-num">{formatNumber(groups.fdr.length)}</span>
          </summary>
          <ul className="pi-reasons__list pi-reasons__list--dense">
            {groups.fdr.map((pair) => (
              <li className="pi-reasons__row" key={`${pair.left}-${pair.right}`}>
                <span className="pi-reasons__pair">{pairTitle(pair)}</span>
                <span className="pi-num pi-reasons__value">
                  {formatNumber(pair.daysHave)}{' '}
                  {pluralRu(pair.daysHave, ['день', 'дня', 'дней'])}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {groups.blank.length > 0 ? (
        <p className="pi-reasons__foot">
          Ещё {formatNumber(groups.blank.length)}{' '}
          {pluralRu(groups.blank.length, ['пара', 'пары', 'пар'])}{' '}
          {groups.blankSeries.length > 0
            ? `без общих дней — в окне нет данных: ${groups.blankSeries.join(', ')}.`
            : 'без общих дней: показатели ни разу не встретились в один день.'}
        </p>
      ) : null}
    </div>
  );
}
