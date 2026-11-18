/** Локальный трекер использования — то, что видит только браузер.
 *
 *  Сервер уже пишет события в `usage_events` и отдаёт сводку
 *  ``GET /api/analytics/usage`` (t21): визиты по экранам, покрытие функций,
 *  тепловая карта и темы мыслей. Но у сводки нет двух вещей, которые нужны
 *  экрану «Статистика»:
 *
 *  * сколько времени человек провёл на экране (событие — это факт загрузки
 *    данных, а не внимание);
 *  * маршруты «экран → экран» и лента последних визитов с длительностью.
 *
 *  Поэтому время, переходы и лента считаются здесь, в браузере, лежат в
 *  localStorage и никуда не отправляются — «только на этом компьютере».
 *  Модуль стартует один раз из `main.tsx`; при смене hash пишется визит,
 *  раз в 15 секунд отрезок времени дописывается к текущему экрану, при
 *  уходе со страницы всё сохраняется.
 *
 *  Пауза сбора (кнопка на экране) останавливает именно этот трекер: серверные
 *  счётчики событий она не трогает, и экран говорит об этом честно.
 */

import { useSyncExternalStore } from 'react';
import { toLocalIso } from './format';
import { readRaw, writeRaw } from './storage';

const STORAGE_KEY = 'lifeos.usage.tracker';
const PAUSED_KEY = 'lifeos.usage.paused';

/** Как часто дописываем время к текущему экрану. */
const TICK_MS = 15_000;
/** Сколько последних записей ленты держим (лента на экране — 100 строк). */
const MAX_EVENTS = 240;
/** Сколько переходов держим (нужны для топа маршрутов). */
const MAX_LINKS = 600;
/** Простой дольше получаса не считаем вниманием. */
const MAX_SEGMENT_MS = 30 * 60_000;
/** Дневные корзины старше этого срока выбрасываем. */
const KEEP_DAYS = 200;
/** Переходы старше этого срока не нужны: окно экрана максимум 90 дней. */
const LINK_KEEP_DAYS = 120;
/** Сколько событий отдаём в отчёт. */
const REPORT_EVENTS = 100;

export type TrackerAction = 'view' | 'pause' | 'resume' | 'export';

export interface TrackerEvent {
  /** Путь экрана: `/`, `/nutrition`, `/usage`. */
  screen: string;
  action: TrackerAction;
  /** Момент события, epoch ms. */
  at: number;
  /** Сколько пробыл на экране; 0 — ещё открыт или это не визит. */
  durationMs: number;
}

export interface TrackerScreenRow {
  path: string;
  /** Ключ экрана в терминах сервера: `/` → `overview`, `/today` → `today`. */
  key: string;
  seconds: number;
  visits: number;
}

export interface TrackerTransition {
  from: string;
  fromKey: string;
  to: string;
  toKey: string;
  count: number;
}

export interface TrackerReport {
  paused: boolean;
  firstSeen: number | null;
  from: string;
  to: string;
  days: number;
  totalSeconds: number;
  visits: number;
  activeDays: number;
  screens: TrackerScreenRow[];
  transitions: TrackerTransition[];
  events: TrackerEvent[];
  lastEventAt: number | null;
}

export interface TrackerSnapshot {
  /** Меняется при каждом обновлении — повод пересчитать отчёт. */
  revision: number;
  paused: boolean;
  firstSeen: number | null;
  lastEventAt: number | null;
  secondsToday: number;
  visitsToday: number;
}

interface DayCell {
  seconds: number;
  visits: number;
}

interface TrackerLink {
  from: string;
  to: string;
  at: number;
}

interface TrackerState {
  version: 1;
  firstSeen: number | null;
  /** Дата → экран → время и число визитов. */
  days: Record<string, Record<string, DayCell>>;
  events: TrackerEvent[];
  links: TrackerLink[];
}

const ACTIONS: readonly TrackerAction[] = ['view', 'pause', 'resume', 'export'];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function emptyState(): TrackerState {
  return { version: 1, firstSeen: null, days: {}, events: [], links: [] };
}

function readCell(raw: unknown): DayCell | null {
  if (!isRecord(raw)) return null;
  const seconds = Math.max(0, Math.round(num(raw.seconds)));
  const visits = Math.max(0, Math.round(num(raw.visits)));
  if (seconds === 0 && visits === 0) return null;
  return { seconds, visits };
}

function readEvent(raw: unknown): TrackerEvent | null {
  if (!isRecord(raw)) return null;
  const screen = typeof raw.screen === 'string' ? raw.screen : '';
  const action = ACTIONS.includes(raw.action as TrackerAction)
    ? (raw.action as TrackerAction)
    : null;
  const at = num(raw.at);
  if (!screen || !action || at <= 0) return null;
  return { screen, action, at, durationMs: Math.max(0, Math.round(num(raw.durationMs))) };
}

function readLink(raw: unknown): TrackerLink | null {
  if (!isRecord(raw)) return null;
  const from = typeof raw.from === 'string' ? raw.from : '';
  const to = typeof raw.to === 'string' ? raw.to : '';
  const at = num(raw.at);
  if (!from || !to || from === to || at <= 0) return null;
  return { from, to, at };
}

/** Терпимая загрузка: битые записи отбрасываем, экран не роняем. */
function loadState(): TrackerState {
  const raw = readRaw(STORAGE_KEY);
  if (!raw) return emptyState();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return emptyState();
  }
  if (!isRecord(parsed)) return emptyState();

  const state = emptyState();
  state.firstSeen = parsed.firstSeen ? num(parsed.firstSeen, 0) || null : null;

  if (isRecord(parsed.days)) {
    Object.entries(parsed.days).forEach(([date, screens]) => {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !isRecord(screens)) return;
      const cells: Record<string, DayCell> = {};
      Object.entries(screens).forEach(([screen, cell]) => {
        const parsedCell = readCell(cell);
        if (parsedCell) cells[screen] = parsedCell;
      });
      if (Object.keys(cells).length > 0) state.days[date] = cells;
    });
  }

  if (Array.isArray(parsed.events)) {
    state.events = parsed.events
      .map(readEvent)
      .filter((item): item is TrackerEvent => item !== null)
      .slice(-MAX_EVENTS);
  }

  if (Array.isArray(parsed.links)) {
    state.links = parsed.links
      .map(readLink)
      .filter((item): item is TrackerLink => item !== null)
      .slice(-MAX_LINKS);
  }

  return state;
}

let state = loadState();
let paused = readRaw(PAUSED_KEY) === '1';
let started = false;
let timer: number | null = null;
/** Текущий отрезок внимания: экран и момент начала. */
let active: { screen: string; since: number } | null = null;

let revision = 0;
const listeners = new Set<() => void>();
let snapshot: TrackerSnapshot = buildSnapshot();

function buildSnapshot(): TrackerSnapshot {
  const cells = state.days[toLocalIso()] ?? {};
  let seconds = 0;
  let visits = 0;
  Object.values(cells).forEach((cell) => {
    seconds += cell.seconds;
    visits += cell.visits;
  });
  const last = state.events[state.events.length - 1];
  return {
    revision,
    paused,
    firstSeen: state.firstSeen,
    lastEventAt: last ? last.at : null,
    secondsToday: seconds,
    visitsToday: visits,
  };
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function emit(): void {
  revision += 1;
  snapshot = buildSnapshot();
  listeners.forEach((listener) => listener());
}

/* ── Состояние ───────────────────────────────────────────── */

function pathFromHash(): string {
  const clean = window.location.hash.replace(/^#/, '').split('?')[0] ?? '';
  if (!clean || clean === '/') return '/';
  const withSlash = clean.startsWith('/') ? clean : `/${clean}`;
  return withSlash.replace(/\/+$/, '') || '/';
}

function cellFor(date: string, screen: string): DayCell {
  const day = state.days[date] ?? (state.days[date] = {});
  return day[screen] ?? (day[screen] = { seconds: 0, visits: 0 });
}

function lastViewedScreen(): string | null {
  for (let index = state.events.length - 1; index >= 0; index -= 1) {
    const event = state.events[index];
    if (event && event.action === 'view') return event.screen;
  }
  return null;
}

function prune(): void {
  const keepFrom = new Date();
  keepFrom.setDate(keepFrom.getDate() - KEEP_DAYS);
  const keepFromIso = toLocalIso(keepFrom);
  Object.keys(state.days).forEach((date) => {
    if (date < keepFromIso) delete state.days[date];
  });

  const linkFrom = Date.now() - LINK_KEEP_DAYS * 24 * 60 * 60 * 1000;
  if (state.links.length > 0) {
    state.links = state.links.filter((link) => link.at >= linkFrom).slice(-MAX_LINKS);
  }
}

function persist(): void {
  prune();
  writeRaw(
    STORAGE_KEY,
    JSON.stringify({
      version: 1,
      firstSeen: state.firstSeen,
      days: state.days,
      events: state.events.slice(-MAX_EVENTS),
      links: state.links.slice(-MAX_LINKS),
    }),
  );
}

function addEvent(event: TrackerEvent): void {
  state.events.push(event);
  if (state.events.length > MAX_EVENTS) {
    state.events = state.events.slice(-MAX_EVENTS);
  }
}

/** Закрывает последний открытый визит: длительность = сколько пробыл. */
function closeOpenView(now: number): void {
  for (let index = state.events.length - 1; index >= 0; index -= 1) {
    const event = state.events[index];
    if (!event || event.action !== 'view') continue;
    if (event.durationMs === 0) {
      event.durationMs = Math.min(Math.max(0, now - event.at), MAX_SEGMENT_MS);
    }
    return;
  }
}

/** Дописывает время текущего экрана к корзине сегодняшнего дня. */
function accountSegment(now: number): boolean {
  if (!active) return false;
  const elapsed = now - active.since;
  if (elapsed < 1000) {
    active = { screen: active.screen, since: now };
    return false;
  }
  const capped = Math.min(elapsed, MAX_SEGMENT_MS);
  cellFor(toLocalIso(new Date(now)), active.screen).seconds += Math.round(capped / 1000);
  active = { screen: active.screen, since: now };
  return true;
}

function beginView(path: string): void {
  if (!state.firstSeen) state.firstSeen = Date.now();
  const now = Date.now();
  accountSegment(now);

  if (paused) {
    active = null;
    persist();
    emit();
    return;
  }

  const previous = lastViewedScreen();
  closeOpenView(now);
  if (previous && previous !== path) {
    state.links.push({ from: previous, to: path, at: now });
    if (state.links.length > MAX_LINKS) state.links = state.links.slice(-MAX_LINKS);
  }
  addEvent({ screen: path, action: 'view', at: now, durationMs: 0 });
  cellFor(toLocalIso(new Date(now)), path).visits += 1;
  active = { screen: path, since: now };
  persist();
  emit();
}

function onHashChange(): void {
  const path = pathFromHash();
  if (path === lastViewedScreen()) return; // повторный переход на тот же экран — не визит
  beginView(path);
}

function onVisibilityChange(): void {
  if (document.visibilityState === 'hidden') {
    const now = Date.now();
    accountSegment(now);
    closeOpenView(now);
    active = null;
    persist();
    emit();
    return;
  }
  if (!paused && started) {
    active = { screen: pathFromHash(), since: Date.now() };
  }
}

function onPageHide(): void {
  const now = Date.now();
  accountSegment(now);
  closeOpenView(now);
  active = null;
  persist();
}

function tick(): void {
  if (paused || !active) return;
  if (document.visibilityState !== 'visible') return;
  if (!accountSegment(Date.now())) return;
  persist();
  emit();
}

/* ── Публичный интерфейс ─────────────────────────────────── */

/** Запуск сбора: один раз на приложение, повторный вызов ничего не делает. */
export function startUsageTracker(): void {
  if (started || typeof window === 'undefined') return;
  started = true;
  if (!state.firstSeen) state.firstSeen = Date.now();
  beginView(pathFromHash());
  timer = window.setInterval(tick, TICK_MS);
  window.addEventListener('hashchange', onHashChange);
  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener('pagehide', onPageHide);
}

/** Останавливает таймер и снимает слушатели (для полноты, в SPA не зовётся). */
export function stopUsageTracker(): void {
  if (!started) return;
  started = false;
  if (timer !== null) window.clearInterval(timer);
  timer = null;
  window.removeEventListener('hashchange', onHashChange);
  document.removeEventListener('visibilitychange', onVisibilityChange);
  window.removeEventListener('pagehide', onPageHide);
  active = null;
}

export function isCollectionPaused(): boolean {
  return paused;
}

/** Пауза и возобновление локального сбора; сама пауза попадает в ленту. */
export function setCollectionPaused(value: boolean): void {
  if (value === paused) return;
  const now = Date.now();
  accountSegment(now);
  closeOpenView(now);
  paused = value;
  writeRaw(PAUSED_KEY, value ? '1' : '0');
  const screen = pathFromHash();
  if (value) {
    active = null;
  } else {
    active = { screen, since: now };
  }
  addEvent({ screen, action: value ? 'pause' : 'resume', at: now, durationMs: 0 });
  persist();
  emit();
}

/** Заметное действие на экране (пауза, выгрузка отчёта) — попадает в ленту. */
export function logTrackerAction(action: TrackerAction): void {
  const now = Date.now();
  addEvent({ screen: pathFromHash(), action, at: now, durationMs: 0 });
  persist();
  emit();
}

/** Ключ экрана в терминах сервера: путь → запись каталога функций. */
export function trackerScreenKey(path: string): string {
  const clean = String(path || '/').split('?')[0] ?? '/';
  if (clean === '/' || clean === '') return 'overview';
  return clean.replace(/^\/+/, '').split('/')[0] || 'overview';
}

function windowStartIso(days: number): string {
  const start = new Date();
  start.setDate(start.getDate() - (Math.max(1, days) - 1));
  return toLocalIso(start);
}

/** Срез локальных данных за окно: время, визиты, переходы и лента. */
export function readTrackerReport(days: number): TrackerReport {
  const span = Math.max(1, Math.round(days));
  const from = windowStartIso(span);
  const to = toLocalIso();
  const screens = new Map<string, TrackerScreenRow>();
  let totalSeconds = 0;
  let visits = 0;
  let activeDays = 0;

  Object.entries(state.days).forEach(([date, cells]) => {
    if (date < from || date > to) return;
    let dayHasData = false;
    Object.entries(cells).forEach(([path, cell]) => {
      const row = screens.get(path) ?? {
        path,
        key: trackerScreenKey(path),
        seconds: 0,
        visits: 0,
      };
      row.seconds += cell.seconds;
      row.visits += cell.visits;
      screens.set(path, row);
      totalSeconds += cell.seconds;
      visits += cell.visits;
      if (cell.seconds > 0 || cell.visits > 0) dayHasData = true;
    });
    if (dayHasData) activeDays += 1;
  });

  const fromMs = new Date(`${from}T00:00:00`).getTime();
  const pairCounts = new Map<string, TrackerTransition>();
  state.links.forEach((link) => {
    if (link.at < fromMs) return;
    const key = `${link.from}\u0000${link.to}`;
    const row = pairCounts.get(key) ?? {
      from: link.from,
      to: link.to,
      fromKey: trackerScreenKey(link.from),
      toKey: trackerScreenKey(link.to),
      count: 0,
    };
    row.count += 1;
    pairCounts.set(key, row);
  });

  const events = state.events
    .filter((event) => event.at >= fromMs)
    .sort((left, right) => right.at - left.at)
    .slice(0, REPORT_EVENTS);

  const lastEvent = state.events[state.events.length - 1];

  return {
    paused,
    firstSeen: state.firstSeen,
    from,
    to,
    days: span,
    totalSeconds,
    visits,
    activeDays,
    screens: [...screens.values()].sort(
      (left, right) => right.seconds - left.seconds || right.visits - left.visits,
    ),
    transitions: [...pairCounts.values()].sort((left, right) => right.count - left.count),
    events,
    lastEventAt: lastEvent ? lastEvent.at : null,
  };
}

/** Снимок для React: меняется при каждом обновлении трекера. */
export function useUsageTracker(): TrackerSnapshot {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
}
