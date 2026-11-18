/**
 * Клиент «Подключений» в настройках (t20): источники веса, импорт файла
 * и ручной ввод.
 *
 * Контракт — сервер t17 (`app/server/api/integrations.py`):
 *
 *   GET   /api/integrations              → [{key, title, kind, enabled, lastSync, notes}]
 *   PATCH /api/integrations/{key}        {enabled} → тот же объект
 *   POST  /api/health/weight/import      {source, points: [{date, weight}]}
 *   POST  /api/health/weight/import/csv  text/csv `date,weight` (?source=…)
 *          → {imported, skipped, firstDate, lastDate}
 *
 * Автоматических OAuth-интеграций нет: «подключение» здесь — это файл,
 * который выбирает человек, либо ручной ввод. Поэтому ручная запись уходит
 * тем же импортом с `source = 'manual'` (отдельного `POST /api/health/weight`
 * сервер не заводит), и дедупликация по паре «дата + источник» остаётся
 * серверной: повторный файл ничего не плодит.
 *
 * Запросы здесь свои, а не `apiRequest` из `client.ts`, по двум причинам:
 * CSV должен уйти как `text/csv` (общий клиент всегда ставит JSON), а ошибки
 * FastAPI приходят в `detail` — «Строка 3: вес не число» полезнее, чем
 * «HTTP 422». Оба случая — только эти четыре эндпоинта.
 */

import { addDays, formatDateShort, formatTime, pluralRu, toLocalIso } from '../lib/format';
import { readRaw, STORAGE_KEYS } from '../lib/storage';

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');
const TIMEOUT_MS = 15000;

/** Вид источника: загрузка файла или ручной ввод. */
export type IntegrationKind = 'import' | 'manual';

export interface WeightSource {
  key: string;
  title: string;
  kind: IntegrationKind;
  enabled: boolean;
  /** ISO-отметка последней синхронизации или null, если её не было. */
  lastSync: string | null;
  notes: string;
}

export interface WeightImportResult {
  imported: number;
  skipped: number;
  /** Диапазон дат, которые реально попали в базу (null, если всё было дублями). */
  firstDate: string | null;
  lastDate: string | null;
}

/** Ошибка API: `status` нужен экрану, чтобы отличить 409 «источник выключен». */
export class IntegrationsError extends Error {
  readonly status: number;

  constructor(message: string, status = 0) {
    super(message);
    this.name = 'IntegrationsError';
    this.status = status;
  }
}

/* ── Разбор ответа ───────────────────────────────────────── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
}

/** ISO-дата или дата-время; всё остальное — null. */
function isoOrNull(value: unknown): string | null {
  if (typeof value !== 'string' || value.trim() === '') return null;
  return Number.isNaN(new Date(value).getTime()) ? null : value;
}

/** Текст ошибки FastAPI: строка `detail`, список сообщений валидатора или HTTP-код. */
function detailOf(payload: unknown, status: number): string {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (isRecord(item) ? str(item.msg) : ''))
        .filter((item) => item !== '');
      if (messages.length) return messages.join('; ');
    }
  }
  return `HTTP ${status}`;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  contentType?: string,
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body) headers.set('Content-Type', contentType ?? 'application/json');
  const token = readRaw(STORAGE_KEYS.token);
  if (token) headers.set('X-LifeOS-Token', token);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      credentials: 'same-origin',
      signal: controller.signal,
    });
    const raw = await response.text();
    let payload: unknown = null;
    if (raw.trim()) {
      try {
        payload = JSON.parse(raw) as unknown;
      } catch {
        payload = null;
      }
    }
    if (!response.ok) throw new IntegrationsError(detailOf(payload, response.status), response.status);
    return payload as T;
  } catch (problem) {
    if (problem instanceof IntegrationsError) throw problem;
    if (controller.signal.aborted) {
      throw new IntegrationsError(`Нет ответа за ${Math.round(TIMEOUT_MS / 1000)} с`);
    }
    throw new IntegrationsError(
      problem instanceof Error && problem.message ? problem.message : 'Сеть недоступна',
    );
  } finally {
    window.clearTimeout(timer);
  }
}

function normalizeSource(raw: unknown): WeightSource | null {
  if (!isRecord(raw)) return null;
  const key = str(raw.key);
  if (!key) return null;
  return {
    key,
    title: str(raw.title, key),
    kind: raw.kind === 'manual' ? 'manual' : 'import',
    enabled: raw.enabled === true,
    lastSync: isoOrNull(raw.lastSync),
    notes: str(raw.notes),
  };
}

function normalizeImport(raw: unknown): WeightImportResult {
  if (!isRecord(raw)) throw new IntegrationsError('Ответ импорта не является объектом');
  return {
    imported: count(raw.imported),
    skipped: count(raw.skipped),
    firstDate: isoOrNull(raw.firstDate),
    lastDate: isoOrNull(raw.lastDate),
  };
}

/* ── Эндпоинты ───────────────────────────────────────────── */

/** GET /api/integrations — источники веса в порядке сервера. */
export async function fetchWeightSources(): Promise<WeightSource[]> {
  const raw = await request<unknown>('/integrations');
  if (!Array.isArray(raw)) throw new IntegrationsError('Сервер не отдал список источников');
  return raw
    .map(normalizeSource)
    .filter((source): source is WeightSource => source !== null);
}

/** PATCH /api/integrations/{key} — включает или выключает приём данных. */
export async function setWeightSourceEnabled(
  key: string,
  enabled: boolean,
): Promise<WeightSource> {
  const raw = await request<unknown>(`/integrations/${encodeURIComponent(key)}`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  });
  const source = normalizeSource(raw);
  if (!source) throw new IntegrationsError('Сервер вернул неизвестный источник');
  return source;
}

/** POST /api/health/weight/import/csv — файл уходит текстом, разбирает сервер. */
export async function importWeightCsv(
  source: string,
  file: File,
): Promise<WeightImportResult> {
  const text = await file.text();
  if (!text.trim()) throw new IntegrationsError('Файл пустой: нужны строки date,weight', 422);
  const raw = await request<unknown>(
    `/health/weight/import/csv?source=${encodeURIComponent(source)}`,
    { method: 'POST', body: text },
    'text/csv; charset=utf-8',
  );
  return normalizeImport(raw);
}

/** Ручная запись: один замер уходит импортом с `source = 'manual'`. */
export async function addWeightPoint(
  date: string,
  weight: number,
): Promise<WeightImportResult> {
  const raw = await request<unknown>('/health/weight/import', {
    method: 'POST',
    body: JSON.stringify({ source: 'manual', points: [{ date, weight }] }),
  });
  return normalizeImport(raw);
}

/* ── Форматтеры и текст результата ───────────────────────── */

/** «18.11, 13:33» или «ещё не было» — подпись под источником. */
export function formatLastSync(value: string | null): string {
  if (!value) return 'ещё не было';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'ещё не было';
  return `${formatDateShort(date)}, ${formatTime(date)}`;
}

/** «16.11» — дата замера в тосте и подсказках. */
export function formatDay(iso: string | null): string {
  return iso ? formatDateShort(iso) : '—';
}

export interface ImportSummary {
  text: string;
  tone: 'success' | 'default';
}

/** Тост после импорта: сколько точек добавлено и за какой период. */
export function importSummary(result: WeightImportResult): ImportSummary {
  if (result.imported > 0) {
    const points = `${result.imported} ${pluralRu(result.imported, ['точка', 'точки', 'точек'])}`;
    const span =
      result.firstDate && result.lastDate && result.firstDate !== result.lastDate
        ? `${formatDay(result.firstDate)} — ${formatDay(result.lastDate)}`
        : formatDay(result.lastDate ?? result.firstDate);
    return { text: `Импортировано ${points}: ${span}`, tone: 'success' };
  }
  if (result.skipped > 0) {
    const days = `${result.skipped} ${pluralRu(result.skipped, ['день', 'дня', 'дней'])}`;
    return { text: `0 новых точек: ${days} уже в базе`, tone: 'default' };
  }
  return { text: 'В файле не оказалось ни одной точки веса', tone: 'default' };
}

/** Тост после ручного ввода. */
export function manualSummary(date: string, weight: number, result: WeightImportResult): ImportSummary {
  const day = formatDay(date);
  const value = `${new Intl.NumberFormat('ru-RU', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  }).format(weight)} кг`;
  if (result.imported > 0) return { text: `Вес за ${day} записан: ${value}`, tone: 'success' };
  return { text: `За ${day} вес уже был — запись не добавлена`, tone: 'default' };
}

/* ── CSV-шаблон ──────────────────────────────────────────── */

export const CSV_TEMPLATE_FILENAME = 'life-os-weight-template.csv';

/** Шаблон: заголовок `date,weight` и две строки примера (даты — два прошлых дня). */
export function csvTemplate(now: Date = new Date()): string {
  const rows = [
    'date,weight',
    `${toLocalIso(addDays(now, -2))},${79.8}`,
    `${toLocalIso(addDays(now, -1))},${79.6}`,
    '',
  ];
  return rows.join('\n');
}

/** Отдаёт шаблон браузеру на скачивание и возвращает имя файла. */
export function downloadCsvTemplate(): string {
  const blob = new Blob([csvTemplate()], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = CSV_TEMPLATE_FILENAME;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  // Освобождаем blob чуть позже: Safari успевает начать скачивание.
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  return CSV_TEMPLATE_FILENAME;
}

/* ── Проверка ручного замера ─────────────────────────────── */

export interface WeightDraft {
  date: string;
  weight: string;
}

export interface WeightDraftCheck {
  ok: boolean;
  /** Нормализованный вес, когда проверка прошла. */
  weight: number | null;
  /** Что именно не так — по-русски, для инлайн-ошибки. */
  error: string | null;
}

/** Проверка полей «дата + вес»: те же границы, что у сервера (30…350 кг). */
export function checkWeightDraft(draft: WeightDraft, today: string = toLocalIso()): WeightDraftCheck {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.date)) {
    return { ok: false, weight: null, error: 'Выберите дату замера' };
  }
  if (draft.date > today) {
    return { ok: false, weight: null, error: 'Дата не может быть в будущем' };
  }
  const text = draft.weight.trim().replace(',', '.');
  if (!text) return { ok: false, weight: null, error: 'Введите вес в килограммах' };
  const weight = Number(text);
  if (!Number.isFinite(weight)) return { ok: false, weight: null, error: 'Вес — это число' };
  if (weight < 30 || weight > 350) {
    return { ok: false, weight: null, error: 'Вес должен быть от 30 до 350 кг' };
  }
  return { ok: true, weight: Math.round(weight * 100) / 100, error: null };
}
