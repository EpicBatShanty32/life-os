/**
 * Клиенты «Аналитики» (handoff §10.6, §12.6).
 *
 * ``GET /api/analytics/summary`` — сводка блоков экрана. Отдельный запрос
 * ``GET /api/analytics/findings`` отдаёт готовые находки: пары, прошедшие FDR,
 * с лагом, силой и человеческим текстом, плюс честные отказы ``notReady``.
 * Порогов «|r| ≥ …» на экране больше нет: значимость считает сервер.
 *
 * Подписи честные: пустые ряды остаются пустыми, средняя энергия без чек-инов
 * приходит как null; проценты — это проценты, а не «оценки».
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiRequest } from './client';
import { apiErrorMessage } from './errors';

const TIMEOUT_MS = 8000;

export type PairStatus = 'ok' | 'weak' | 'insufficient_data' | 'no_variance';

export interface EnergyPoint {
  date: string;
  /** null — чек-ина не было: линия рвётся, а не достраивается через пробел. */
  value: number | null;
}

export interface AnalyticsEnergy {
  series: EnergyPoint[];
  avg: number | null;
  daysWithData: number;
}

export interface SphereSlice {
  id: number;
  name: string;
  color: string;
  done: number;
  total: number;
  /** Доля задач сферы среди всех задач окна, 0..1. */
  share: number;
  /** Доля закрытых задач сферы, 0..100. */
  pct: number;
}

export interface AnalyticsReading {
  pages: number;
  booksDone: number;
  pagesAll: number;
  booksDoneAll: number;
  booksReading: number;
}

export interface FinanceDay {
  date: string;
  income: number;
  expense: number;
}

export interface AnalyticsFinance {
  income: number;
  expense: number;
  balance: number;
  series: FinanceDay[];
}

export interface HabitDay {
  date: string;
  marks: number;
  pct: number;
}

export interface AnalyticsHabits {
  pct: number;
  habits: number;
  marks: number;
  series: HabitDay[];
}

export interface AnalyticsTasks {
  done: number;
  total: number;
  pct: number;
}

export interface CorrelationPair {
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

export interface AnalyticsSummary {
  days: number;
  from: string;
  to: string;
  energy: AnalyticsEnergy;
  spheres: SphereSlice[];
  reading: AnalyticsReading;
  finance: AnalyticsFinance;
  habits: AnalyticsHabits;
  tasks: AnalyticsTasks;
  correlations: CorrelationPair[];
}

/* ── Нормализация ────────────────────────────────────────── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function isoDay(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const day = value.slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null;
}

const PAIR_STATUSES: readonly PairStatus[] = ['ok', 'weak', 'insufficient_data', 'no_variance'];

function normalizePair(raw: unknown, index: number): CorrelationPair | null {
  if (!isRecord(raw)) return null;
  const status = (PAIR_STATUSES as readonly string[]).includes(str(raw.status))
    ? (str(raw.status) as PairStatus)
    : 'insufficient_data';
  const left = str(raw.left, `left-${index}`);
  const right = str(raw.right, `right-${index}`);
  return {
    left,
    right,
    label: str(raw.label, `${left} ↔ ${right}`),
    leftLabel: str(raw.left_label, left),
    rightLabel: str(raw.right_label, right),
    n: Math.max(0, Math.round(num(raw.n))),
    r: nullableNumber(raw.r),
    status,
    text: str(raw.text),
    sentence: str(raw.sentence),
    enoughData: raw.enough_data === true,
  };
}

function normalizeSummary(raw: unknown, requestedDays: number): AnalyticsSummary {
  if (!isRecord(raw)) throw new Error('analytics/summary: ответ не является объектом');

  const energyRaw = isRecord(raw.energy) ? raw.energy : {};
  const energySeries = asArray(energyRaw.series).flatMap((item): EnergyPoint[] => {
    if (!isRecord(item)) return [];
    const date = isoDay(item.date);
    return date ? [{ date, value: nullableNumber(item.value) }] : [];
  });

  const readingRaw = isRecord(raw.reading) ? raw.reading : {};
  const financeRaw = isRecord(raw.finance) ? raw.finance : {};
  const habitsRaw = isRecord(raw.habits) ? raw.habits : {};
  const tasksRaw = isRecord(raw.tasks) ? raw.tasks : {};

  const spheres = asArray(raw.spheres).flatMap((item): SphereSlice[] => {
    if (!isRecord(item)) return [];
    const id = Math.round(num(item.id));
    const name = str(item.name).trim();
    if (!id || !name) return [];
    return [
      {
        id,
        name,
        color: str(item.color),
        done: Math.max(0, Math.round(num(item.done))),
        total: Math.max(0, Math.round(num(item.total))),
        share: Math.max(0, Math.min(1, num(item.share))),
        pct: Math.max(0, Math.min(100, num(item.pct))),
      },
    ];
  });

  const financeSeries = asArray(financeRaw.series).flatMap((item): FinanceDay[] => {
    if (!isRecord(item)) return [];
    const date = isoDay(item.date);
    return date
      ? [{ date, income: num(item.income), expense: num(item.expense) }]
      : [];
  });

  const habitSeries = asArray(habitsRaw.series).flatMap((item): HabitDay[] => {
    if (!isRecord(item)) return [];
    const date = isoDay(item.date);
    return date
      ? [{ date, marks: Math.max(0, Math.round(num(item.marks))), pct: num(item.pct) }]
      : [];
  });

  const correlations = asArray(raw.correlations).flatMap((item, index) => {
    const pair = normalizePair(item, index);
    return pair ? [pair] : [];
  });

  return {
    days: Math.max(1, Math.round(num(raw.days, requestedDays))),
    from: isoDay(raw.from) ?? '',
    to: isoDay(raw.to) ?? '',
    energy: {
      series: energySeries,
      avg: nullableNumber(energyRaw.avg),
      daysWithData: Math.max(0, Math.round(num(energyRaw.daysWithData))),
    },
    spheres,
    reading: {
      pages: Math.max(0, Math.round(num(readingRaw.pages))),
      booksDone: Math.max(0, Math.round(num(readingRaw.booksDone))),
      pagesAll: Math.max(0, Math.round(num(readingRaw.pagesAll))),
      booksDoneAll: Math.max(0, Math.round(num(readingRaw.booksDoneAll))),
      booksReading: Math.max(0, Math.round(num(readingRaw.booksReading))),
    },
    finance: {
      income: num(financeRaw.income),
      expense: num(financeRaw.expense),
      balance: num(financeRaw.balance),
      series: financeSeries,
    },
    habits: {
      pct: num(habitsRaw.pct),
      habits: Math.max(0, Math.round(num(habitsRaw.habits))),
      marks: Math.max(0, Math.round(num(habitsRaw.marks))),
      series: habitSeries,
    },
    tasks: {
      done: Math.max(0, Math.round(num(tasksRaw.done))),
      total: Math.max(0, Math.round(num(tasksRaw.total))),
      pct: num(tasksRaw.pct),
    },
    correlations,
  };
}

/* ── Запросы ─────────────────────────────────────────────── */

export async function fetchAnalyticsSummary(days: number): Promise<AnalyticsSummary> {
  const raw = await apiRequest<unknown>(
    `/analytics/summary?days=${encodeURIComponent(String(days))}`,
    undefined,
    TIMEOUT_MS,
  );
  return normalizeSummary(raw, days);
}

export interface AnalyticsState {
  data: AnalyticsSummary | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

export function useAnalyticsSummary(days: number): AnalyticsState {
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const next = await fetchAnalyticsSummary(days);
      if (!alive.current) return;
      setData(next);
    } catch (problem) {
      if (!alive.current) return;
      setError(apiErrorMessage(problem, 'Сервер не ответил'));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  return { data, loading, error, reload: load };
}

/* ── Находки: GET /api/analytics/findings ─────────────────── */

export type FindingStrength = 'weak' | 'moderate' | 'strong';

/** Причина, по которой пара не стала находкой (сервер, aggregates.notReady). */
export type NotReadyReason = 'insufficient_data' | 'no_variance' | 'fdr';

/** Человеческие названия рядов: notReady приходит ключами, без подписей. */
export const SERIES_TITLES: Readonly<Record<string, string>> = {
  sleep: 'Сон',
  energy: 'Энергия',
  mood: 'Настроение',
  water: 'Вода',
  weight: 'Вес',
  training: 'Тренировки',
  steps: 'Шаги',
  reading: 'Чтение',
  expenses: 'Расходы',
  income: 'Доходы',
  tasks: 'Закрытые задачи',
  habits: 'Привычки',
};

export function seriesTitle(key: string): string {
  return SERIES_TITLES[key] ?? key;
}

export interface AnalyticsFinding {
  left: string;
  right: string;
  leftLabel: string;
  rightLabel: string;
  /** Сдвиг правого ряда в днях: 0 — тот же день, 1 — следующий. */
  lag: number;
  r: number;
  rho: number | null;
  n: number;
  p: number | null;
  /** q-value после поправки на множественные сравнения. */
  q: number | null;
  /** Доля разброса правого ряда, объяснённая левым, 0..100. */
  effectPct: number | null;
  strength: FindingStrength;
  /**
   * Короткая формулировка сервера («Сон и тренировки идут вместе»,
   * «Сон вчера, энергия сегодня»). Та же строка уходит в Обзор, поэтому экраны
   * не могут назвать одну находку по-разному. Пустая — у старых ответов.
   */
  title: string;
  /** «Что это значит» — готовая фраза сервера. */
  text: string;
  /** «Что проверить» — конкретное действие, без обещаний причинности. */
  advice: string;
  ci: [number, number] | null;
}

export interface NotReadyPair {
  left: string;
  right: string;
  reason: NotReadyReason;
  daysHave: number;
  needDays: number;
}

export interface FindingsWindow {
  from: string;
  to: string;
  days: number;
}

export interface AnalyticsFindings {
  window: FindingsWindow;
  /** Сколько пар × лагов реально протестировано. */
  tested: number;
  found: AnalyticsFinding[];
  notReady: NotReadyPair[];
}

const NOT_READY_REASONS: readonly NotReadyReason[] = ['insufficient_data', 'no_variance', 'fdr'];

function normalizeStrength(value: unknown, r: number): FindingStrength {
  if (value === 'weak' || value === 'moderate' || value === 'strong') return value;
  const abs = Math.abs(r);
  if (abs >= 0.5) return 'strong';
  if (abs >= 0.3) return 'moderate';
  return 'weak';
}

function normalizeFinding(raw: unknown): AnalyticsFinding | null {
  if (!isRecord(raw)) return null;
  const r = nullableNumber(raw.r);
  // Находка без коэффициента не рисуется: пустая карточка хуже, чем её отсутствие.
  if (r === null) return null;
  const left = str(raw.left);
  const right = str(raw.right);
  const leftLabel = str(raw.leftLabel, str(raw.left_label, seriesTitle(left)));
  const rightLabel = str(raw.rightLabel, str(raw.right_label, seriesTitle(right)));
  const rawCi = asArray(raw.ci).flatMap((value): number[] => {
    const number = nullableNumber(value);
    return number === null ? [] : [number];
  });

  return {
    left,
    right,
    leftLabel,
    rightLabel,
    lag: Math.max(0, Math.round(num(raw.lag))),
    r,
    rho: nullableNumber(raw.rho),
    n: Math.max(0, Math.round(num(raw.n))),
    p: nullableNumber(raw.p),
    q: nullableNumber(raw.q),
    effectPct: nullableNumber(raw.effectPct),
    strength: normalizeStrength(raw.strength, r),
    title: str(raw.title),
    text: str(raw.text),
    advice: str(raw.advice),
    ci: rawCi.length === 2 ? [rawCi[0]!, rawCi[1]!] : null,
  };
}

function normalizeNotReady(raw: unknown): NotReadyPair | null {
  if (!isRecord(raw)) return null;
  const left = str(raw.left);
  const right = str(raw.right);
  if (!left || !right) return null;
  const reason = (NOT_READY_REASONS as readonly string[]).includes(str(raw.reason))
    ? (str(raw.reason) as NotReadyReason)
    : 'insufficient_data';
  return {
    left,
    right,
    reason,
    daysHave: Math.max(0, Math.round(num(raw.daysHave))),
    needDays: Math.max(1, Math.round(num(raw.needDays, 14))),
  };
}

function normalizeFindings(raw: unknown): AnalyticsFindings {
  if (!isRecord(raw)) throw new Error('analytics/findings: ответ не является объектом');
  const windowRaw = isRecord(raw.window) ? raw.window : {};
  return {
    window: {
      from: isoDay(windowRaw.from) ?? '',
      to: isoDay(windowRaw.to) ?? '',
      days: Math.max(1, Math.round(num(windowRaw.days))),
    },
    tested: Math.max(0, Math.round(num(raw.tested))),
    // Сервер уже отдаёт top-5; слайс защищает экран от случайного расширения контракта.
    found: asArray(raw.found)
      .flatMap((item): AnalyticsFinding[] => {
        const finding = normalizeFinding(item);
        return finding ? [finding] : [];
      })
      .slice(0, 5),
    notReady: asArray(raw.notReady).flatMap((item): NotReadyPair[] => {
      const pair = normalizeNotReady(item);
      return pair ? [pair] : [];
    }),
  };
}

export async function fetchAnalyticsFindings(days: number): Promise<AnalyticsFindings> {
  const raw = await apiRequest<unknown>(
    `/analytics/findings?days=${encodeURIComponent(String(days))}`,
    undefined,
    TIMEOUT_MS,
  );
  return normalizeFindings(raw);
}

export interface AnalyticsFindingsState {
  data: AnalyticsFindings | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

export function useAnalyticsFindings(days: number): AnalyticsFindingsState {
  const [data, setData] = useState<AnalyticsFindings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const next = await fetchAnalyticsFindings(days);
      if (!alive.current) return;
      setData(next);
    } catch (problem) {
      if (!alive.current) return;
      setError(apiErrorMessage(problem, 'Сервер не ответил'));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  return { data, loading, error, reload: load };
}
