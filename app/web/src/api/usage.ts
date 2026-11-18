/**
 * Клиент «Статистики» (v4): локальная аналитика использования и темы мыслей.
 *
 * Источник — ``GET /api/analytics/usage?days=…&thoughtsDays=…`` (t21): события
 * веба и бота в одной таблице, покрытие каталога функций, тепловая карта
 * «день недели × час», тренд к прошлому окну и темы журнала. Ответ
 * нормализуется: страница не доверяет форме и переживает пропущенное поле.
 *
 * Чего в ответе нет и что даёт только браузер (см. `lib/usageTracker.ts`):
 * время на экране, переходы «экран → экран» и лента последних визитов.
 * Здесь же — чистые функции раскладки: каталог по разделам, действия,
 * слоты тепловой карты, форматтеры и выгрузка отчёта в JSON.
 */

import { formatDateShort, formatNumber, formatTime } from '../lib/format';
import type { TrackerReport } from '../lib/usageTracker';
import { apiRequest } from './client';

/** Окна экрана: неделя, месяц, квартал. */
export const USAGE_WINDOWS = [7, 30, 90] as const;
export type UsageWindowDays = (typeof USAGE_WINDOWS)[number];
export const DEFAULT_USAGE_WINDOW: UsageWindowDays = 30;

/** Темы мыслей живут дольше окна использования — так их видно на коротком окне. */
export const THOUGHTS_DAYS = 90;

export type UsageSourceKey = 'web' | 'bot';

export interface UsageWindow {
  from: string;
  to: string;
  days: number;
}

export interface UsageTotals {
  events: number;
  activeDays: number;
  days: number;
  web: number;
  bot: number;
  messages: number;
  commands: number;
  voice: number;
  media: number;
}

export interface UsageSource {
  key: UsageSourceKey;
  label: string;
  count: number;
  share: number;
  activeDays: number;
}

export interface UsageScreen {
  key: string;
  label: string;
  source: UsageSourceKey;
  count: number;
  share: number;
  lastUsed: string | null;
  actions: Record<string, number>;
}

export interface CoverageBucket {
  used: number;
  total: number;
  pct: number;
}

export interface CatalogFeature {
  key: string;
  label: string;
  source: UsageSourceKey;
}

export interface UsageCoverage extends CoverageBucket {
  web: CoverageBucket;
  bot: CoverageBucket;
  unused: CatalogFeature[];
}

export interface HeatPeak {
  weekday: number;
  hour: number;
  count: number;
  weekdayLabel: string;
}

export interface UsageHeatmap {
  weekdays: string[];
  hours: number[];
  matrix: number[][];
  byDay: Array<{ date: string; count: number }>;
  peak: HeatPeak | null;
}

export interface UsageTrend {
  previousEvents: number;
  changePct: number | null;
  previousActiveDays: number;
  activeDaysChange: number;
}

export interface ThoughtTopic {
  topic: string;
  label: string;
  count: number;
  prevCount: number;
  growthPct: number | null;
  firstSeen: string | null;
  lastSeen: string | null;
  isNew: boolean;
}

export interface UsageThoughts {
  window: UsageWindow | null;
  entries: number;
  prevEntries: number;
  topics: ThoughtTopic[];
  rising: ThoughtTopic[];
  new: ThoughtTopic[];
  note: string | null;
}

export interface UsageData {
  window: UsageWindow;
  totals: UsageTotals;
  sources: UsageSource[];
  screens: UsageScreen[];
  coverage: UsageCoverage;
  heatmap: UsageHeatmap;
  trend: UsageTrend;
  thoughts: UsageThoughts;
}

/* ── Нормализация ответа ─────────────────────────────────── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function optionalNum(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function int(value: unknown): number {
  return Math.max(0, Math.round(num(value)));
}

function share(value: unknown): number {
  return Math.min(1, Math.max(0, num(value)));
}

function sourceKey(value: unknown): UsageSourceKey {
  return value === 'bot' ? 'bot' : 'web';
}

function dateText(value: unknown): string | null {
  const text = str(value);
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : null;
}

function readActions(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  const actions: Record<string, number> = {};
  Object.entries(value).forEach(([key, count]) => {
    const parsed = int(count);
    if (key && parsed > 0) actions[key] = parsed;
  });
  return actions;
}

function readMatrix(value: unknown): number[][] {
  const rows = asArray(value).map((row) =>
    asArray(row).map((cell) => int(cell)),
  );
  // Ровно 7 дней недели: сервер отдаёт 7, но форма ответа не должна ронять экран.
  const matrix: number[][] = [];
  for (let index = 0; index < 7; index += 1) {
    const row = rows[index] ?? [];
    matrix.push(Array.from({ length: 24 }, (_, hour) => row[hour] ?? 0));
  }
  return matrix;
}

function normalizeFeature(raw: unknown): CatalogFeature | null {
  if (!isRecord(raw)) return null;
  const key = str(raw.key);
  if (!key) return null;
  return { key, label: str(raw.label, key), source: sourceKey(raw.source) };
}

function normalizeTopic(raw: unknown): ThoughtTopic | null {
  if (!isRecord(raw)) return null;
  const topic = str(raw.topic);
  if (!topic) return null;
  return {
    topic,
    label: str(raw.label, topic),
    count: int(raw.count),
    prevCount: int(raw.prevCount),
    growthPct: optionalNum(raw.growthPct),
    firstSeen: dateText(raw.firstSeen),
    lastSeen: dateText(raw.lastSeen),
    isNew: raw.isNew === true,
  };
}

function readTopics(value: unknown): ThoughtTopic[] {
  return asArray(value)
    .map(normalizeTopic)
    .filter((item): item is ThoughtTopic => item !== null);
}

export function normalizeUsage(raw: unknown): UsageData {
  const payload = isRecord(raw) && isRecord(raw.data) ? raw.data : raw;
  if (!isRecord(payload)) throw new Error('usage: ответ не является объектом');

  const windowRaw = isRecord(payload.window) ? payload.window : {};
  const totalsRaw = isRecord(payload.totals) ? payload.totals : {};
  const coverageRaw = isRecord(payload.coverage) ? payload.coverage : {};
  const heatmapRaw = isRecord(payload.heatmap) ? payload.heatmap : {};
  const trendRaw = isRecord(payload.trend) ? payload.trend : {};
  const thoughtsRaw = isRecord(payload.thoughts) ? payload.thoughts : {};

  const coverageBucket = (value: unknown, fallbackTotal: number): CoverageBucket => {
    const bucket = isRecord(value) ? value : {};
    const total = int(bucket.total) || fallbackTotal;
    const used = Math.min(int(bucket.used), total);
    return { used, total, pct: num(bucket.pct, total ? (used / total) * 100 : 0) };
  };

  const unused = asArray(coverageRaw.unused)
    .map(normalizeFeature)
    .filter((item): item is CatalogFeature => item !== null);

  const coverageTotal = int(coverageRaw.total) || 54;
  const coverageUsed = Math.min(int(coverageRaw.used), coverageTotal);

  const peakRaw = isRecord(heatmapRaw.peak) ? heatmapRaw.peak : null;
  const weekdays = asArray(heatmapRaw.weekdays).map((item, index) => {
    const text = str(item);
    return text || ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][index] || '';
  });

  return {
    window: {
      from: dateText(windowRaw.from) ?? '',
      to: dateText(windowRaw.to) ?? '',
      days: int(windowRaw.days) || DEFAULT_USAGE_WINDOW,
    },
    totals: {
      events: int(totalsRaw.events),
      activeDays: int(totalsRaw.activeDays),
      days: int(totalsRaw.days) || DEFAULT_USAGE_WINDOW,
      web: int(totalsRaw.web),
      bot: int(totalsRaw.bot),
      messages: int(totalsRaw.messages),
      commands: int(totalsRaw.commands),
      voice: int(totalsRaw.voice),
      media: int(totalsRaw.media),
    },
    sources: asArray(payload.sources).flatMap((item): UsageSource[] => {
      if (!isRecord(item)) return [];
      const key = sourceKey(item.key);
      return [
        {
          key,
          label: str(item.label, key === 'web' ? 'Веб' : 'Бот'),
          count: int(item.count),
          share: share(item.share),
          activeDays: int(item.activeDays),
        },
      ];
    }),
    screens: asArray(payload.screens).flatMap((item): UsageScreen[] => {
      if (!isRecord(item)) return [];
      const key = str(item.key);
      if (!key) return [];
      return [
        {
          key,
          label: str(item.label, key),
          source: sourceKey(item.source),
          count: int(item.count),
          share: share(item.share),
          lastUsed: dateText(item.lastUsed),
          actions: readActions(item.actions),
        },
      ];
    }),
    coverage: {
      used: coverageUsed,
      total: coverageTotal,
      pct: num(coverageRaw.pct, coverageTotal ? (coverageUsed / coverageTotal) * 100 : 0),
      web: coverageBucket(coverageRaw.web, 19),
      bot: coverageBucket(coverageRaw.bot, 35),
      unused,
    },
    heatmap: {
      weekdays: weekdays.length === 7 ? weekdays : ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
      hours: Array.from({ length: 24 }, (_, hour) => hour),
      matrix: readMatrix(heatmapRaw.matrix),
      byDay: asArray(heatmapRaw.byDay).flatMap((item) => {
        if (!isRecord(item)) return [];
        const date = dateText(item.date);
        return date ? [{ date, count: int(item.count) }] : [];
      }),
      peak: peakRaw
        ? {
            weekday: Math.min(6, Math.max(0, int(peakRaw.weekday))),
            hour: Math.min(23, Math.max(0, int(peakRaw.hour))),
            count: int(peakRaw.count),
            weekdayLabel: str(peakRaw.weekdayLabel),
          }
        : null,
    },
    trend: {
      previousEvents: int(trendRaw.previousEvents),
      changePct: optionalNum(trendRaw.changePct),
      previousActiveDays: int(trendRaw.previousActiveDays),
      activeDaysChange: Math.round(num(trendRaw.activeDaysChange)),
    },
    thoughts: {
      window: isRecord(thoughtsRaw.window)
        ? {
            from: dateText(thoughtsRaw.window.from) ?? '',
            to: dateText(thoughtsRaw.window.to) ?? '',
            days: int(thoughtsRaw.window.days) || THOUGHTS_DAYS,
          }
        : null,
      entries: int(thoughtsRaw.entries),
      prevEntries: int(thoughtsRaw.prevEntries),
      topics: readTopics(thoughtsRaw.topics),
      rising: readTopics(thoughtsRaw.rising),
      new: readTopics(thoughtsRaw.new),
      note: typeof thoughtsRaw.note === 'string' ? thoughtsRaw.note : null,
    },
  };
}

/** GET /api/analytics/usage — сводка использования и темы мыслей. */
export async function fetchUsage(days: number): Promise<UsageData> {
  const span = Math.max(1, Math.round(days));
  const raw = await apiRequest<unknown>(
    `/analytics/usage?days=${span}&thoughtsDays=${THOUGHTS_DAYS}`,
    undefined,
    6000,
  );
  return normalizeUsage(raw);
}

/* ── Раскладка данных ────────────────────────────────────── */

export interface CatalogRow extends CatalogFeature {
  used: boolean;
  count: number;
  lastUsed: string | null;
}

export interface CatalogGroup {
  id: 'web' | 'intents' | 'commands';
  title: string;
  hint: string;
  rows: CatalogRow[];
  used: number;
  total: number;
}

/** Полный каталог функций по разделам: использованные сверху, «ни разу» ниже. */
export function catalogGroups(data: UsageData): CatalogGroup[] {
  const rows = new Map<string, CatalogRow>();
  const id = (source: string, key: string): string => `${source}:${key}`;

  data.coverage.unused.forEach((feature) => {
    rows.set(id(feature.source, feature.key), {
      ...feature,
      used: false,
      count: 0,
      lastUsed: null,
    });
  });
  data.screens.forEach((screen) => {
    rows.set(id(screen.source, screen.key), {
      key: screen.key,
      label: screen.label,
      source: screen.source,
      used: true,
      count: screen.count,
      lastUsed: screen.lastUsed,
    });
  });

  const all = [...rows.values()];
  const groups: CatalogGroup[] = [
    {
      id: 'web',
      title: 'Веб-экраны',
      hint: 'Экраны приложения, которые открывались в браузере',
      rows: all.filter((row) => row.source === 'web'),
      used: 0,
      total: 0,
    },
    {
      id: 'intents',
      title: 'Бот: сообщения',
      hint: 'Что бот распознал в переписке',
      rows: all.filter((row) => row.source === 'bot' && !row.key.startsWith('/')),
      used: 0,
      total: 0,
    },
    {
      id: 'commands',
      title: 'Бот: команды',
      hint: 'Явные команды с косой чертой',
      rows: all.filter((row) => row.source === 'bot' && row.key.startsWith('/')),
      used: 0,
      total: 0,
    },
  ];

  groups.forEach((group) => {
    group.rows.sort((left, right) => {
      if (left.used !== right.used) return left.used ? -1 : 1;
      return right.count - left.count || left.label.localeCompare(right.label, 'ru');
    });
    group.used = group.rows.filter((row) => row.used).length;
    group.total = group.rows.length;
  });

  return groups;
}

export const ACTION_LABELS: Record<string, string> = {
  view: 'Просмотр экрана',
  message: 'Сообщение боту',
  command: 'Команда боту',
  voice: 'Голосовое',
  media: 'Медиа',
  pause: 'Пауза сбора',
  resume: 'Сбор возобновлён',
  export: 'Выгрузка отчёта',
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

export interface ActionRow {
  action: string;
  label: string;
  count: number;
  share: number;
  /** Два первых экрана, где встречалось действие. */
  where: string;
}

/** «Частые действия»: сводим действия по всем экранам в одну таблицу. */
export function actionRows(data: UsageData): ActionRow[] {
  const totals = new Map<string, number>();
  const places = new Map<string, Map<string, number>>();

  data.screens.forEach((screen) => {
    Object.entries(screen.actions).forEach(([action, count]) => {
      totals.set(action, (totals.get(action) ?? 0) + count);
      const place = places.get(action) ?? new Map<string, number>();
      place.set(screen.label, (place.get(screen.label) ?? 0) + count);
      places.set(action, place);
    });
  });

  const all = [...totals.values()].reduce((sum, value) => sum + value, 0);
  return [...totals.entries()]
    .map(([action, count]) => {
      const top = [...(places.get(action) ?? new Map<string, number>()).entries()]
        .sort((left, right) => right[1] - left[1])
        .slice(0, 3)
        .map(([label]) => label);
      return {
        action,
        label: actionLabel(action),
        count,
        share: all ? count / all : 0,
        where: top.join(', '),
      };
    })
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

/** Слоты тепловой карты: 24 часа свёрнуты в 8 трёхчасовых полос. */
export const HEAT_SLOTS = 8;

export function heatSlotLabel(index: number): string {
  const from = index * 3;
  return `${from}–${from + 3}`;
}

export interface HeatGrid {
  /** 7 строк × 8 слотов: сумма событий за трёхчасовой интервал. */
  slots: number[][];
  max: number;
}

export function heatGrid(heatmap: UsageHeatmap): HeatGrid {
  const slots = heatmap.matrix.map((row) => {
    const cells = Array.from({ length: HEAT_SLOTS }, () => 0);
    row.forEach((value, hour) => {
      const index = Math.min(HEAT_SLOTS - 1, Math.floor(hour / 3));
      cells[index] = (cells[index] ?? 0) + value;
    });
    return cells;
  });
  const max = slots.reduce(
    (top, row) => row.reduce((inner, value) => Math.max(inner, value), top),
    0,
  );
  return { slots, max };
}

/** Подписи экранов из ответа: ключ → человеческое название.
 *
 *  Ключи веба и бота пересекаются (`journal`, `today`), поэтому веб-строка
 *  главнее: подпись бота («Мысль», «Сводка дня») не должна перекрывать
 *  название раздела, по которому ходит браузер.
 */
export function screenLabels(data: UsageData): Map<string, string> {
  const labels = new Map<string, string>();
  data.screens.forEach((screen) => {
    if (screen.source === 'web' || !labels.has(screen.key)) {
      labels.set(screen.key, screen.label);
    }
  });
  return labels;
}

/* ── Форматтеры ──────────────────────────────────────────── */

/** «2 ч 14 мин», «12 мин», «40 с» — для времени на экране. */
export function formatSpan(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${formatNumber(total)} с`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${formatNumber(minutes)} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${formatNumber(hours)} ч ${formatNumber(rest)} мин` : `${formatNumber(hours)} ч`;
}

/** Часы и минуты отдельно — для KPI «Активное время». */
export function formatSpanParts(seconds: number): { hours: number; minutes: number } {
  const minutes = Math.round(Math.max(0, seconds) / 60);
  return { hours: Math.floor(minutes / 60), minutes: minutes % 60 };
}

/** «01:48:12» по epoch ms — моноширинная лента. */
export function formatClock(at: number): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return '—';
  return `${formatTime(date)}:${String(date.getSeconds()).padStart(2, '0')}`;
}

/** «18.11, 01:48» — компактная отметка времени. */
export function formatStamp(at: number): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return '—';
  return `${formatDateShort(date)}, ${formatTime(date)}`;
}

/** «18.11» из ISO-даты или null. */
export function formatDay(value: string | null): string {
  return value ? formatDateShort(value) : '—';
}

export function percentText(value: number, digits = 0): string {
  return `${formatNumber(value, digits)} %`;
}

/* ── Отчёт JSON ──────────────────────────────────────────── */

export interface UsageReport {
  app: string;
  generatedAt: string;
  note: string;
  window: UsageWindow;
  usage: UsageData;
  tracker: TrackerReport;
}

export function buildUsageReport(data: UsageData, tracker: TrackerReport): UsageReport {
  return {
    app: 'Life OS',
    generatedAt: new Date().toISOString(),
    note:
      'Локальная аналитика: сводка из базы Life OS и данные браузерного трекера. ' +
      'Файл собран на этом компьютере.',
    window: data.window,
    usage: data,
    tracker,
  };
}

/** Складывает отчёт в файл и отдаёт браузеру на скачивание. */
export function downloadUsageReport(data: UsageData, tracker: TrackerReport): string {
  const report = buildUsageReport(data, tracker);
  const name = `life-os-usage-${data.window.to || 'report'}-${data.window.days}d.json`;
  const blob = new Blob([JSON.stringify(report, null, 2)], {
    type: 'application/json;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  // Освобождаем blob чуть позже: Safari успевает начать скачивание.
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  return name;
}
