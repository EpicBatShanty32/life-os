/**
 * Клиент «Личного» и «Итогов» (handoff §12.5) и задачи, которые эти экраны
 * меняют: перенос даты (PATCH /api/tasks/{id}), отмена (DELETE) и отметка
 * привычки (POST /api/habits/{id}/mark) — контракт §12.2.
 *
 * Все запросы идут через apiRequest из ./client: тот же токен владельца,
 * Accept/JSON и таймаут. Формы ответов нормализуются терпимо: недостающие
 * поля не выдумываются, а пустые значения остаются пустыми (честные подписи).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { formatDateWithYear, parseIsoDate } from '../lib/format';
import { apiRequest } from './client';
import { apiErrorMessage } from './errors';

const TIMEOUT_MS = 6000;

export type ReviewPeriod = 'week' | 'month';

export interface Checkin {
  date: string;
  energy: number;
  mood: string;
  note: string;
}

export interface HabitSummary {
  id: number;
  name: string;
  emoji: string;
  /** Categorical color from the API; rendered through dataColor(). */
  color: string;
  targetPerWeek: number;
  streak: number;
  /** Отметки только за текущую неделю: {'YYYY-MM-DD': 1}. */
  marks: Record<string, 1>;
  /** Семь дат недели, от старой к новой. */
  week: string[];
  doneWeek: number;
}

export interface TaskCounts {
  must: number;
  important: number;
  low: number;
  done: number;
  total: number;
}

export interface DayEnergy {
  date: string;
  value: number | null;
}

export interface PersonalSummary {
  date: string;
  checkin: Checkin | null;
  habits: HabitSummary[];
  tasks: TaskCounts;
  reading: { pagesToday: number; pagesWeek: number };
  /** Энергия за 7 дней; пропуски — value: null, не нули. */
  energy7: DayEnergy[];
}

export interface PersonalTask {
  id: number;
  title: string;
  tags: string[];
  dueDate: string | null;
  resource: string;
  listKind: string;
}

export interface ReviewFacts {
  tasksDone: number;
  tasksTotal: number;
  pagesRead: number;
  energyAvg: number | null;
  habitsPct: number;
  days: number;
}

export interface ReviewRecord {
  id: number | null;
  period: ReviewPeriod;
  startDate: string;
  endDate: string;
  wins: string;
  misses: string;
  focus: string;
  care: string;
  facts: ReviewFacts & { demo?: boolean };
  created: string;
}

export interface ReviewOpenTask {
  id: number;
  title: string;
  dueDate: string;
}

export interface ReviewView {
  period: ReviewPeriod;
  startDate: string;
  endDate: string;
  existing: ReviewRecord | null;
  facts: ReviewFacts;
  openTasks: ReviewOpenTask[];
}

export interface ReviewInput {
  period: ReviewPeriod;
  startDate: string;
  wins: string;
  misses: string;
  focus: string;
  care: string;
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

function int(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** ISO-день; всё остальное — null, чтобы не рисовать выдуманные даты. */
function isoDay(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const day = value.slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null;
}

function normalizeCheckin(raw: unknown): Checkin | null {
  if (!isRecord(raw)) return null;
  const date = isoDay(raw.date);
  const energy = int(raw.energy, 0);
  if (!date || energy < 1 || energy > 5) return null;
  return { date, energy, mood: str(raw.mood), note: str(raw.note) };
}

function normalizeMarks(raw: unknown): Record<string, 1> {
  if (!isRecord(raw)) return {};
  const marks: Record<string, 1> = {};
  for (const [key, value] of Object.entries(raw)) {
    const day = isoDay(key);
    if (day && typeof value === 'number' && value > 0) marks[day] = 1;
  }
  return marks;
}

function normalizeHabit(raw: unknown): HabitSummary | null {
  if (!isRecord(raw)) return null;
  const id = int(raw.id, 0);
  const name = str(raw.name).trim();
  if (!id || !name) return null;
  const week = asArray(raw.week).flatMap((day) => {
    const iso = isoDay(day);
    return iso ? [iso] : [];
  });
  const marks = normalizeMarks(raw.marks);
  return {
    id,
    name,
    emoji: str(raw.emoji),
    color: str(raw.color),
    targetPerWeek: int(raw.targetPerWeek, 0),
    streak: int(raw.streak, 0),
    marks,
    week,
    doneWeek:
      week.length > 0
        ? week.filter((day) => marks[day] === 1).length
        : Object.keys(marks).length,
  };
}

function normalizeCounts(raw: unknown): TaskCounts {
  const source = isRecord(raw) ? raw : {};
  return {
    must: int(source.must),
    important: int(source.important),
    low: int(source.low),
    done: int(source.done),
    total: int(source.total),
  };
}

function normalizeSummary(raw: unknown): PersonalSummary {
  if (!isRecord(raw)) throw new Error('personal/summary: ответ не является объектом');
  const reading = isRecord(raw.reading) ? raw.reading : {};
  const energy7 = asArray(raw.energy7).flatMap((item): DayEnergy[] => {
    if (!isRecord(item)) return [];
    const date = isoDay(item.date);
    return date ? [{ date, value: nullableNumber(item.value) }] : [];
  });
  const habits = asArray(raw.habits).flatMap((item) => {
    const habit = normalizeHabit(item);
    return habit ? [habit] : [];
  });
  return {
    date: isoDay(raw.date) ?? '',
    checkin: normalizeCheckin(raw.checkin),
    habits,
    tasks: normalizeCounts(raw.tasks),
    reading: {
      pagesToday: int(reading.pagesToday),
      pagesWeek: int(reading.pagesWeek),
    },
    energy7,
  };
}

function normalizeTask(raw: unknown): PersonalTask | null {
  if (!isRecord(raw)) return null;
  const id = int(raw.id, 0);
  const title = str(raw.title).trim();
  if (!id || !title) return null;
  const tags = (Array.isArray(raw.tags) ? raw.tags : [str(raw.tags)])
    .flatMap((tag) => (typeof tag === 'string' && tag.trim() ? [tag.trim()] : []));
  return {
    id,
    title,
    tags,
    dueDate: isoDay(raw.dueDate),
    resource: str(raw.resource, 'important'),
    listKind: str(raw.listKind, 'personal'),
  };
}

function normalizeFacts(raw: unknown): ReviewFacts {
  const source = isRecord(raw) ? raw : {};
  return {
    tasksDone: int(source.tasksDone),
    tasksTotal: int(source.tasksTotal),
    pagesRead: int(source.pagesRead),
    energyAvg: nullableNumber(source.energyAvg),
    habitsPct: int(source.habitsPct),
    days: int(source.days),
  };
}

function normalizeReview(raw: unknown): ReviewRecord | null {
  if (!isRecord(raw)) return null;
  const period = raw.period === 'month' ? 'month' : 'week';
  const startDate = isoDay(raw.startDate);
  const endDate = isoDay(raw.endDate);
  if (!startDate || !endDate) return null;
  const factsRaw = isRecord(raw.facts) ? raw.facts : {};
  return {
    id: typeof raw.id === 'number' ? int(raw.id) : null,
    period,
    startDate,
    endDate,
    wins: str(raw.wins),
    misses: str(raw.misses),
    focus: str(raw.focus),
    care: str(raw.care),
    facts: { ...normalizeFacts(factsRaw), demo: factsRaw.demo === true },
    created: str(raw.created),
  };
}

function normalizeReviewView(raw: unknown): ReviewView {
  if (!isRecord(raw)) throw new Error('personal/review: ответ не является объектом');
  const period = raw.period === 'month' ? 'month' : 'week';
  const openTasks = asArray(raw.openTasks).flatMap((item): ReviewOpenTask[] => {
    if (!isRecord(item)) return [];
    const id = int(item.id, 0);
    const title = str(item.title).trim();
    const dueDate = isoDay(item.dueDate);
    if (!id || !title || !dueDate) return [];
    return [{ id, title, dueDate }];
  });
  return {
    period,
    startDate: isoDay(raw.startDate) ?? '',
    endDate: isoDay(raw.endDate) ?? '',
    existing: normalizeReview(raw.existing),
    facts: normalizeFacts(raw.facts),
    openTasks,
  };
}

/* ── Запросы ─────────────────────────────────────────────── */

export async function fetchPersonalSummary(date?: string): Promise<PersonalSummary> {
  const query = date ? `?date=${encodeURIComponent(date)}` : '';
  const raw = await apiRequest<unknown>(`/personal/summary${query}`, undefined, TIMEOUT_MS);
  return normalizeSummary(raw);
}

export interface CheckinInput {
  date?: string;
  energy: number;
  mood?: string;
  note?: string;
}

export async function saveCheckin(input: CheckinInput): Promise<Checkin> {
  const raw = await apiRequest<unknown>(
    '/personal/checkin',
    { method: 'POST', body: JSON.stringify(input) },
    TIMEOUT_MS,
  );
  const checkin = normalizeCheckin(raw);
  if (!checkin) throw new Error('personal/checkin: сервер не вернул чек-ин');
  return checkin;
}

export async function fetchReview(period: ReviewPeriod, start?: string): Promise<ReviewView> {
  const params = new URLSearchParams({ period });
  if (start) params.set('start', start);
  const raw = await apiRequest<unknown>(
    `/personal/review?${params.toString()}`,
    undefined,
    TIMEOUT_MS,
  );
  return normalizeReviewView(raw);
}

export async function saveReview(input: ReviewInput): Promise<ReviewRecord> {
  const raw = await apiRequest<unknown>(
    '/personal/review',
    { method: 'POST', body: JSON.stringify(input) },
    TIMEOUT_MS,
  );
  const record = normalizeReview(raw);
  if (!record) throw new Error('personal/review: сервер не вернул итог');
  return record;
}

export async function fetchReviews(limit = 12): Promise<ReviewRecord[]> {
  const raw = await apiRequest<unknown>(`/personal/reviews?limit=${limit}`, undefined, TIMEOUT_MS);
  if (!isRecord(raw)) return [];
  return asArray(raw.items).flatMap((item) => {
    const record = normalizeReview(item);
    return record ? [record] : [];
  });
}

/** Личные задачи: все открытые, чтобы разложить по тегам на клиенте. */
export async function fetchPersonalTasks(): Promise<PersonalTask[]> {
  const raw = await apiRequest<unknown>(
    '/tasks?view=kanban&listKind=personal&status=todo',
    undefined,
    TIMEOUT_MS,
  );
  if (!isRecord(raw)) return [];
  return asArray(raw.items).flatMap((item) => {
    const task = normalizeTask(item);
    return task ? [task] : [];
  });
}

/** Перенос задачи на конкретный день (null снимает дату). */
export async function setTaskDue(taskId: number, dueDate: string | null): Promise<void> {
  await apiRequest(
    `/tasks/${taskId}`,
    { method: 'PATCH', body: JSON.stringify({ dueDate }) },
    TIMEOUT_MS,
  );
}

/** «Отменить без вины»: статус cancelled. */
export async function cancelTask(taskId: number): Promise<void> {
  await apiRequest(`/tasks/${taskId}`, { method: 'DELETE' }, TIMEOUT_MS);
}

/** Отметка привычки за день через существующий эндпоинт /api/habits/{id}/mark. */
export async function setHabitMark(habitId: number, day: string, value: number): Promise<void> {
  await apiRequest(
    `/habits/${habitId}/mark`,
    { method: 'POST', body: JSON.stringify({ day, value }) },
    TIMEOUT_MS,
  );
}

/** Начало периода, в котором лежит дата: неделя — понедельник, месяц — 1-е. */
export function periodStart(day: string, period: ReviewPeriod): string {
  if (period === 'month') return `${day.slice(0, 7)}-01`;
  const date = new Date(`${day}T00:00:00`);
  const weekday = (date.getDay() + 6) % 7; // 0 = понедельник
  date.setDate(date.getDate() - weekday);
  return isoFromDate(date);
}

/** Сдвиг периода на delta (в неделях или месяцах), ISO-день на выходе. */
export function shiftPeriod(start: string, period: ReviewPeriod, delta: number): string {
  const date = new Date(`${start}T00:00:00`);
  if (period === 'month') {
    date.setDate(1);
    date.setMonth(date.getMonth() + delta);
  } else {
    date.setDate(date.getDate() + delta * 7);
  }
  return isoFromDate(date);
}

function isoFromDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** Подпись периода: «16–22 ноября 2026» или «28 октября — 3 ноября 2026». */
export function formatPeriodRange(start: string, end: string): string {
  const first = parseIsoDate(start);
  const last = parseIsoDate(end);
  if (first.getFullYear() !== last.getFullYear()) {
    return `${formatDateWithYear(first)} — ${formatDateWithYear(last)}`;
  }
  if (first.getMonth() === last.getMonth()) {
    return `${first.getDate()}–${formatDateWithYear(last)}`;
  }
  const startLabel = formatDateWithYear(first).replace(/\s\d{4}$/u, '');
  return `${startLabel} — ${formatDateWithYear(last)}`;
}

/**
 * Отметки привычек за 4 недели: /api/personal/summary отдаёт только 7 дней,
 * поэтому берём четыре недели якорями и объединяем отметки. Данные настоящие,
 * запросов ровно столько, сколько недель в сетке.
 */
export async function fetchHabitMonth(
  anchor: string,
  weeks = 4,
): Promise<Map<number, Set<string>>> {
  const anchors = Array.from({ length: weeks }, (_, index) => shiftPeriod(anchor, 'week', -index));
  const summaries = await Promise.all(anchors.map((day) => fetchPersonalSummary(day)));
  const marks = new Map<number, Set<string>>();
  for (const summary of summaries) {
    for (const habit of summary.habits) {
      const days = marks.get(habit.id) ?? new Set<string>();
      for (const day of Object.keys(habit.marks)) days.add(day);
      marks.set(habit.id, days);
    }
  }
  return marks;
}

/**
 * Палитра из API — это данные (цвет привычки или сферы приходит hex-строкой),
 * а в интерфейсе живут только токены. Здесь единственное место, где hex
 * встречается в коде этих экранов: известные цвета сводятся к семейству
 * cat-1..5, неизвестный показываем нейтральной точкой, а не выдуманным цветом.
 */
export type ColorFamily = 1 | 2 | 3 | 4 | 5 | 'accent';

const COLOR_FAMILIES: Readonly<Record<string, ColorFamily>> = {
  '#8e76a8': 1,
  '#7fa890': 2,
  '#c98f74': 3,
  '#7e98bc': 4,
  '#b08e5e': 5,
  // прежняя палитра v3, которой ещё могут быть покрашены записи
  '#a18bb7': 1,
  '#aedec2': 2,
  '#f2c9b6': 3,
  '#b9cbe8': 4,
  '#7c639b': 'accent',
};

export function colorFamily(value: string): ColorFamily | null {
  return COLOR_FAMILIES[value.trim().toLowerCase()] ?? null;
}

export function dataColor(value: string): string {
  const family = colorFamily(value);
  if (family === null) return 'var(--text-tertiary)';
  return family === 'accent' ? 'var(--accent)' : `var(--cat-${family})`;
}

/** Тон ProgressBar для цвета из API — та же семья, что у точки сферы. */
export function progressTone(
  value: string,
): 'accent' | 'cat-1' | 'cat-2' | 'cat-3' | 'cat-4' | 'cat-5' {
  const family = colorFamily(value);
  if (family === null) return 'cat-1';
  return family === 'accent' ? 'accent' : `cat-${family}`;
}

/* ── Хуки ────────────────────────────────────────────────── */

export interface PersonalState {
  data: PersonalSummary | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  /** Оптимистичное изменение сводки: страница откатывается через reload(). */
  apply: (updater: (previous: PersonalSummary) => PersonalSummary) => void;
}

export function usePersonalSummary(date?: string): PersonalState {
  const [data, setData] = useState<PersonalSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const next = await fetchPersonalSummary(date);
      if (!alive.current) return;
      setData(next);
    } catch (problem) {
      if (!alive.current) return;
      setError(apiErrorMessage(problem, 'Сервер не ответил'));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [date]);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  const apply = useCallback((updater: (previous: PersonalSummary) => PersonalSummary) => {
    setData((previous) => (previous ? updater(previous) : previous));
  }, []);

  return { data, loading, error, reload: load, apply };
}

export interface PersonalTasksState {
  tasks: PersonalTask[];
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  setTasks: (updater: (previous: PersonalTask[]) => PersonalTask[]) => void;
}

export function usePersonalTasks(): PersonalTasksState {
  const [tasks, setTasks] = useState<PersonalTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      const next = await fetchPersonalTasks();
      if (!alive.current) return;
      setTasks(next);
    } catch (problem) {
      if (!alive.current) return;
      setError(apiErrorMessage(problem, 'Сервер не ответил'));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  const setTasksOptimistic = useCallback(
    (updater: (previous: PersonalTask[]) => PersonalTask[]) => {
      setTasks(updater);
    },
    [],
  );

  return { tasks, loading, error, reload: load, setTasks: setTasksOptimistic };
}
