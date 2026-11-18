import { useCallback, useEffect, useState } from 'react';
import { toLocalIso } from '../lib/format';
import { apiRequest } from './client';
import type { DataSource } from './types';

/* ─────────────────────────────────────────────────────────────
   Практики и очки — живой API t33 (studio/PRACTICES.md).

   Контракт (camelCase, реализация — app/server/api/practices.py):
     GET  /api/practices
          → { practices[], count, points: {balance, level, streak, cap}, dayCap }
     GET  /api/practices/{slug}
          → { practice, points, dayCap }        (карточка — на будущее)
     POST /api/practice-sessions
          { slug, roundsDone, bestHoldS, source, startedAt, finishedAt }
          → { pointsEarned, awards[], recordUpdated, bestHoldS,
              roundsDone, streak, total, level, cap, … }
     GET  /api/points
          → { balance, level, streak, cap, days[], totalsByKind, … }

   Каталог приходит «как в базе»: шаги одной схемы (phase/seconds/rounds)
   и уровни (rounds + holds либо cycleSeconds). Плееру нужны готовые
   раунды, поэтому развёртка уровня в раунды живёт здесь: экран и плеер
   не знают, что на сервере практика может иметь несколько уровней.

   Очки: 10 за практику, +5 за новый рекорд задержки, +2 за день серии
   (кап 20), кап дня — 60. Одна сессия на практику в день: повтор не
   начисляет второй раз, и это видно в ответе (`awards[].duplicate`),
   чтобы итог говорил «уже учтено», а не «0 очков» без объяснений.
   ───────────────────────────────────────────────────────────── */

export type PracticeKind = 'breathing' | 'stretch' | 'other';

export type PhaseKind = 'inhale' | 'hold' | 'exhale' | 'holdOut' | 'rest' | 'pulse' | 'pose';

export interface PracticePhase {
  kind: PhaseKind;
  seconds: number;
  /** Для `pulse`: сколько вдохов-выдохов укладывается в отрезок. */
  cycles?: number;
  /** Своя подпись фазы: «Шея и плечи», «Задержка на выдохе». */
  label?: string;
}

export interface PracticeRound {
  phases: PracticePhase[];
}

/** Уровень практики: сколько раундов и какие задержки (как на сервере). */
export interface PracticeLevel {
  key: string;
  title: string;
  rounds: number;
  holds: number[];
  cycleSeconds: number | null;
}

export interface Practice {
  /** Слаг — он же id в API. */
  slug: string;
  title: string;
  kind: PracticeKind;
  summary: string;
  safetyNote: string;
  /** Длина выбранного уровня в секундах и в минутах для каталога. */
  seconds: number;
  minutes: number;
  rounds: PracticeRound[];
  level: PracticeLevel | null;
  /** Сколько даёт завершённая практика (PRACTICES.md, POINTS["practice"]). */
  points: number;
  recordHoldSec: number | null;
  recordLabel: string | null;
  streakDays: number;
  lastDate: string | null;
  doneCount: number;
  /** Сегодня эта практика уже засчитана: повтор очков не принесёт. */
  todayDone: boolean;
}

export interface LevelState {
  key: string;
  title: string;
  min: number;
  next: { title: string; min: number } | null;
  toNext: number;
  progress: number;
}

export interface StreakState {
  days: number;
  best: number;
  lastDate: string | null;
  alive: boolean;
}

export interface CapState {
  day: number;
  today: number;
  remaining: number;
}

export interface PointsState {
  balance: number;
  level: LevelState;
  streak: StreakState;
  cap: CapState;
  dayCap: number;
}

export interface PracticesPayload {
  items: Practice[];
  points: PointsState;
  source: DataSource;
}

/** Точка истории очков: один календарный день. */
export interface PointsDay {
  date: string;
  points: number;
}

export interface PointsHistory extends PointsState {
  days: PointsDay[];
  /** Сколько очков за окно и в лучший день — для подписи под полосками. */
  sum: number;
  bestDay: number;
  /** Разбивка по видам начислений за окно (практика, рекорд, серия, …). */
  totals: Record<string, number>;
}

export interface PracticeSessionInput {
  roundsDone: number;
  bestHoldSec: number;
  /** Настенное время начала — по нему сервер отличает ретрай от новой сессии. */
  startedAt: string;
  finishedAt: string;
}

export interface PracticeAward {
  kind: string;
  points: number;
  reason: string;
  duplicate: boolean;
  capped: boolean;
}

export interface PracticeResult {
  pointsEarned: number;
  totalPoints: number;
  streakDays: number;
  recordHoldSec: number | null;
  recordLabel: string | null;
  isRecord: boolean;
  roundsDone: number;
  awards: PracticeAward[];
  cap: CapState;
  /** Уровень по балансу после начисления: его видно в итоге. */
  level: LevelState;
  /** Повтор в тот же день: очков нет, потому что уже начислены. */
  duplicated: boolean;
  /** Очков нет, потому что кап дня выбран. */
  capped: boolean;
}

const PRACTICE_POINTS = 10;
const DEFAULT_DAY_CAP = 60;

/* ── Разбор ответа: поля проверяем, недостающее — нейтральным ── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
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

function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function int(value: unknown, fallback = 0, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  return Math.min(max, Math.max(min, Math.round(num(value, fallback))));
}

const KIND_ALIAS: Record<string, PracticeKind> = {
  breathing: 'breathing',
  breath: 'breathing',
  stretch: 'stretch',
  mobility: 'stretch',
  other: 'other',
};

/** Фаза из шага API → фаза плеера. Неизвестное не роняем, а показываем статично. */
const PHASE_KIND: Record<string, PhaseKind> = {
  breath: 'pulse',
  pulse: 'pulse',
  inhale: 'inhale',
  exhale: 'exhale',
  hold: 'hold',
  recover: 'rest',
  rest: 'rest',
  move: 'pose',
  pose: 'pose',
  cold: 'pose',
};

function phaseFromStep(raw: unknown): PracticePhase | null {
  if (!isRecord(raw)) return null;
  const seconds = int(raw.seconds, 0, 1, 3600);
  if (seconds <= 0) return null;
  const kind = PHASE_KIND[str(raw.phase, '').toLowerCase()] ?? 'pose';
  const label = str(raw.label, '');

  if (kind === 'pulse') {
    const cycles = int(raw.rounds, 1, 1, 200);
    return {
      kind,
      seconds: seconds * cycles,
      cycles,
      ...(label ? { label } : {}),
    };
  }
  // У боксового дыхания вторая задержка — после выдоха: подпись различает их.
  if (kind === 'hold' && /после\s+выдох/i.test(label)) {
    return { kind: 'holdOut', seconds, ...(label ? { label } : {}) };
  }
  return { kind, seconds, ...(label ? { label } : {}) };
}

function levelFrom(raw: unknown): PracticeLevel | null {
  if (!isRecord(raw)) return null;
  const key = str(raw.key, '');
  const title = str(raw.title, '');
  if (!key) return null;
  const holds = arr(raw.holds)
    .map((value) => int(value, 0, 1, 3600))
    .filter((value) => value > 0);
  return {
    key,
    title: title || key,
    rounds: int(raw.rounds, 1, 1, 100),
    holds,
    cycleSeconds: raw.cycleSeconds === undefined ? null : int(raw.cycleSeconds, 0, 0, 3600) || null,
  };
}

/**
 * Уровень → готовые раунды плеера.
 *
 * Один раунд — это все шаги практики по порядку. `holds` перекрывают
 * длительность задержек по раундам (Вим Хоф: 1:30 → 2:00 → 2:30),
 * `rounds` задаёт число повторов (4-7-8: четыре дыхательных цикла).
 */
function expandRounds(steps: PracticePhase[], level: PracticeLevel | null): PracticeRound[] {
  if (steps.length === 0) return [];
  const count = level?.rounds ?? 1;
  const holds = level?.holds ?? [];
  return Array.from({ length: count }, (_, index) => ({
    phases: steps.map((phase) => {
      if (holds.length === 0) return { ...phase };
      if (phase.kind !== 'hold' && phase.kind !== 'holdOut') return { ...phase };
      const hold = holds[Math.min(index, holds.length - 1)];
      return { ...phase, seconds: hold };
    }),
  }));
}

function totalSeconds(rounds: readonly PracticeRound[]): number {
  return rounds.reduce(
    (sum, round) => sum + round.phases.reduce((inner, phase) => inner + phase.seconds, 0),
    0,
  );
}

function buildPractice(raw: unknown): Practice | null {
  if (!isRecord(raw)) return null;
  const slug = str(raw.slug, '');
  const title = str(raw.title, '');
  if (!slug || !title) return null;

  const steps = arr(raw.steps)
    .map(phaseFromStep)
    .filter((phase): phase is PracticePhase => phase !== null);
  const levels = arr(raw.levels)
    .map(levelFrom)
    .filter((level): level is PracticeLevel => level !== null);
  const defaultKey = str(raw.defaultLevel, '');
  const level =
    levels.find((item) => item.key === defaultKey) ?? levels.find((item) => item.rounds > 0) ?? null;

  const rounds = expandRounds(steps, level);
  if (rounds.length === 0) return null;

  const seconds = totalSeconds(rounds);
  const stats = isRecord(raw.stats) ? raw.stats : {};
  const recordHoldSec = int(stats.bestHoldS, 0, 0, 3600);
  const lastDate = str(stats.lastDate, '') || null;

  return {
    slug,
    title,
    kind: KIND_ALIAS[str(raw.kind, '').toLowerCase()] ?? 'other',
    summary: str(raw.description, ''),
    safetyNote: str(raw.safetyNote, ''),
    seconds,
    minutes: Math.max(1, Math.round(seconds / 60)),
    rounds,
    level,
    points: PRACTICE_POINTS,
    recordHoldSec: recordHoldSec > 0 ? recordHoldSec : null,
    recordLabel: str(stats.bestHoldLabel, '') || null,
    streakDays: int(stats.streakDays, 0, 0, 3650),
    lastDate,
    doneCount: int(stats.sessions, 0, 0, 100000),
    // Сегодняшняя отметка важна до старта: повтор не даст очков второй раз.
    todayDone: lastDate === toLocalIso(),
  };
}

function capFrom(raw: unknown): CapState {
  const source = isRecord(raw) ? raw : {};
  const day = int(source.day, DEFAULT_DAY_CAP, 1, 1000);
  const today = int(source.today, 0, 0, day);
  return { day, today, remaining: int(source.remaining, Math.max(0, day - today), 0, day) };
}

function levelFromState(raw: unknown): LevelState {
  const source = isRecord(raw) ? raw : {};
  const next = isRecord(source.next) ? source.next : null;
  return {
    key: str(source.key, 'novice'),
    title: str(source.title, 'Новичок'),
    min: int(source.min, 0, 0),
    next: next ? { title: str(next.title, ''), min: int(next.min, 0, 0) } : null,
    toNext: int(source.toNext, 0, 0),
    progress: Math.min(1, Math.max(0, num(source.progress, 0))),
  };
}

function streakFromState(raw: unknown): StreakState {
  const source = isRecord(raw) ? raw : {};
  return {
    days: int(source.days, 0, 0, 3650),
    best: int(source.best, 0, 0, 3650),
    lastDate: str(source.lastDate, '') || null,
    alive: bool(source.alive, false),
  };
}

function pointsFrom(raw: unknown, dayCap: number): PointsState {
  const source = isRecord(raw) ? raw : {};
  return {
    balance: int(source.balance, 0, 0),
    level: levelFromState(source.level),
    streak: streakFromState(source.streak),
    cap: capFrom(source.cap),
    dayCap,
  };
}

/* ── Запросы ─────────────────────────────────────────────── */

export async function loadPractices(): Promise<PracticesPayload> {
  const raw = await apiRequest<unknown>('/practices');
  const body = isRecord(raw) ? raw : {};
  const dayCap = int(body.dayCap, DEFAULT_DAY_CAP, 1, 1000);
  const items = arr(body.practices)
    .map(buildPractice)
    .filter((practice): practice is Practice => practice !== null);
  return { items, points: pointsFrom(body.points, dayCap), source: 'api' };
}

function resultFrom(raw: unknown, input: PracticeSessionInput): PracticeResult {
  const body = isRecord(raw) ? raw : {};
  const awards = arr(body.awards).flatMap((item): PracticeAward[] => {
    if (!isRecord(item)) return [];
    return [
      {
        kind: str(item.kind, 'practice'),
        points: int(item.points, 0, 0, 100),
        reason: str(item.reason, ''),
        duplicate: bool(item.duplicate, false),
        capped: bool(item.capped, false),
      },
    ];
  });
  const bestHold = int(body.bestHoldS, 0, 0, 3600);
  const cap = capFrom(body.cap);
  return {
    pointsEarned: int(body.pointsEarned, 0, 0, 200),
    totalPoints: int(body.total, 0, 0),
    streakDays: streakFromState(body.streak).days,
    recordHoldSec: bestHold > 0 ? bestHold : null,
    recordLabel: str(body.bestHoldLabel, '') || null,
    isRecord: bool(body.recordUpdated, false),
    roundsDone: int(body.roundsDone, input.roundsDone, 0, 100),
    awards,
    cap,
    level: levelFromState(body.level),
    duplicated: awards.some((award) => award.duplicate),
    capped: cap.remaining === 0 || awards.some((award) => award.capped),
  };
}

export async function submitPracticeSession(
  practice: Practice,
  input: PracticeSessionInput,
): Promise<PracticeResult> {
  const raw = await apiRequest<unknown>(
    '/practice-sessions',
    {
      method: 'POST',
      body: JSON.stringify({
        slug: practice.slug,
        roundsDone: input.roundsDone,
        bestHoldS: input.bestHoldSec,
        source: 'web',
        startedAt: input.startedAt,
        finishedAt: input.finishedAt,
      }),
    },
    6000,
  );
  return resultFrom(raw, input);
}

/* ── Хук экрана ──────────────────────────────────────────── */

/** GET /api/points: баланс, уровень, серия, кап и история по дням за окно. */
export async function loadPointsHistory(days = 30): Promise<PointsHistory> {
  const raw = await apiRequest<unknown>(`/points?days=${days}&recent=0`);
  const body = isRecord(raw) ? raw : {};
  const pointDays = arr(body.days).flatMap((item): PointsDay[] => {
    if (!isRecord(item)) return [];
    const date = str(item.date, '');
    if (!date) return [];
    return [{ date, points: Math.max(0, Math.round(num(item.points, 0))) }];
  });
  const totals: Record<string, number> = {};
  if (isRecord(body.totalsByKind)) {
    Object.entries(body.totalsByKind).forEach(([kind, value]) => {
      totals[kind] = Math.max(0, Math.round(num(value, 0)));
    });
  }
  const cap = capFrom(body.cap);
  return {
    balance: int(body.balance, 0, 0),
    level: levelFromState(body.level),
    streak: streakFromState(body.streak),
    cap,
    dayCap: cap.day,
    days: pointDays,
    sum: pointDays.reduce((sum, day) => sum + day.points, 0),
    bestDay: pointDays.reduce((best, day) => Math.max(best, day.points), 0),
    totals,
  };
}

export interface PointsHistoryState {
  data: PointsHistory | null;
  loading: boolean;
  error: boolean;
  refresh: () => void;
}

export function usePointsHistory(days = 30): PointsHistoryState {
  const [data, setData] = useState<PointsHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    loadPointsHistory(days)
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setError(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days, nonce]);

  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  return { data, loading, error, refresh };
}

export interface PracticesState {
  data: PracticesPayload | null;
  loading: boolean;
  error: boolean;
  source: DataSource;
  refresh: () => void;
}

export function usePractices(): PracticesState {
  const [data, setData] = useState<PracticesPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    loadPractices()
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setError(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  return { data, loading, error, source: data?.source ?? 'api', refresh };
}
