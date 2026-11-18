/**
 * Клиент Питания (v4): дневник еды, справочник продуктов, нормы и вода.
 *
 * Источники: ``/api/nutrition/*`` (схема v4, t15), вода — ``POST /api/water``,
 * ряд воды для графика — ``/api/sections/nutrition`` (метрика ``water``).
 * Все функции возвращают нормализованные данные: страница не доверяет форме
 * ответа и не падает на пропущенном поле.
 */

import { apiRequest } from './client';

export type Meal = 'breakfast' | 'lunch' | 'dinner' | 'snack';

/** Порядок приёмов пищи в дневнике. */
export const MEALS: readonly Meal[] = ['breakfast', 'lunch', 'dinner', 'snack'];

export const MEAL_LABELS: Record<Meal, string> = {
  breakfast: 'Завтрак',
  lunch: 'Обед',
  dinner: 'Ужин',
  snack: 'Перекус',
};

export type Macro = 'kcal' | 'protein' | 'fat' | 'carbs' | 'fiber' | 'sugar';

/** Все макросы в порядке показа. */
export const MACROS: readonly Macro[] = ['kcal', 'protein', 'fat', 'carbs', 'fiber', 'sugar'];

/** Макросы без калорий: именно они идут строкой «Б · Ж · У · Кл · Сах». */
export const MACROS_SHORT: readonly Macro[] = ['protein', 'fat', 'carbs', 'fiber', 'sugar'];

export type Macros = Record<Macro, number>;

export const MACRO_LABELS: Record<Macro, string> = {
  kcal: 'Калории',
  protein: 'Белки',
  fat: 'Жиры',
  carbs: 'Углеводы',
  fiber: 'Клетчатка',
  sugar: 'Сахара',
};

export const MACRO_SHORT: Record<Macro, string> = {
  kcal: 'ккал',
  protein: 'Б',
  fat: 'Ж',
  carbs: 'У',
  fiber: 'Кл',
  sugar: 'Сах',
};

export interface FoodEntry {
  id: number;
  date: string;
  ts: string;
  meal: Meal;
  productId: number | null;
  text: string;
  grams: number;
  kcal: number;
  protein: number;
  fat: number;
  carbs: number;
  fiber: number;
  sugar: number;
}

export interface MealBlock {
  meal: Meal;
  label: string;
  entries: FoodEntry[];
  totals: Macros;
}

export interface NutritionTargets {
  kcal: number;
  protein: number;
  fat: number;
  carbs: number;
  fiber: number;
  sugar: number;
  waterMl: number;
  updated: string;
}

export interface WaterDay {
  ml: number;
  target: number;
}

export interface NutritionDay {
  date: string;
  meals: MealBlock[];
  totals: Macros;
  targets: NutritionTargets;
  remaining: Macros;
  water: WaterDay;
}

export interface NutritionProduct {
  id: number;
  name: string;
  brand: string;
  /** КБЖУ на ``baseGrams`` граммов (в справочнике — на 100 г). */
  kcal: number;
  protein: number;
  fat: number;
  carbs: number;
  fiber: number;
  sugar: number;
  baseGrams: number;
  archived: boolean;
}

export interface DailyNutrition extends Macros {
  date: string;
}

export interface NutritionSummary {
  days: number;
  startDate: string;
  endDate: string;
  daysWithEntries: number;
  daily: DailyNutrition[];
  average: Macros;
  targets: NutritionTargets;
  coverage: Record<Macro, number>;
}

export interface WaterPoint {
  date: string;
  value: number;
}

export interface WaterSeries {
  points: WaterPoint[];
  /** Последний день с водой. */
  last: number;
  /** Среднее за последние 7 дней с данными. */
  avg: number;
  unit: string;
}

export type NewEntryInput =
  | { date: string; meal: Meal; productId: number; grams: number }
  | {
      date: string;
      meal: Meal;
      text: string;
      grams: number;
      kcal: number;
      protein: number;
      fat: number;
      carbs: number;
      fiber?: number;
      sugar?: number;
    };

/* ── Нормализация ответов ─────────────────────────────────── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

/** Пустые макросы — основа для сумм и заглушек. */
export function zeroMacros(): Macros {
  return { kcal: 0, protein: 0, fat: 0, carbs: 0, fiber: 0, sugar: 0 };
}

function normalizeMacros(raw: unknown): Macros {
  const source = isRecord(raw) ? raw : {};
  const totals = zeroMacros();
  for (const macro of MACROS) totals[macro] = num(source[macro], 0);
  return totals;
}

function normalizeMeal(raw: unknown): Meal {
  return MEALS.includes(raw as Meal) ? (raw as Meal) : 'snack';
}

function normalizeEntry(raw: unknown): FoodEntry | null {
  if (!isRecord(raw)) return null;
  const id = num(raw.id, 0);
  if (id <= 0) return null;
  const productId = raw.productId === null || raw.productId === undefined ? 0 : num(raw.productId, 0);
  return {
    id,
    date: str(raw.date, ''),
    ts: str(raw.ts, ''),
    meal: normalizeMeal(raw.meal),
    productId: productId && productId > 0 ? productId : null,
    text: str(raw.text, 'Запись'),
    grams: num(raw.grams, 0),
    kcal: num(raw.kcal, 0),
    protein: num(raw.protein, 0),
    fat: num(raw.fat, 0),
    carbs: num(raw.carbs, 0),
    fiber: num(raw.fiber, 0),
    sugar: num(raw.sugar, 0),
  };
}

function normalizeProduct(raw: unknown): NutritionProduct | null {
  if (!isRecord(raw)) return null;
  const id = num(raw.id, 0);
  const name = str(raw.name, '');
  if (id <= 0 || !name) return null;
  const baseGrams = num(raw.baseGrams, 100);
  return {
    id,
    name,
    brand: str(raw.brand, ''),
    kcal: num(raw.kcal, 0),
    protein: num(raw.protein, 0),
    fat: num(raw.fat, 0),
    carbs: num(raw.carbs, 0),
    fiber: num(raw.fiber, 0),
    sugar: num(raw.sugar, 0),
    baseGrams: baseGrams > 0 ? baseGrams : 100,
    archived: raw.archived === true,
  };
}

function normalizeTargets(raw: unknown): NutritionTargets {
  const source = isRecord(raw) ? raw : {};
  return {
    kcal: num(source.kcal, 0),
    protein: num(source.protein, 0),
    fat: num(source.fat, 0),
    carbs: num(source.carbs, 0),
    fiber: num(source.fiber, 0),
    sugar: num(source.sugar, 0),
    waterMl: num(source.waterMl, 0),
    updated: str(source.updated, ''),
  };
}

function normalizeDay(raw: unknown): NutritionDay {
  if (!isRecord(raw)) throw new Error('Питание: ожидался дневник за день');
  const blocks = asArray(raw.meals).flatMap((item): MealBlock[] => {
    if (!isRecord(item)) return [];
    const meal = normalizeMeal(item.meal);
    return [
      {
        meal,
        label: str(item.label, MEAL_LABELS[meal]),
        entries: asArray(item.entries).flatMap((entry) => {
          const parsed = normalizeEntry(entry);
          return parsed ? [parsed] : [];
        }),
        totals: normalizeMacros(item.totals),
      },
    ];
  });

  // Четыре блока в фиксированном порядке — даже если сервер прислал не все.
  const meals = MEALS.map(
    (meal): MealBlock =>
      blocks.find((block) => block.meal === meal) ?? {
        meal,
        label: MEAL_LABELS[meal],
        entries: [],
        totals: zeroMacros(),
      },
  );

  const water = isRecord(raw.water) ? raw.water : {};
  const totals = normalizeMacros(raw.totals);
  const targets = normalizeTargets(raw.targets);
  const remaining = isRecord(raw.remaining)
    ? normalizeMacros(raw.remaining)
    : subtractMacros(targets, totals);

  return {
    date: str(raw.date, ''),
    meals,
    totals,
    targets,
    remaining,
    water: { ml: num(water.ml, 0), target: num(water.target, targets.waterMl) },
  };
}

function normalizeSummary(raw: unknown): NutritionSummary {
  if (!isRecord(raw)) throw new Error('Питание: ожидалась сводка за период');
  const daily = asArray(raw.daily).flatMap((item): DailyNutrition[] => {
    if (!isRecord(item)) return [];
    const date = str(item.date, '');
    if (!date) return [];
    return [{ date, ...normalizeMacros(item) }];
  });
  const coverage = {} as Record<Macro, number>;
  const coverageRaw = isRecord(raw.coverage) ? raw.coverage : {};
  for (const macro of MACROS) coverage[macro] = num(coverageRaw[macro], 0);
  return {
    days: num(raw.days, daily.length),
    startDate: str(raw.startDate, ''),
    endDate: str(raw.endDate, ''),
    daysWithEntries: num(raw.daysWithEntries, daily.length),
    daily,
    average: normalizeMacros(raw.average),
    targets: normalizeTargets(raw.targets),
    coverage,
  };
}

/* ── Математика на клиенте ────────────────────────────────── */

export function sumMacros(list: readonly Macros[]): Macros {
  const totals = zeroMacros();
  for (const item of list) {
    for (const macro of MACROS) totals[macro] += item[macro];
  }
  for (const macro of MACROS) totals[macro] = round1(totals[macro]);
  return totals;
}

export function subtractMacros(left: Macros, right: Macros): Macros {
  const result = zeroMacros();
  for (const macro of MACROS) result[macro] = round1(left[macro] - right[macro]);
  return result;
}

/** Пересчёт макросов при смене граммовки: пропорционально весу. */
export function scaleMacros(macros: Macros, fromGrams: number, toGrams: number): Macros {
  if (fromGrams <= 0) return { ...macros };
  const ratio = toGrams / fromGrams;
  const result = zeroMacros();
  for (const macro of MACROS) result[macro] = round1(macros[macro] * ratio);
  return result;
}

/** КБЖУ продукта на произвольную порцию (справочник хранит норму на baseGrams). */
export function macrosForGrams(product: NutritionProduct, grams: number): Macros {
  const ratio = grams > 0 ? grams / product.baseGrams : 0;
  return {
    kcal: round1(product.kcal * ratio),
    protein: round1(product.protein * ratio),
    fat: round1(product.fat * ratio),
    carbs: round1(product.carbs * ratio),
    fiber: round1(product.fiber * ratio),
    sugar: round1(product.sugar * ratio),
  };
}

/* ── Дневник ──────────────────────────────────────────────── */

export async function fetchNutritionDay(date: string): Promise<NutritionDay> {
  const raw = await apiRequest<unknown>(`/nutrition/day?date=${encodeURIComponent(date)}`);
  return normalizeDay(raw);
}

export async function createNutritionEntry(input: NewEntryInput): Promise<FoodEntry> {
  const raw = await apiRequest<unknown>('/nutrition/entries', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  const entry = normalizeEntry(raw);
  if (!entry) throw new Error('Питание: сервер не вернул запись дневника');
  return entry;
}

export async function patchNutritionEntry(
  entryId: number,
  patch: { grams?: number; meal?: Meal },
): Promise<FoodEntry> {
  const raw = await apiRequest<unknown>(`/nutrition/entries/${entryId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  const entry = normalizeEntry(raw);
  if (!entry) throw new Error('Питание: сервер не вернул обновлённую запись');
  return entry;
}

export async function deleteNutritionEntry(entryId: number): Promise<void> {
  await apiRequest(`/nutrition/entries/${entryId}`, { method: 'DELETE' });
}

/* ── Справочник ───────────────────────────────────────────── */

export async function searchNutritionProducts(
  query: string,
  limit = 8,
): Promise<NutritionProduct[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  const text = query.trim();
  if (text) params.set('q', text);
  const raw = await apiRequest<unknown>(`/nutrition/products?${params.toString()}`);
  if (!isRecord(raw)) throw new Error('Питание: ожидался справочник продуктов');
  return asArray(raw.items).flatMap((item) => {
    const product = normalizeProduct(item);
    return product ? [product] : [];
  });
}

/* ── Нормы, сводка, вода ──────────────────────────────────── */

export async function fetchNutritionTargets(): Promise<NutritionTargets> {
  return normalizeTargets(await apiRequest<unknown>('/nutrition/targets'));
}

export type TargetsPatch = Partial<Omit<NutritionTargets, 'updated'>>;

export async function putNutritionTargets(patch: TargetsPatch): Promise<NutritionTargets> {
  const raw = await apiRequest<unknown>('/nutrition/targets', {
    method: 'PUT',
    body: JSON.stringify(patch),
  });
  return normalizeTargets(raw);
}

export async function fetchNutritionSummary(days: number): Promise<NutritionSummary> {
  const raw = await apiRequest<unknown>(`/nutrition/summary?days=${days}`);
  return normalizeSummary(raw);
}

/** Ряд воды за окно: ``null``, если метрики воды нет вовсе. */
export async function fetchWaterSeries(days: number): Promise<WaterSeries | null> {
  const raw = await apiRequest<unknown>(`/sections/nutrition?days=${days}`);
  if (!isRecord(raw)) return null;
  const series = asArray(raw.series).find(
    (item) => isRecord(item) && str(item.key, '') === 'water',
  );
  if (!isRecord(series)) return null;
  const points = asArray(series.points).flatMap((item): WaterPoint[] => {
    if (!isRecord(item)) return [];
    const date = str(item.date, '');
    if (!date) return [];
    return [{ date, value: num(item.value, 0) }];
  });
  if (points.length === 0) return null;
  return {
    points,
    last: num(series.last, points[points.length - 1]?.value ?? 0),
    avg: num(series.avg, 0),
    unit: str(series.unit, 'мл'),
  };
}

/** Добавить порцию воды за дату (``/api/water``); день перечитывает страница. */
export async function addWater(ml: number, date: string): Promise<void> {
  await apiRequest('/water', {
    method: 'POST',
    body: JSON.stringify({ ml, date }),
  });
}
