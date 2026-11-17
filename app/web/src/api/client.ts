import { useCallback, useEffect, useRef, useState } from 'react';
import { toLocalIso, weekdayShort } from '../lib/format';
import { readJson, readNumber, readRaw, removeRaw, STORAGE_KEYS, writeJson, writeRaw } from '../lib/storage';
import { createMockOverview } from './mock';
import type {
  ActivityDay,
  AgendaItem,
  AgendaKind,
  DataSource,
  DayPoint,
  Insight,
  NavBadges,
  OverviewCourse,
  OverviewKpis,
  OverviewPayload,
  PostResult,
  QuickEntryInput,
  SyncState,
  SyncStatus,
} from './types';

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');
const REQUEST_TIMEOUT_MS = 3000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function num(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function str(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.trim() !== '' ? value : fallback;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function nonNegativeInt(value: unknown, fallback: number): number {
  return Math.max(0, Math.round(num(value, fallback)));
}

function readToken(): string | null {
  return readRaw(STORAGE_KEYS.token);
}

/** Токен владельца дублируем в cookie: так его видят и «сырые» fetch без заголовка. */
export function writeOwnerToken(token: string | null): void {
  const value = (token ?? '').trim();
  if (value) {
    writeRaw(STORAGE_KEYS.token, value);
    document.cookie = `lifeos_token=${encodeURIComponent(value)}; path=/; SameSite=Strict`;
  } else {
    removeRaw(STORAGE_KEYS.token);
    document.cookie = 'lifeos_token=; path=/; Max-Age=0; SameSite=Strict';
  }
}

// При старте приложения cookie могла пропасть (очистка браузера) — восстановим.
const storedToken = readToken();
if (storedToken) {
  try {
    document.cookie = `lifeos_token=${encodeURIComponent(storedToken)}; path=/; SameSite=Strict`;
  } catch {
    /* cookie недоступны — остаётся заголовок X-LifeOS-Token */
  }
}

async function request<T>(path: string, init?: RequestInit, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = new Headers(init?.headers);
  headers.set('Accept', 'application/json');
  if (init?.body) headers.set('Content-Type', 'application/json');
  const token = readToken();
  if (token) headers.set('X-LifeOS-Token', token);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`API ${path}: HTTP ${response.status}`);
    }
    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.includes('application/json')) {
      throw new Error(`API ${path}: ожидался JSON`);
    }
    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

function unwrapPayload(raw: unknown): Record<string, unknown> {
  if (!isRecord(raw)) throw new Error('overview: ответ не является объектом');
  if (isRecord(raw.data)) return raw.data;
  if (isRecord(raw.overview)) return raw.overview;
  return raw;
}

function normalizeSeries(raw: unknown, fallback: DayPoint[]): DayPoint[] {
  const series = asArray(raw).flatMap((item): DayPoint[] => {
    if (!isRecord(item)) return [];
    const date = str(item.date, '');
    const value = num(item.value, Number.NaN);
    if (!date || !Number.isFinite(value)) return [];
    return [{ date, value }];
  });
  return series.length > 0 ? series : fallback;
}

function normalizeCourse(raw: unknown): OverviewCourse | null {
  if (!isRecord(raw)) return null;
  const title = str(raw.title, '');
  if (!title) return null;
  const liked = raw.nextLessonId ?? raw.next_lesson_id;
  return {
    id: str(raw.id, 'course'),
    title,
    lessonTitle: str(raw.lessonTitle ?? raw.lesson_title, 'Следующий урок'),
    doneLessons: nonNegativeInt(raw.doneLessons ?? raw.done_lessons, 0),
    totalLessons: Math.max(1, nonNegativeInt(raw.totalLessons ?? raw.total_lessons, 1)),
    nextLessonId: typeof liked === 'string' && liked ? liked : undefined,
  };
}

const AGENDA_KINDS: readonly AgendaKind[] = ['habit', 'goal', 'workout', 'journal'];

function normalizeAgenda(raw: unknown): AgendaItem[] {
  return asArray(raw).flatMap((item, index): AgendaItem[] => {
    if (!isRecord(item)) return [];
    const title = str(item.title, '');
    if (!title) return [];
    const kindRaw = str(item.kind, 'habit');
    const kind = (AGENDA_KINDS as readonly string[]).includes(kindRaw)
      ? (kindRaw as AgendaKind)
      : 'habit';
    return [
      {
        id: str(item.id, `agenda-${index}`),
        title,
        meta: str(item.meta, ''),
        done: bool(item.done, false),
        kind,
      },
    ];
  });
}

function normalizeActivity(raw: unknown): ActivityDay[] {
  return asArray(raw).flatMap((item): ActivityDay[] => {
    if (!isRecord(item)) return [];
    const date = str(item.date, '');
    if (!date) return [];
    return [
      {
        date,
        label: str(item.label, weekdayShort(date)),
        minutes: Math.max(0, Math.round(num(item.minutes ?? item.value, 0))),
      },
    ];
  });
}

const SYNC_STATES: readonly SyncState[] = ['ok', 'syncing', 'error'];

function normalizeSync(raw: unknown, fallback: SyncStatus): SyncStatus {
  if (!isRecord(raw)) return fallback;
  const stateRaw = str(raw.state, fallback.state);
  const state = (SYNC_STATES as readonly string[]).includes(stateRaw)
    ? (stateRaw as SyncState)
    : fallback.state;
  return { state, at: str(raw.at, fallback.at) };
}

function normalizeNav(raw: unknown, fallback: NavBadges): NavBadges {
  if (!isRecord(raw)) return fallback;
  return {
    today: nonNegativeInt(raw.today, fallback.today),
    habits: nonNegativeInt(raw.habits, fallback.habits),
    goals: nonNegativeInt(raw.goals, fallback.goals),
    courses: nonNegativeInt(raw.courses, fallback.courses),
  };
}

function normalizeInsight(raw: unknown): Insight | null {
  if (!isRecord(raw)) return null;
  const text = str(raw.text, '');
  if (!text) return null;
  return {
    title: str(raw.title, 'Инсайт дня'),
    text,
    footnote: str(raw.footnote, ''),
  };
}

/**
 * Терпимая нормализация /api/overview: сервер Ф4 может отдавать поля
 * в camelCase или snake_case, а вложенное — как {data: {...}} или напрямую.
 * Если контракт неполный (нет kpis/weight) — бросаем ошибку и уходим в мок.
 */
export function normalizeOverview(raw: unknown): OverviewPayload {
  const payload = unwrapPayload(raw);
  const kpisRaw = payload.kpis;
  const weightRaw = payload.weight;
  if (!isRecord(kpisRaw) || !isRecord(weightRaw)) {
    throw new Error('overview: неполный контракт (нет kpis или weight)');
  }

  // Нейтральные значения по умолчанию: недостающие поля API не подменяются
  // демо-историей, чтобы интерфейс не выдавал мок за реальные данные.
  const defaults: OverviewPayload = {
    date: toLocalIso(),
    kpis: {
      streak: { days: 0, bestDays: 0 },
      checkins: { completed: 0, total: 0 },
      workouts: { week: 0, target: 0 },
      sleep: { lastHours: 0, avg7Hours: 0 },
      water: { ml: 0, targetMl: 0 },
      mood: { score: 0, max: 10, label: 'нет данных', avg7: 0 },
    },
    weight: { currentKg: 0, targetKg: 0, delta30Kg: 0, series: [] },
    course: null,
    agenda: [],
    activity: [],
    insight: null,
    sync: { state: 'ok', at: new Date().toISOString() },
    nav: { today: 0, habits: 0, goals: 0, courses: 0 },
  };

  const streakRaw = isRecord(kpisRaw.streak) ? kpisRaw.streak : {};
  const checkinsRaw = isRecord(kpisRaw.checkins) ? kpisRaw.checkins : {};
  const workoutsRaw = isRecord(kpisRaw.workouts) ? kpisRaw.workouts : {};
  const sleepRaw = isRecord(kpisRaw.sleep) ? kpisRaw.sleep : {};
  const waterRaw = isRecord(kpisRaw.water) ? kpisRaw.water : {};
  const moodRaw = isRecord(kpisRaw.mood) ? kpisRaw.mood : {};

  const kpis: OverviewKpis = {
    streak: {
      days: nonNegativeInt(streakRaw.days, defaults.kpis.streak.days),
      bestDays: nonNegativeInt(
        streakRaw.bestDays ?? streakRaw.best_days,
        defaults.kpis.streak.bestDays,
      ),
    },
    checkins: {
      completed: nonNegativeInt(checkinsRaw.completed, defaults.kpis.checkins.completed),
      total: nonNegativeInt(checkinsRaw.total, defaults.kpis.checkins.total),
    },
    workouts: {
      week: nonNegativeInt(workoutsRaw.week, defaults.kpis.workouts.week),
      target: nonNegativeInt(workoutsRaw.target, defaults.kpis.workouts.target),
    },
    sleep: {
      lastHours: num(
        sleepRaw.lastHours ?? sleepRaw.last_hours,
        defaults.kpis.sleep.lastHours,
      ),
      avg7Hours: num(
        sleepRaw.avg7Hours ?? sleepRaw.avg7_hours,
        defaults.kpis.sleep.avg7Hours,
      ),
    },
    water: {
      ml: nonNegativeInt(waterRaw.ml, defaults.kpis.water.ml),
      targetMl: nonNegativeInt(
        waterRaw.targetMl ?? waterRaw.target_ml,
        defaults.kpis.water.targetMl,
      ),
    },
    mood: {
      score: num(moodRaw.score, defaults.kpis.mood.score),
      max: Math.max(1, num(moodRaw.max, defaults.kpis.mood.max)),
      label: str(moodRaw.label, defaults.kpis.mood.label),
      avg7: num(moodRaw.avg7, defaults.kpis.mood.avg7),
    },
  };

  return {
    date: str(payload.date, defaults.date),
    kpis,
    weight: {
      currentKg: num(weightRaw.currentKg ?? weightRaw.current_kg, defaults.weight.currentKg),
      targetKg: num(weightRaw.targetKg ?? weightRaw.target_kg, defaults.weight.targetKg),
      delta30Kg: num(
        weightRaw.delta30Kg ?? weightRaw.delta30_kg,
        defaults.weight.delta30Kg,
      ),
      series: normalizeSeries(weightRaw.series, defaults.weight.series),
    },
    course: normalizeCourse(payload.course),
    agenda: Array.isArray(payload.agenda)
      ? normalizeAgenda(payload.agenda)
      : defaults.agenda,
    activity: Array.isArray(payload.activity)
      ? normalizeActivity(payload.activity)
      : defaults.activity,
    insight: normalizeInsight(payload.insight),
    sync: normalizeSync(payload.sync, defaults.sync),
    nav: normalizeNav(payload.nav, defaults.nav),
  };
}

export interface FetchedOverview {
  data: OverviewPayload;
  source: DataSource;
}

/** GET /api/overview с фолбэком на демо-данные (Ф4 ещё может быть не готов). */
export async function fetchOverview(): Promise<FetchedOverview> {
  try {
    const raw = await request<unknown>('/overview', undefined, 2500);
    return { data: normalizeOverview(raw), source: 'api' };
  } catch {
    return { data: createMockOverview(), source: 'mock' };
  }
}

function waterKey(date: string): string {
  return STORAGE_KEYS.waterExtra(date);
}

function readLocalWater(date: string): number {
  return Math.max(0, readNumber(waterKey(date), 0));
}

function addLocalWater(date: string, ml: number): void {
  writeRaw(waterKey(date), String(readLocalWater(date) + ml));
}

function withLocalWater(payload: OverviewPayload): OverviewPayload {
  const extra = readLocalWater(toLocalIso());
  if (extra <= 0) return payload;
  return {
    ...payload,
    kpis: {
      ...payload.kpis,
      water: { ...payload.kpis.water, ml: payload.kpis.water.ml + extra },
    },
  };
}

interface StoredQuickEntry extends QuickEntryInput {
  at: string;
}

function saveLocalQuickEntry(input: QuickEntryInput): void {
  const current = readJson<StoredQuickEntry[]>(STORAGE_KEYS.localQuick, []);
  const list = Array.isArray(current) ? current : [];
  writeJson(STORAGE_KEYS.localQuick, [
    ...list.slice(-19),
    { ...input, at: new Date().toISOString() },
  ]);
}

/** POST /api/water; при недоступном API вода сохраняется локально и не теряется. */
export async function postWater(ml: number): Promise<PostResult> {
  const date = toLocalIso();
  try {
    await request('/water', { method: 'POST', body: JSON.stringify({ ml, date }) }, 3000);
    return { ok: true, source: 'api' };
  } catch {
    addLocalWater(date, ml);
    return { ok: true, source: 'mock' };
  }
}

/** POST /api/quick; при недоступном API запись остаётся в локальном демо-буфере. */
export async function postQuick(input: QuickEntryInput): Promise<PostResult> {
  try {
    await request('/quick', { method: 'POST', body: JSON.stringify(input) }, 4000);
    return { ok: true, source: 'api' };
  } catch {
    saveLocalQuickEntry(input);
    return { ok: true, source: 'mock' };
  }
}

export interface OverviewState {
  data: OverviewPayload | null;
  source: DataSource;
  loading: boolean;
  refreshing: boolean;
  refresh: () => Promise<void>;
  addWater: (ml: number) => Promise<DataSource>;
}

/** Единая точка загрузки «Обзора»: данные нужны и странице, и сайдбару (бейджи, синк). */
export function useOverview(): OverviewState {
  const [data, setData] = useState<OverviewPayload | null>(null);
  const [source, setSource] = useState<DataSource>('mock');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setRefreshing(true);
    const result = await fetchOverview();
    if (!alive.current) return;
    setData(withLocalWater(result.data));
    setSource(result.source);
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  const addWater = useCallback(async (ml: number): Promise<DataSource> => {
    setData((previous) =>
      previous
        ? {
            ...previous,
            kpis: {
              ...previous.kpis,
              water: { ...previous.kpis.water, ml: previous.kpis.water.ml + ml },
            },
          }
        : previous,
    );
    const result = await postWater(ml);
    return result.source;
  }, []);

  return { data, source, loading, refreshing, refresh: load, addWater };
}
