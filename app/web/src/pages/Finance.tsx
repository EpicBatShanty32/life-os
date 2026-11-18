import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  Button,
  DateInput,
  EmptyState,
  Input,
  Menu,
  ProgressBar,
  Segmented,
  Select,
  Skeleton,
  Table,
} from '../components/ui';
import type { TableColumn } from '../components/ui';
import { toast } from '../lib/toast';
import { formatDateShort, formatNumber, pluralRu } from '../lib/format';
import { currentFilters, setFilters } from '../lib/router';
import type {
  AccountsPayload,
  Budget,
  DailyPoint,
  FinanceCategory,
  FinanceSummary,
  Transaction,
  TxKind,
} from '../api/finance';
import {
  createTransaction,
  currentMonth,
  dayOfMonth,
  daysInMonth,
  deleteTransaction,
  fetchAccounts,
  fetchFinanceCategories,
  fetchFinanceSummary,
  fetchTransactions,
  formatPercent,
  formatRub,
  monthLabel,
  parseAmountToKopecks,
  putBudget,
  shiftMonth,
  todayIso,
} from '../api/finance';
import '../styles/pages-life.css';

type CatTone = 'cat-1' | 'cat-2' | 'cat-3' | 'cat-4' | 'cat-5';

const CAT_TONES: readonly CatTone[] = ['cat-1', 'cat-2', 'cat-3', 'cat-4', 'cat-5'];

let catTokenColors: string[] | null = null;

/** Фактические значения токенов палитры — чтобы свести цвет из БД к tone без хардкода. */
function readCatTokenColors(): string[] {
  if (catTokenColors) return catTokenColors;
  if (typeof window === 'undefined') return (catTokenColors = []);
  const style = window.getComputedStyle(document.documentElement);
  catTokenColors = CAT_TONES.map((tone) =>
    style.getPropertyValue(`--${tone}`).trim().toLowerCase(),
  );
  return catTokenColors;
}

/** Цвет категории из БД сводим к пяти токенам палитры (без инлайн-hex в разметке). */
function categoryTone(color: string | null | undefined, seed = 0): CatTone {
  const value = (color ?? '').trim().toLowerCase();
  if (value) {
    const index = readCatTokenColors().indexOf(value);
    if (index >= 0) return CAT_TONES[index] ?? 'cat-1';
  }
  return CAT_TONES[Math.abs(seed) % CAT_TONES.length] ?? 'cat-1';
}

function monthDay(month: string, day: number): string {
  return `${month}-${String(day).padStart(2, '0')}`;
}

function defaultDateForMonth(month: string): string {
  if (month === currentMonth()) return todayIso();
  return `${month}-01`;
}

function matchesFilters(
  tx: Transaction,
  month: string,
  kind: TxKind | '',
  categoryId: number | '',
  query: string,
): boolean {
  if (!tx.date.startsWith(month)) return false;
  if (kind && tx.kind !== kind) return false;
  if (categoryId !== '' && tx.categoryId !== categoryId) return false;
  if (query.trim() && !tx.note.toLowerCase().includes(query.trim().toLowerCase())) return false;
  return true;
}

function sortTransactions(list: Transaction[]): Transaction[] {
  return [...list].sort((a, b) => {
    if (a.id < 0 || b.id < 0) {
      if (a.id < 0 && b.id < 0) return b.id - a.id;
      return a.id < 0 ? -1 : 1;
    }
    return b.date.localeCompare(a.date) || b.id - a.id;
  });
}

/* ── Сводка месяца ────────────────────────────────────────── */

function SummaryStrip({
  summary,
  loading,
  error,
  onRetry,
}: {
  summary: FinanceSummary | null;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  if (!summary && error) {
    return (
      <section className="fin-summary fin-summary--error" aria-label="Сводка месяца">
        <p className="ledger-error">
          Сводка не загрузилась.
          <button type="button" className="ledger-retry" onClick={onRetry}>
            Повторить
          </button>
        </p>
      </section>
    );
  }
  if (!summary && loading) {
    return (
      <section className="fin-summary" aria-label="Сводка месяца" aria-busy="true">
        <Skeleton variant="block" className="fin-summary__skeleton" />
      </section>
    );
  }
  if (!summary) return null;

  const balance = summary.balanceKopecks;
  const tone = balance > 0 ? 'positive' : balance < 0 ? 'negative' : 'flat';

  return (
    <section className="fin-summary" aria-label="Сводка месяца">
      <div className="fin-summary__cell">
        <span className="fin-summary__label">Доход</span>
        <span className="fin-summary__value">{formatRub(summary.incomeKopecks)}</span>
      </div>
      <div className="fin-summary__cell">
        <span className="fin-summary__label">Расход</span>
        <span className="fin-summary__value">{formatRub(summary.expenseKopecks)}</span>
      </div>
      <div className="fin-summary__cell fin-summary__cell--balance">
        <span className="fin-summary__label">Разница</span>
        <span className="fin-summary__value" data-tone={tone}>
          {formatRub(balance, { signed: balance > 0 })}
        </span>
        <span className="fin-summary__hint">
          {balance > 0 ? 'профицит' : balance < 0 ? 'дефицит' : 'ровно в ноль'}
        </span>
      </div>
    </section>
  );
}

/* ── Категории расходов ───────────────────────────────────── */

interface CategoryRow {
  key: string;
  name: string;
  sumKopecks: number;
  share: number;
  tone: CatTone;
}

function CategoryBars({
  summary,
  loading,
  error,
  onRetry,
  onAdd,
}: {
  summary: FinanceSummary | null;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  onAdd: () => void;
}) {
  const expenseRows = useMemo(() => {
    if (!summary) return [] as CategoryRow[];
    const expenses = summary.byCategory
      .filter((slice) => slice.kind === 'expense')
      .sort((a, b) => b.sumKopecks - a.sumKopecks);
    const total = summary.expenseKopecks;
    const rows: CategoryRow[] = expenses.map((slice, index) => ({
      key: `${slice.id ?? 'none'}-${slice.name}`,
      name: slice.name,
      sumKopecks: slice.sumKopecks,
      share: total > 0 ? slice.sumKopecks / total : 0,
      tone: categoryTone(slice.color, slice.id ?? index),
    }));
    if (rows.length <= 9) return rows;
    const head = rows.slice(0, 8);
    const rest = rows.slice(8);
    const restSum = rest.reduce((sum, row) => sum + row.sumKopecks, 0);
    return [
      ...head,
      {
        key: 'rest',
        name: `Прочее (${rest.length})`,
        sumKopecks: restSum,
        share: total > 0 ? restSum / total : 0,
        tone: 'cat-5' as CatTone,
      },
    ];
  }, [summary]);

  const incomeRows = useMemo(
    () =>
      (summary?.byCategory ?? [])
        .filter((slice) => slice.kind === 'income')
        .sort((a, b) => b.sumKopecks - a.sumKopecks),
    [summary],
  );

  return (
    <section className="fin-panel" aria-labelledby="fin-cat-title">
      <header className="fin-panel__head">
        <div>
          <h2 className="fin-panel__title" id="fin-cat-title">
            Расход по категориям
          </h2>
          <p className="fin-panel__sub">Доли внутри расходов месяца</p>
        </div>
        <span className="fin-panel__total">
          {summary ? formatRub(summary.expenseKopecks) : '—'}
        </span>
      </header>

      {!summary && loading ? (
        <div className="fin-panel__state" role="status" aria-label="Загружаем категории">
          <Skeleton variant="text" lines={3} />
        </div>
      ) : !summary && error ? (
        <p className="ledger-error">
          Категории не загрузились.
          <button type="button" className="ledger-retry" onClick={onRetry}>
            Повторить
          </button>
        </p>
      ) : expenseRows.length === 0 ? (
        <div className="fin-panel__state">
          <EmptyState
            title="В этом месяце расходов нет"
            description="Добавьте первую операцию — бары появятся сами."
            action={
              <Button variant="secondary" size="sm" onClick={onAdd}>
                Добавить операцию
              </Button>
            }
          />
        </div>
      ) : (
        <ul className="fin-bars">
          {expenseRows.map((row) => (
            <li className="fin-bar" key={row.key}>
              <div className="fin-bar__top">
                <span className="fin-dot" data-tone={row.tone} aria-hidden="true" />
                <span className="fin-bar__name">{row.name}</span>
                <span className="fin-bar__sum">{formatRub(row.sumKopecks)}</span>
                <span className="fin-bar__share">
                  {formatPercent(row.share * 100, row.share * 100 < 10 ? 1 : 0)}
                </span>
              </div>
              <ProgressBar
                value={row.share * 100}
                tone={row.tone}
                ariaLabel={`${row.name}: ${formatRub(row.sumKopecks)}, ${formatPercent(row.share * 100)}`}
              />
            </li>
          ))}
        </ul>
      )}

      {incomeRows.length > 0 ? (
        <p className="fin-income-line">
          <span className="fin-income-line__label">Поступления:</span>{' '}
          {incomeRows.map((slice, index) => (
            <span key={`${slice.id ?? 'none'}-${slice.name}`}>
              {index > 0 ? ' · ' : ''}
              {slice.name} — {formatRub(slice.sumKopecks)}
            </span>
          ))}
        </p>
      ) : null}
    </section>
  );
}

/* ── Тренд по дням ────────────────────────────────────────── */

interface TrendPoint {
  date: string;
  value: number;
}

function TrendPanel({
  summary,
  month,
  loading,
  error,
  onRetry,
}: {
  summary: FinanceSummary | null;
  month: string;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const points = useMemo<TrendPoint[]>(() => {
    const byDate = new Map<string, DailyPoint>();
    for (const point of summary?.daily ?? []) byDate.set(point.date, point);
    const today = new Date();
    const isCurrent = month === currentMonth();
    const lastDay = isCurrent
      ? Math.max(1, Math.min(today.getDate(), daysInMonth(month)))
      : daysInMonth(month);
    const result: TrendPoint[] = [];
    for (let day = 1; day <= lastDay; day += 1) {
      const date = monthDay(month, day);
      result.push({ date, value: byDate.get(date)?.expenseKopecks ?? 0 });
    }
    return result;
  }, [summary, month]);

  const hasData = points.some((point) => point.value > 0);
  const values = points.map((point) => point.value);
  const max = Math.max(...values, 1);
  const peakIndex = values.reduce((best, value, index) => (value > values[best]! ? index : best), 0);
  const peak = points[peakIndex];
  const total = values.reduce((sum, value) => sum + value, 0);

  const width = 320;
  const height = 108;
  const padX = 6;
  const padTop = 16;
  const padBottom = 22;
  const innerWidth = width - padX * 2;
  const innerHeight = height - padTop - padBottom;
  const xFor = (index: number) =>
    points.length <= 1 ? padX + innerWidth / 2 : padX + (index * innerWidth) / (points.length - 1);
  const yFor = (value: number) => padTop + (1 - value / max) * innerHeight;
  const path = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${xFor(index).toFixed(1)},${yFor(point.value).toFixed(1)}`)
    .join(' ');

  return (
    <section className="fin-panel" aria-labelledby="fin-trend-title">
      <header className="fin-panel__head">
        <div>
          <h2 className="fin-panel__title" id="fin-trend-title">
            Динамика
          </h2>
          <p className="fin-panel__sub">Расход по дням месяца</p>
        </div>
        <span className="fin-panel__total">{formatRub(total)}</span>
      </header>

      {!summary && loading ? (
        <div className="fin-panel__state" role="status" aria-label="Загружаем динамику">
          <Skeleton variant="block" />
        </div>
      ) : !summary && error ? (
        <p className="ledger-error">
          Динамика не загрузилась.
          <button type="button" className="ledger-retry" onClick={onRetry}>
            Повторить
          </button>
        </p>
      ) : !hasData ? (
        <div className="fin-panel__state">
          <p className="fin-empty-line">
            {month === currentMonth()
              ? 'За этот месяц операций ещё нет — тренд появится после первой записи.'
              : 'В этом месяце расходов не было.'}
          </p>
        </div>
      ) : (
        <figure className="fin-trend">
          <figcaption className="fin-trend__caption">
            Пик расхода — {formatRub(peak?.value ?? 0)} {peak ? dayLabel(peak.date) : ''}.{' '}
            Дней с операциями: {points.filter((point) => point.value > 0).length}.
          </figcaption>
          <svg
            className="fin-trend__svg"
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label={`Расход по дням: максимум ${formatRub(max)} ${peak ? dayLabel(peak.date) : ''}`}
          >
            {[0, 0.5, 1].map((fraction) => (
              <line
                key={fraction}
                className="fin-trend__grid"
                x1={padX}
                x2={width - padX}
                y1={padTop + innerHeight * fraction}
                y2={padTop + innerHeight * fraction}
              />
            ))}
            <path className="fin-trend__line" d={path} fill="none" />
            {peak ? (
              <circle
                className="fin-trend__peak"
                cx={xFor(peakIndex)}
                cy={yFor(peak.value)}
                r="3"
              >
                <title>
                  {dayLabel(peak.date)}: {formatRub(peak.value)}
                </title>
              </circle>
            ) : null}
            <text className="fin-trend__axis" x={padX} y={10}>
              {formatRub(max)}
            </text>
            <text className="fin-trend__axis" x={padX} y={height - 6}>
              0 ₽
            </text>
            <text className="fin-trend__axis fin-trend__axis--right" x={width - padX} y={height - 6}>
              {points.length > 0 ? `${dayOfMonth(points[points.length - 1]!.date)}.${month.slice(5, 7)}` : ''}
            </text>
          </svg>
          <details className="fin-trend__data">
            <summary>Данные по дням</summary>
            <div className="fin-trend__tablewrap">
              <table>
                <caption className="visually-hidden">Расход по дням месяца</caption>
                <thead>
                  <tr>
                    <th scope="col">День</th>
                    <th scope="col" className="is-numeric">
                      Расход
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {points
                    .filter((point) => point.value > 0)
                    .map((point) => (
                      <tr key={point.date}>
                        <td>{dayLabel(point.date)}</td>
                        <td className="is-numeric">{formatRub(point.value)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </details>
        </figure>
      )}
    </section>
  );
}

function dayLabel(date: string): string {
  const day = Number(date.slice(8, 10));
  const monthIndex = Number(date.slice(5, 7)) - 1;
  const months = [
    'января',
    'февраля',
    'марта',
    'апреля',
    'мая',
    'июня',
    'июля',
    'августа',
    'сентября',
    'октября',
    'ноября',
    'декабря',
  ];
  return `${day} ${months[monthIndex] ?? ''}`.trim();
}

/* ── Бюджеты ──────────────────────────────────────────────── */

interface BudgetsPanelProps {
  budgets: Budget[];
  categories: FinanceCategory[];
  summary: FinanceSummary | null;
  month: string;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  onChanged: () => void;
}

function BudgetsPanel({
  budgets,
  categories,
  summary,
  month,
  loading,
  error,
  onRetry,
  onChanged,
}: BudgetsPanelProps) {
  const [local, setLocal] = useState<Budget[]>(budgets);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editValue, setEditValue] = useState('');
  const [addCategory, setAddCategory] = useState('');
  const [addLimit, setAddLimit] = useState('');
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    setLocal(budgets);
  }, [budgets]);

  const expenseCategories = useMemo(
    () => categories.filter((category) => category.kind === 'expense' && !category.archived),
    [categories],
  );

  const available = useMemo(
    () => expenseCategories.filter((category) => !local.some((budget) => budget.categoryId === category.id)),
    [expenseCategories, local],
  );

  useEffect(() => {
    if (available.length > 0 && !available.some((category) => String(category.id) === addCategory)) {
      setAddCategory(String(available[0]!.id));
    }
  }, [available, addCategory]);

  function spentFor(categoryId: number): number {
    return (
      summary?.byCategory.find((slice) => slice.kind === 'expense' && slice.id === categoryId)
        ?.sumKopecks ?? 0
    );
  }

  function makeBudget(category: FinanceCategory, limitKopecks: number): Budget {
    const spentKopecks = spentFor(category.id);
    return {
      id: -Date.now(),
      categoryId: category.id,
      name: category.name,
      color: category.color,
      month,
      limitKopecks,
      spentKopecks,
      pct: limitKopecks > 0 ? Math.round((spentKopecks / limitKopecks) * 1000) / 10 : 0,
      over: spentKopecks > limitKopecks,
      overKopecks: Math.max(0, spentKopecks - limitKopecks),
    };
  }

  async function saveLimit(budget: Budget, limitKopecks: number) {
    const previous = local;
    const optimistic: Budget = {
      ...budget,
      limitKopecks,
      pct: limitKopecks > 0 ? Math.round((budget.spentKopecks / limitKopecks) * 1000) / 10 : 0,
      over: budget.spentKopecks > limitKopecks,
      overKopecks: Math.max(0, budget.spentKopecks - limitKopecks),
    };
    setLocal((current) => current.map((item) => (item.categoryId === budget.categoryId ? optimistic : item)));
    setBusy(`budget-${budget.categoryId}`);
    try {
      const saved = await putBudget({
        categoryId: budget.categoryId,
        month,
        limit: limitKopecks / 100,
      });
      setLocal((current) =>
        current.map((item) => (item.categoryId === budget.categoryId ? saved : item)),
      );
      setEditingId(null);
      setFormError('');
      onChanged();
    } catch {
      setLocal(previous);
      toast('Лимит не сохранился — проверьте сумму', 'error');
    } finally {
      setBusy(null);
    }
  }

  async function addBudget() {
    const category = available.find((item) => String(item.id) === addCategory);
    if (!category) {
      setFormError('Выберите категорию');
      return;
    }
    const parsed = parseAmountToKopecks(addLimit);
    if ('error' in parsed) {
      setFormError(parsed.error);
      return;
    }
    const optimistic = makeBudget(category, parsed.kopecks);
    setLocal((current) => [...current, optimistic]);
    setBusy('budget-add');
    setFormError('');
    try {
      const saved = await putBudget({ categoryId: category.id, month, limit: parsed.kopecks / 100 });
      setLocal((current) =>
        current.map((item) =>
          item.categoryId === saved.categoryId && item.id < 0 ? saved : item,
        ),
      );
      setAddLimit('');
      onChanged();
    } catch {
      setLocal((current) => current.filter((item) => item !== optimistic));
      setFormError('Лимит не сохранился');
    } finally {
      setBusy(null);
    }
  }

  const sorted = useMemo(
    () =>
      [...local].sort(
        (a, b) => Number(b.over) - Number(a.over) || b.pct - a.pct || a.name.localeCompare(b.name),
      ),
    [local],
  );

  return (
    <section className="fin-panel" aria-labelledby="fin-budgets-title">
      <header className="fin-panel__head">
        <div>
          <h2 className="fin-panel__title" id="fin-budgets-title">
            Бюджеты
          </h2>
          <p className="fin-panel__sub">Лимиты на месяц</p>
        </div>
        <span className="fin-panel__total">
          {local.length > 0
            ? `${local.length} ${pluralRu(local.length, ['лимит', 'лимита', 'лимитов'])}`
            : '—'}
        </span>
      </header>

      {loading && !summary ? (
        <div className="fin-panel__state" role="status" aria-label="Загружаем бюджеты">
          <Skeleton variant="text" lines={2} />
        </div>
      ) : error && !summary ? (
        <p className="ledger-error">
          Бюджеты не загрузились.
          <button type="button" className="ledger-retry" onClick={onRetry}>
            Повторить
          </button>
        </p>
      ) : sorted.length === 0 ? (
        <div className="fin-panel__state">
          <EmptyState
            title="Лимитов на месяц нет"
            description="Задайте лимит на категорию — увидите перерасход заранее."
            action={
              available.length > 0 ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => document.getElementById('fin-budget-category')?.focus()}
                >
                  Задать лимит
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <ul className="fin-budgets">
          {sorted.map((budget) => {
            const tone = categoryTone(budget.color, budget.categoryId);
            const remaining = budget.limitKopecks - budget.spentKopecks;
            const editing = editingId === budget.categoryId;
            return (
              <li className="fin-budget" key={budget.categoryId}>
                <div className="fin-budget__top">
                  <span className="fin-dot" data-tone={tone} aria-hidden="true" />
                  <span className="fin-budget__name">{budget.name}</span>
                  {editing ? null : (
                    <button
                      type="button"
                      className="fin-budget__edit"
                      onClick={() => {
                        setEditingId(budget.categoryId);
                        setEditValue(String(budget.limitKopecks / 100));
                        setFormError('');
                      }}
                      aria-label={`Изменить лимит для «${budget.name}»`}
                    >
                      лимит
                    </button>
                  )}
                  <span className="fin-budget__pct">{formatPercent(budget.pct, budget.pct < 10 ? 1 : 0)}</span>
                </div>
                <ProgressBar
                  value={Math.min(100, Math.max(0, budget.pct))}
                  tone={budget.over ? 'danger' : budget.pct >= 80 ? 'warning' : tone}
                  ariaLabel={`${budget.name}: израсходовано ${formatPercent(budget.pct)} лимита`}
                />
                <div className="fin-budget__bottom">
                  <span className="fin-budget__sum">
                    {formatRub(budget.spentKopecks)} / {formatRub(budget.limitKopecks)}
                  </span>
                  <span
                    className="fin-budget__note"
                    data-tone={budget.over ? 'danger' : budget.pct >= 80 ? 'warning' : undefined}
                  >
                    {budget.over
                      ? `перерасход ${formatRub(Math.max(0, -remaining))}`
                      : budget.pct >= 80
                        ? `близко к лимиту · осталось ${formatRub(Math.max(0, remaining))}`
                        : `осталось ${formatRub(Math.max(0, remaining))}`}
                  </span>
                </div>
                {editing ? (
                  <div className="fin-budget__editor">
                    <Input
                      value={editValue}
                      onChange={(event) => setEditValue(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          const parsed = parseAmountToKopecks(editValue);
                          if ('error' in parsed) setFormError(parsed.error);
                          else void saveLimit(budget, parsed.kopecks);
                        }
                        if (event.key === 'Escape') setEditingId(null);
                      }}
                      inputMode="decimal"
                      size="sm"
                      aria-label={`Новый лимит для «${budget.name}», рублей`}
                      autoFocus
                    />
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        const parsed = parseAmountToKopecks(editValue);
                        if ('error' in parsed) setFormError(parsed.error);
                        else void saveLimit(budget, parsed.kopecks);
                      }}
                      loading={busy === `budget-${budget.categoryId}`}
                    >
                      Сохранить
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setEditingId(null)}>
                      Отмена
                    </Button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      {formError ? (
        <p className="fin-form__error" role="alert">
          {formError}
        </p>
      ) : null}

      {available.length > 0 ? (
        <div className="fin-budget-add">
          <span className="fin-budget-add__label">Новый лимит</span>
          <Select
            id="fin-budget-category"
            value={addCategory}
            onChange={(event) => {
              setAddCategory(event.target.value);
              setFormError('');
            }}
            options={available.map((category) => ({ value: String(category.id), label: category.name }))}
            size="sm"
            aria-label="Категория бюджета"
          />
          <Input
            value={addLimit}
            onChange={(event) => setAddLimit(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void addBudget();
              }
            }}
            inputMode="decimal"
            placeholder="Лимит, ₽"
            size="sm"
            aria-label="Лимит бюджета в рублях"
          />
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void addBudget()}
            loading={busy === 'budget-add'}
          >
            Поставить лимит
          </Button>
        </div>
      ) : null}
    </section>
  );
}

/* ── Транзакции ───────────────────────────────────────────── */

interface TransactionsPanelProps {
  month: string;
  categories: FinanceCategory[];
  accounts: AccountsPayload[];
  metaError: boolean;
  onRetryMeta: () => void;
  onChanged: () => void;
}

function TransactionsPanel({
  month,
  categories,
  accounts,
  metaError,
  onRetryMeta,
  onChanged,
}: TransactionsPanelProps) {
  const [items, setItems] = useState<Transaction[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const initialFilters = useRef(currentFilters()).current;
  const initialQuery = (initialFilters.q ?? '').trim();
  const [kindFilter, setKindFilter] = useState<TxKind | ''>(() =>
    initialFilters.kind === 'income' || initialFilters.kind === 'expense'
      ? initialFilters.kind
      : '',
  );
  const [categoryFilter, setCategoryFilter] = useState<number | ''>(() => {
    const value = Number(initialFilters.categoryId ?? initialFilters.category);
    return Number.isInteger(value) && value > 0 ? value : '';
  });
  const [query, setQuery] = useState(initialQuery);
  const [appliedQuery, setAppliedQuery] = useState(initialQuery);

  const [formDate, setFormDate] = useState(() => defaultDateForMonth(month));
  const [formAmount, setFormAmount] = useState('');
  const [formKind, setFormKind] = useState<TxKind>('expense');
  const [formCategory, setFormCategory] = useState('');
  const [formAccount, setFormAccount] = useState('');
  const [formNote, setFormNote] = useState('');
  const [amountError, setAmountError] = useState('');
  const [formBusy, setFormBusy] = useState(false);

  const seq = useRef(0);

  const focusAmount = () => {
    document.getElementById('fin-amount')?.focus();
  };

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedQuery(query.trim()), 220);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    setFilters(
      {
        kind: kindFilter,
        categoryId: categoryFilter === '' ? '' : String(categoryFilter),
        q: appliedQuery,
      },
      { replace: true },
    );
  }, [kindFilter, categoryFilter, appliedQuery]);

  useEffect(() => {
    setFormDate(defaultDateForMonth(month));
  }, [month]);

  const expenseCategories = useMemo(
    () => categories.filter((category) => category.kind === 'expense' && !category.archived),
    [categories],
  );
  const incomeCategories = useMemo(
    () => categories.filter((category) => category.kind === 'income' && !category.archived),
    [categories],
  );
  const formCategories = formKind === 'expense' ? expenseCategories : incomeCategories;

  useEffect(() => {
    if (formCategories.length === 0) {
      setFormCategory('');
      return;
    }
    if (!formCategories.some((category) => String(category.id) === formCategory)) {
      setFormCategory(String(formCategories[0]!.id));
    }
  }, [formCategories, formCategory]);

  const load = useCallback(async () => {
    const current = ++seq.current;
    setLoading(true);
    try {
      const payload = await fetchTransactions({
        month,
        kind: kindFilter || undefined,
        categoryId: categoryFilter === '' ? undefined : categoryFilter,
        q: appliedQuery,
        limit: 300,
      });
      if (current !== seq.current) return;
      setItems(payload.items);
      setTotal(payload.total);
      setError(false);
    } catch {
      if (current !== seq.current) return;
      setError(true);
    } finally {
      if (current === seq.current) setLoading(false);
    }
  }, [month, kindFilter, categoryFilter, appliedQuery]);

  useEffect(() => {
    void load();
  }, [load]);

  const filterCategories = useMemo(() => {
    if (!kindFilter) return categories.filter((category) => !category.archived);
    return categories.filter((category) => category.kind === kindFilter && !category.archived);
  }, [categories, kindFilter]);

  useEffect(() => {
    if (categoryFilter === '') return;
    if (!filterCategories.some((category) => category.id === categoryFilter)) {
      setCategoryFilter('');
    }
  }, [filterCategories, categoryFilter]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = parseAmountToKopecks(formAmount);
    if ('error' in parsed) {
      setAmountError(parsed.error);
      return;
    }
    const category = formCategories.find((item) => String(item.id) === formCategory);
    if (!category) {
      setAmountError('Выберите категорию');
      return;
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(formDate)) {
      setAmountError('Укажите дату');
      return;
    }
    setAmountError('');
    setFormBusy(true);
    const tempId = -Date.now();
    const temp: Transaction = {
      id: tempId,
      date: formDate,
      amountKopecks: parsed.kopecks,
      kind: formKind,
      categoryId: category.id,
      categoryName: category.name,
      accountId: formAccount ? Number(formAccount) : null,
      accountName: accounts.find((account) => String(account.id) === formAccount)?.name ?? '',
      note: formNote.trim(),
      source: 'manual',
    };
    const visible = matchesFilters(temp, month, kindFilter, categoryFilter, appliedQuery);
    if (visible) {
      setItems((current) => sortTransactions([temp, ...current]));
      setTotal((current) => current + 1);
    }
    try {
      const created = await createTransaction({
        date: formDate,
        amount: (parsed.kopecks / 100).toFixed(2),
        kind: formKind,
        categoryId: category.id,
        accountId: temp.accountId,
        note: temp.note,
      });
      setItems((current) => {
        const withoutTemp = current.filter((item) => item.id !== tempId);
        const next = visible ? [created, ...withoutTemp] : withoutTemp;
        return sortTransactions(next);
      });
      if (!visible) void load();
      setFormAmount('');
      setFormNote('');
      setAmountError('');
      onChanged();
      window.requestAnimationFrame(focusAmount);
    } catch {
      setItems((current) => current.filter((item) => item.id !== tempId));
      if (visible) setTotal((current) => Math.max(0, current - 1));
      toast('Операция не сохранилась — проверьте поля', 'error');
    } finally {
      setFormBusy(false);
    }
  }

  async function removeTransaction(tx: Transaction) {
    const snapshot = items;
    setItems((current) => current.filter((item) => item.id !== tx.id));
    setTotal((current) => Math.max(0, current - 1));
    try {
      await deleteTransaction(tx.id);
      onChanged();
    } catch {
      setItems(snapshot);
      setTotal((current) => current + 1);
      toast('Не удалось удалить операцию', 'error');
    }
  }

  const columns = useMemo<TableColumn<Transaction>[]>(
    () => [
      {
        key: 'date',
        header: 'Дата',
        width: '64px',
        cell: (tx) => (
          <span className="fin-date" title={tx.date}>
            {formatDateShort(tx.date)}
          </span>
        ),
      },
      {
        key: 'category',
        header: 'Категория',
        cell: (tx) => (
          <span className="fin-cat">
            <span
              className="fin-dot"
              data-tone={categoryTone(
                categories.find((category) => category.id === tx.categoryId)?.color,
                tx.categoryId ?? 0,
              )}
              aria-hidden="true"
            />
            <span className="fin-cat__name">{tx.categoryName}</span>
            {tx.note ? <span className="fin-cat__note">{tx.note}</span> : null}
          </span>
        ),
      },
      {
        key: 'account',
        header: 'Счёт',
        width: '132px',
        className: 'fin-col-account',
        cell: (tx) => <span className="fin-account">{tx.accountName || '—'}</span>,
      },
      {
        key: 'amount',
        header: 'Сумма',
        numeric: true,
        cell: (tx) => (
          <span className="fin-amount" data-kind={tx.kind}>
            {tx.kind === 'income'
              ? formatRub(tx.amountKopecks, { signed: true })
              : formatRub(-tx.amountKopecks)}
          </span>
        ),
      },
      {
        key: 'actions',
        header: '',
        width: '36px',
        className: 'fin-col-actions',
        cell: (tx) => (
          <Menu
            label={`Действия: ${tx.categoryName}, ${formatRub(tx.amountKopecks)}`}
            items={[
              {
                id: 'delete',
                label: 'Удалить',
                icon: 'trash',
                danger: true,
                onSelect: () => void removeTransaction(tx),
              },
            ]}
          />
        ),
      },
    ],
    [categories, items],
  );

  const filtersActive = Boolean(kindFilter || categoryFilter !== '' || appliedQuery);
  const amountId = 'fin-amount';

  return (
    <section className="fin-panel" aria-labelledby="fin-tx-title">
      <header className="fin-panel__head">
        <div>
          <h2 className="fin-panel__title" id="fin-tx-title">
            Транзакции
          </h2>
          <p className="fin-panel__sub">
            {formatNumber(total)} {pluralRu(total, ['операция', 'операции', 'операций'])} за месяц
          </p>
        </div>
      </header>

      <form className="fin-form" onSubmit={submit}>
        <label className="fin-form__field fin-form__field--date">
          <span className="fin-form__label">Дата</span>
          <DateInput
            value={formDate}
            onChange={(event) => setFormDate(event.target.value)}
            size="sm"
            aria-label="Дата операции"
          />
        </label>
        <label className="fin-form__field fin-form__field--amount">
          <span className="fin-form__label">Сумма, ₽</span>
          <Input
            id={amountId}
            value={formAmount}
            onChange={(event) => {
              setFormAmount(event.target.value);
              setAmountError('');
            }}
            inputMode="decimal"
            placeholder="0,00"
            size="sm"
            aria-invalid={amountError ? true : undefined}
            aria-describedby={amountError ? 'fin-amount-error' : undefined}
          />
        </label>
        <div className="fin-form__field fin-form__field--kind">
          <span className="fin-form__label">Тип</span>
          <Segmented
            ariaLabel="Тип операции"
            value={formKind}
            options={[
              { value: 'expense', label: 'Расход' },
              { value: 'income', label: 'Доход' },
            ]}
            onChange={(value) => setFormKind(value)}
          />
        </div>
        <label className="fin-form__field fin-form__field--category">
          <span className="fin-form__label">Категория</span>
          <Select
            value={formCategory}
            onChange={(event) => setFormCategory(event.target.value)}
            options={formCategories.map((category) => ({
              value: String(category.id),
              label: category.name,
            }))}
            size="sm"
            disabled={formCategories.length === 0}
            aria-label="Категория операции"
          />
        </label>
        <label className="fin-form__field fin-form__field--account">
          <span className="fin-form__label">Счёт</span>
          <Select
            value={formAccount}
            onChange={(event) => setFormAccount(event.target.value)}
            options={[
              { value: '', label: 'Без счёта' },
              ...accounts.map((account) => ({ value: String(account.id), label: account.name })),
            ]}
            size="sm"
            aria-label="Счёт операции"
          />
        </label>
        <label className="fin-form__field fin-form__field--note">
          <span className="fin-form__label">Заметка</span>
          <Input
            value={formNote}
            onChange={(event) => setFormNote(event.target.value)}
            placeholder="Например: обед"
            size="sm"
            maxLength={500}
            aria-label="Заметка к операции"
          />
        </label>
        <Button
          variant="secondary"
          size="sm"
          type="submit"
          className="fin-form__submit"
          loading={formBusy}
          disabled={metaError || formCategories.length === 0}
        >
          Записать
        </Button>
      </form>
      {amountError ? (
        <p className="fin-form__error" id="fin-amount-error" role="alert">
          {amountError}
        </p>
      ) : null}
      {metaError ? (
        <p className="ledger-error fin-form__meta-error">
          Категории и счета не загрузились — форма недоступна.
          <button type="button" className="ledger-retry" onClick={onRetryMeta}>
            Повторить
          </button>
        </p>
      ) : null}

      <div className="fin-filters">
        <Segmented
          ariaLabel="Фильтр по типу операции"
          value={kindFilter}
          options={[
            { value: '', label: 'Все' },
            { value: 'expense', label: 'Расходы' },
            { value: 'income', label: 'Доходы' },
          ]}
          onChange={(value) => setKindFilter(value)}
        />
        <label className="fin-filters__field">
          <span className="visually-hidden">Категория</span>
          <Select
            value={categoryFilter === '' ? '' : String(categoryFilter)}
            onChange={(event) =>
              setCategoryFilter(event.target.value === '' ? '' : Number(event.target.value))
            }
            options={[
              { value: '', label: 'Все категории' },
              ...filterCategories.map((category) => ({
                value: String(category.id),
                label: category.name,
              })),
            ]}
            size="sm"
          />
        </label>
        <label className="fin-filters__field fin-filters__field--search">
          <span className="visually-hidden">Поиск по заметке</span>
          <Input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Поиск по заметке"
            size="sm"
          />
        </label>
        {filtersActive ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setKindFilter('');
              setCategoryFilter('');
              setQuery('');
            }}
          >
            Сбросить
          </Button>
        ) : null}
      </div>

      {error && items.length === 0 ? (
        <p className="ledger-error">
          Транзакции не загрузились.
          <button type="button" className="ledger-retry" onClick={() => void load()}>
            Повторить
          </button>
        </p>
      ) : loading && items.length === 0 ? (
        <div className="fin-table-state" role="status" aria-label="Загружаем операции">
          <Skeleton variant="row" />
          <Skeleton variant="row" />
          <Skeleton variant="row" />
        </div>
      ) : (
        <>
          {error ? (
            <p className="ledger-error ledger-error--inline">
              Список мог устареть.
              <button type="button" className="ledger-retry" onClick={() => void load()}>
                Обновить
              </button>
            </p>
          ) : null}
          <Table
            columns={columns}
            rows={items}
            rowKey={(tx) => String(tx.id)}
            caption={`Транзакции за ${monthLabel(month)}`}
            className={loading ? 'fin-table is-loading' : 'fin-table'}
            empty={
              <EmptyState
                title={filtersActive ? 'Под фильтры ничего не попало' : 'В этом месяце операций нет'}
                description={
                  filtersActive
                    ? 'Сбросьте фильтры или измените запрос.'
                    : 'Добавьте первую операцию — сводка и бюджеты обновятся сразу.'
                }
                action={
                  filtersActive ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setKindFilter('');
                        setCategoryFilter('');
                        setQuery('');
                      }}
                    >
                      Сбросить фильтры
                    </Button>
                  ) : (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        focusAmount();
                        document.getElementById(amountId)?.scrollIntoView({ block: 'center' });
                      }}
                    >
                      Добавить операцию
                    </Button>
                  )
                }
              />
            }
          />
        </>
      )}
    </section>
  );
}

/* ── Страница ─────────────────────────────────────────────── */

export function Finance() {
  const [month, setMonth] = useState(() => {
    const raw = currentFilters().month;
    const current = currentMonth();
    const min = shiftMonth(current, -23);
    return raw && /^\d{4}-\d{2}$/.test(raw) && raw >= min && raw <= current ? raw : current;
  });
  const [summary, setSummary] = useState<FinanceSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState(false);
  const [accounts, setAccounts] = useState<AccountsPayload[]>([]);
  const [categories, setCategories] = useState<FinanceCategory[]>([]);
  const [metaError, setMetaError] = useState(false);

  const minMonth = shiftMonth(currentMonth(), -23);
  const nowMonth = currentMonth();

  const loadSummary = useCallback(async (targetMonth: string, silent = false) => {
    if (!silent) setSummaryLoading(true);
    try {
      const data = await fetchFinanceSummary(targetMonth);
      setSummary(data);
      setSummaryError(false);
    } catch {
      setSummaryError(true);
    } finally {
      if (!silent) setSummaryLoading(false);
    }
  }, []);

  const loadMeta = useCallback(async () => {
    try {
      const [accountList, categoryList] = await Promise.all([
        fetchAccounts(),
        fetchFinanceCategories(),
      ]);
      setAccounts(accountList.filter((account) => !account.archived));
      setCategories(categoryList.filter((category) => !category.archived));
      setMetaError(false);
    } catch {
      setMetaError(true);
    }
  }, []);

  useEffect(() => {
    void loadMeta();
  }, [loadMeta]);

  useEffect(() => {
    void loadSummary(month);
  }, [month, loadSummary]);

  useEffect(() => {
    setFilters({ month: month === currentMonth() ? '' : month }, { replace: true });
  }, [month]);

  const refresh = useCallback(() => {
    void loadSummary(month, true);
  }, [loadSummary, month]);

  const accountsLabel =
    accounts.length > 0
      ? `${formatNumber(accounts.length)} ${pluralRu(accounts.length, ['счёт', 'счёта', 'счетов'])}`
      : metaError
        ? 'счета не загружены'
        : 'нет счетов';

  return (
    <div className="page ledger-page fin-page">
      <header className="ledger-head">
        <div className="ledger-head__text">
          <h1 className="ledger-title">Финансы</h1>
          <p className="ledger-context">
            {monthLabel(month)} · {accountsLabel} · данные из локальной базы
          </p>
        </div>
        <div className="ledger-head__actions fin-month">
          <Button
            variant="ghost"
            size="sm"
            leadingIcon="chevron-left"
            aria-label="Предыдущий месяц"
            title="Предыдущий месяц"
            disabled={month <= minMonth}
            onClick={() => setMonth((current) => shiftMonth(current, -1))}
          />
          <span className="fin-month__label" aria-live="polite">
            {monthLabel(month)}
          </span>
          <Button
            variant="ghost"
            size="sm"
            leadingIcon="chevron-right"
            aria-label="Следующий месяц"
            title="Следующий месяц"
            disabled={month >= nowMonth}
            onClick={() => setMonth((current) => shiftMonth(current, 1))}
          />
        </div>
      </header>

      <SummaryStrip
        summary={summary}
        loading={summaryLoading}
        error={summaryError}
        onRetry={() => void loadSummary(month)}
      />

      <div className="fin-grid">
        <div className="fin-area fin-area--cats">
          <CategoryBars
            summary={summary}
            loading={summaryLoading}
            error={summaryError}
            onRetry={() => void loadSummary(month)}
            onAdd={() => {
              document.getElementById('fin-amount')?.focus();
              document.getElementById('fin-amount')?.scrollIntoView({ block: 'center' });
            }}
          />
        </div>
        <div className="fin-area fin-area--trend">
          <TrendPanel
            summary={summary}
            month={month}
            loading={summaryLoading}
            error={summaryError}
            onRetry={() => void loadSummary(month)}
          />
        </div>
        <div className="fin-area fin-area--tx">
          <TransactionsPanel
            month={month}
            categories={categories}
            accounts={accounts}
            metaError={metaError}
            onRetryMeta={() => void loadMeta()}
            onChanged={refresh}
          />
        </div>
        <div className="fin-area fin-area--budgets">
          <BudgetsPanel
            budgets={summary?.budgets ?? []}
            categories={categories}
            summary={summary}
            month={month}
            loading={summaryLoading}
            error={summaryError}
            onRetry={() => void loadSummary(month)}
            onChanged={refresh}
          />
        </div>
      </div>
    </div>
  );
}
