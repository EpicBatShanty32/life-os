import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent } from 'react';
import {
  Button,
  EmptyState,
  Field,
  Input,
  ProgressBar,
  Segmented,
  Sheet,
  Skeleton,
} from '../components/ui';
import { Icon } from '../components/Icon';
import { apiErrorMessage } from '../api/errors';
import {
  formatDateShort,
  formatDateWithYear,
  formatNumber,
  parseIsoDate,
  toLocalIso,
  weekdayShort,
} from '../lib/format';
import { toast } from '../lib/toast';
import {
  MACRO_LABELS,
  MACRO_SHORT,
  MEALS,
  addWater,
  createNutritionEntry,
  deleteNutritionEntry,
  fetchNutritionDay,
  fetchNutritionSummary,
  fetchWaterSeries,
  macrosForGrams,
  patchNutritionEntry,
  putNutritionTargets,
  scaleMacros,
  searchNutritionProducts,
  subtractMacros,
  sumMacros,
} from '../api/nutrition';
import type {
  FoodEntry,
  Macro,
  Macros,
  Meal,
  MealBlock,
  NewEntryInput,
  NutritionDay,
  NutritionProduct,
  NutritionSummary,
  NutritionTargets,
  TargetsPatch,
  WaterDay,
  WaterPoint,
  WaterSeries,
} from '../api/nutrition';
import '../styles/pages-life.css';
import '../styles/nutrition.css';

/* ── Константы и форматтеры ──────────────────────────────── */

const WINDOWS = [30, 90, 120] as const;
type WindowDays = (typeof WINDOWS)[number];

const WINDOW_OPTIONS = WINDOWS.map((value) => ({
  value: String(value),
  label: `${value} дн.`,
}));

/** Порции воды для быстрого добавления, мл. */
const WATER_PORTS = [200, 250, 500] as const;

/** Цвет макроса — категориальный маркер, а не украшение. */
const MACRO_TONE: Record<Macro, 'accent' | 'cat-1' | 'cat-2' | 'cat-3' | 'cat-4' | 'cat-5'> = {
  kcal: 'accent',
  protein: 'cat-1',
  fat: 'cat-3',
  carbs: 'cat-4',
  fiber: 'cat-2',
  sugar: 'cat-5',
};

const STRIP_MACROS: readonly Macro[] = ['protein', 'fat', 'carbs', 'fiber', 'sugar'];

const MEAL_ORDER = MEALS;

function numberText(value: number): string {
  return formatNumber(value, Number.isInteger(value) ? 0 : 1);
}

function kcalText(value: number): string {
  return formatNumber(Math.round(value));
}

function gramsText(value: number): string {
  return `${formatNumber(Math.round(value))} г`;
}

function mlText(value: number): string {
  return `${formatNumber(Math.round(value))} мл`;
}

/** Строка записи: только Б·Ж·У — клетчатка и сахара живут в итогах дня. */
function entryMacroLine(entry: Macros): string {
  return `${MACRO_SHORT.protein} ${numberText(entry.protein)} · ${MACRO_SHORT.fat} ${numberText(
    entry.fat,
  )} · ${MACRO_SHORT.carbs} ${numberText(entry.carbs)}`;
}

function macroLine(totals: Macros): string {
  return `${MACRO_SHORT.protein} ${numberText(totals.protein)} · ${MACRO_SHORT.fat} ${numberText(
    totals.fat,
  )} · ${MACRO_SHORT.carbs} ${numberText(totals.carbs)} · ${MACRO_SHORT.fiber} ${numberText(
    totals.fiber,
  )} · ${MACRO_SHORT.sugar} ${numberText(totals.sugar)}`;
}

/** «1 250,5» → 1250.5; пустое → null. Запятая и пробелы допустимы. */
function parseAmount(value: string): number | null {
  const normalized = value.replace(',', '.').replace(/\s/g, '').trim();
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function shiftIso(iso: string, delta: number): string {
  const date = parseIsoDate(iso);
  date.setDate(date.getDate() + delta);
  return toLocalIso(date);
}

const MONTHS_GENITIVE = [
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

/** «ср, 18 ноября» (с годом, если он не текущий). */
function dayTitle(iso: string, today: string): string {
  const date = parseIsoDate(iso);
  const tail = `${date.getDate()} ${MONTHS_GENITIVE[date.getMonth()]}`;
  const year = date.getFullYear() === parseIsoDate(today).getFullYear() ? '' : ` ${date.getFullYear()}`;
  return `${weekdayShort(date)}, ${tail}${year}`;
}

/* ── Пересчёт дня после локальной правки ─────────────────── */

function compareEntries(a: FoodEntry, b: FoodEntry): number {
  return a.ts.localeCompare(b.ts) || a.id - b.id;
}

function recalcDay(day: NutritionDay): NutritionDay {
  const meals = day.meals.map((block) => ({ ...block, totals: sumMacros(block.entries) }));
  const totals = sumMacros(meals.map((block) => block.totals));
  return {
    ...day,
    meals,
    totals,
    remaining: subtractMacros(day.targets, totals),
    water: { ...day.water, target: day.targets.waterMl || day.water.target },
  };
}

/** Вставить или обновить запись, сохранив порядок приёмов пищи. */
function upsertEntry(day: NutritionDay, entry: FoodEntry): NutritionDay {
  const meals = day.meals.map((block) => {
    const rest = block.entries.filter((item) => item.id !== entry.id);
    if (block.meal !== entry.meal) return { ...block, entries: rest };
    return { ...block, entries: [...rest, entry].sort(compareEntries) };
  });
  return recalcDay({ ...day, meals });
}

function removeEntry(day: NutritionDay, entryId: number): NutritionDay {
  return recalcDay({
    ...day,
    meals: day.meals.map((block) => ({
      ...block,
      entries: block.entries.filter((item) => item.id !== entryId),
    })),
  });
}

/* ── Полоса КБЖУ за день ─────────────────────────────────── */

interface StripProps {
  day: NutritionDay | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

function NutritionStrip({ day, loading, error, onRetry }: StripProps) {
  if (!day) {
    if (error) {
      return (
        <p className="nu-note" role="status">
          <Icon name="sync" size={15} />
          <span>Дневник не загрузился — {error}</span>
          <button className="nu-link" type="button" onClick={onRetry}>
            Повторить
          </button>
        </p>
      );
    }
    return <Skeleton variant="block" label="Загружаю КБЖУ за день" className="nu-strip-skeleton" />;
  }

  const kcalOver = day.targets.kcal > 0 && day.totals.kcal > day.targets.kcal;
  const remainder = day.remaining.kcal;

  return (
    <section className="nu-strip" aria-label="КБЖУ за день">
      <div className="nu-strip__cell nu-strip__cell--kcal">
        <span className="nu-strip__label">Съедено</span>
        <p className="nu-strip__value">
          {kcalText(day.totals.kcal)}
          <span className="nu-strip__unit">ккал</span>
        </p>
        <p className="nu-strip__meta">
          {day.targets.kcal > 0 ? `норма ${kcalText(day.targets.kcal)}` : 'норма не задана'}
          {day.targets.kcal > 0
            ? remainder >= 0
              ? ` · осталось ${kcalText(remainder)}`
              : ` · перебор ${kcalText(-remainder)}`
            : ''}
        </p>
        <ProgressBar
          value={day.totals.kcal}
          max={day.targets.kcal || 1}
          tone={kcalOver ? 'warning' : 'accent'}
          ariaLabel={`Калории: ${kcalText(day.totals.kcal)} из ${kcalText(day.targets.kcal)}`}
        />
      </div>

      {STRIP_MACROS.map((macro) => {
        const value = day.totals[macro];
        const target = day.targets[macro];
        const over = target > 0 && value > target;
        return (
          <div className="nu-strip__cell" key={macro}>
            <span className="nu-strip__label">{MACRO_LABELS[macro]}</span>
            <p className="nu-strip__value nu-strip__value--sm">
              {numberText(value)}
              <span className="nu-strip__unit">г</span>
            </p>
            <p className="nu-strip__meta">
              {target > 0 ? `норма ${numberText(target)} г` : 'без нормы'}
              {over ? ` · выше на ${numberText(value - target)} г` : ''}
            </p>
            <ProgressBar
              value={value}
              max={target || 1}
              tone={over ? 'warning' : MACRO_TONE[macro]}
              ariaLabel={`${MACRO_LABELS[macro]}: ${numberText(value)} из ${numberText(target)} г`}
            />
          </div>
        );
      })}

      {loading ? <span className="nu-strip__status">Обновляю</span> : null}
    </section>
  );
}

/* ── Строка продукта с инлайн-правкой граммов ────────────── */

interface EntryRowProps {
  entry: FoodEntry;
  onSaveGrams: (entry: FoodEntry, grams: number) => Promise<void>;
  onDelete: (entry: FoodEntry) => void;
}

function EntryRow({ entry, onSaveGrams, onDelete }: EntryRowProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [returnFocus, setReturnFocus] = useState(false);
  const inputId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (editing || !returnFocus) return;
    triggerRef.current?.focus();
    setReturnFocus(false);
  }, [editing, returnFocus]);

  function start() {
    setDraft(String(entry.grams).replace('.', ','));
    setError(null);
    setEditing(true);
  }

  function cancel() {
    setEditing(false);
    setError(null);
    setReturnFocus(true);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    const grams = parseAmount(draft);
    if (grams === null || grams <= 0 || grams > 5000) {
      setError('Укажите вес от 1 до 5 000 г');
      return;
    }
    if (Math.abs(grams - entry.grams) < 0.05) {
      cancel();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSaveGrams(entry, grams);
      setEditing(false);
      setReturnFocus(true);
    } catch (failure) {
      setError(apiErrorMessage(failure, 'Не удалось изменить граммы'));
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <li className="nu-entry nu-entry--editing">
        <form
          className="nu-entry__form"
          onSubmit={submit}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.stopPropagation();
              cancel();
            }
          }}
        >
          <label className="nu-entry__form-label" htmlFor={inputId}>
            {entry.text}
          </label>
          <span className="nu-entry__form-field">
            <Input
              id={inputId}
              size="sm"
              inputMode="decimal"
              autoFocus
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onFocus={(event) => event.currentTarget.select()}
              aria-label={`Граммы: ${entry.text}`}
              className="nu-entry__input"
            />
            <span className="nu-entry__form-unit" aria-hidden="true">
              г
            </span>
          </span>
          <Button
            type="submit"
            size="sm"
            variant="secondary"
            loading={saving}
            aria-label="Сохранить граммы"
            title="Сохранить"
          >
            <Icon name="check" size={15} />
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={cancel}
            aria-label="Отменить правку"
            title="Отменить"
          >
            <Icon name="close" size={15} />
          </Button>
          {error ? (
            <p className="nu-entry__error" role="alert">
              {error}
            </p>
          ) : null}
        </form>
      </li>
    );
  }

  return (
    <li className="nu-entry">
      <div className="nu-entry__main">
        <p className="nu-entry__title">
          <span className="nu-entry__name" title={entry.text}>
            {entry.text}
          </span>
          <span className="nu-entry__kcal">
            {kcalText(entry.kcal)}
            <span className="nu-entry__kcal-unit">ккал</span>
          </span>
        </p>
        <p className="nu-entry__meta">
          <span className="nu-entry__grams">{gramsText(entry.grams)}</span>
          <span className="nu-entry__dot" aria-hidden="true">
            ·
          </span>
          <span className="nu-entry__macros">{entryMacroLine(entry)}</span>
        </p>
      </div>
      <div className="nu-entry__actions">
        <button
          ref={triggerRef}
          type="button"
          className="nu-icon-btn"
          onClick={start}
          aria-label={`Изменить граммы: ${entry.text}`}
          title="Изменить граммы"
        >
          <Icon name="edit" size={15} />
        </button>
        <button
          type="button"
          className="nu-icon-btn nu-icon-btn--danger"
          onClick={() => onDelete(entry)}
          aria-label={`Удалить: ${entry.text}`}
          title="Удалить"
        >
          <Icon name="trash" size={15} />
        </button>
      </div>
    </li>
  );
}

/* ── Форма добавления: справочник или своя запись ─────────── */

type AddMode = 'catalog' | 'manual';

const ADD_MODES = [
  { value: 'catalog' as AddMode, label: 'Из справочника' },
  { value: 'manual' as AddMode, label: 'Своя запись' },
];

interface AddFormProps {
  date: string;
  meal: Meal;
  onClose: () => void;
  onSubmit: (input: NewEntryInput) => Promise<void>;
}

function AddForm({ date, meal, onClose, onSubmit }: AddFormProps) {
  const [mode, setMode] = useState<AddMode>('catalog');
  const [query, setQuery] = useState('');
  const [products, setProducts] = useState<NutritionProduct[]>([]);
  const [productsLoading, setProductsLoading] = useState(false);
  const [productsError, setProductsError] = useState<string | null>(null);
  const [selected, setSelected] = useState<NutritionProduct | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [grams, setGrams] = useState('100');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState('');

  const [name, setName] = useState('');
  const [manualKcal, setManualKcal] = useState('');
  const [manualProtein, setManualProtein] = useState('');
  const [manualFat, setManualFat] = useState('');
  const [manualCarbs, setManualCarbs] = useState('');

  const sequence = useRef(0);
  const listId = useId();

  const loadProducts = useCallback(async (text: string) => {
    const ticket = ++sequence.current;
    setProductsLoading(true);
    try {
      const items = await searchNutritionProducts(text, 8);
      if (sequence.current !== ticket) return;
      setProducts(items);
      setActiveIndex(0);
      setProductsError(null);
    } catch (failure) {
      if (sequence.current !== ticket) return;
      setProducts([]);
      setProductsError(apiErrorMessage(failure, 'Справочник недоступен'));
    } finally {
      if (sequence.current === ticket) setProductsLoading(false);
    }
  }, []);

  // Поиск по справочнику: debounce 250 мс, гонки гасим номером запроса.
  useEffect(() => {
    if (mode !== 'catalog' || selected) return undefined;
    const handle = window.setTimeout(() => {
      void loadProducts(query);
    }, 250);
    return () => window.clearTimeout(handle);
  }, [query, mode, selected, loadProducts]);

  const gramsValue = parseAmount(grams);
  const chosenMacros = selected ? macrosForGrams(selected, gramsValue ?? selected.baseGrams) : null;

  function choose(product: NutritionProduct) {
    setSelected(product);
    setQuery(product.name);
    setActiveIndex(0);
    setError(null);
  }

  function resetChoice() {
    setSelected(null);
    setQuery('');
    setError(null);
  }

  function handleSearchKeys(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (products.length === 0) return;
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      setActiveIndex((current) => (current + direction + products.length) % products.length);
      return;
    }
    if (event.key === 'Enter') {
      const product = products[activeIndex];
      if (!product) return;
      event.preventDefault();
      choose(product);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const weight = parseAmount(grams);
    if (weight === null || weight <= 0 || weight > 5000) {
      setError('Укажите вес порции от 1 до 5 000 г');
      return;
    }

    setBusy(true);
    setError(null);
    try {
      if (mode === 'catalog') {
        if (!selected) {
          setError('Выберите продукт из справочника');
          return;
        }
        await onSubmit({ date, meal, productId: selected.id, grams: weight });
        setStatus(`Добавлено: ${selected.name}, ${gramsText(weight)}`);
        resetChoice();
        setGrams('100');
      } else {
        const title = name.trim();
        if (!title) {
          setError('Введите название записи');
          return;
        }
        const kcal = parseAmount(manualKcal);
        if (kcal === null || kcal < 0) {
          setError('Укажите калории порции');
          return;
        }
        await onSubmit({
          date,
          meal,
          text: title,
          grams: weight,
          kcal,
          protein: parseAmount(manualProtein) ?? 0,
          fat: parseAmount(manualFat) ?? 0,
          carbs: parseAmount(manualCarbs) ?? 0,
        });
        setStatus(`Добавлено: ${title}`);
        setName('');
        setManualKcal('');
        setManualProtein('');
        setManualFat('');
        setManualCarbs('');
        setGrams('100');
      }
    } catch (failure) {
      setError(apiErrorMessage(failure, 'Не удалось добавить запись'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="nu-add"
      onSubmit={submit}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <Segmented
        options={ADD_MODES}
        value={mode}
        onChange={(next) => {
          setMode(next);
          setError(null);
        }}
        ariaLabel="Способ добавления записи"
      />

      {mode === 'catalog' ? (
        selected ? (
          <div className="nu-add__chosen">
            <Icon name="check" size={15} />
            <span className="nu-add__chosen-name">{selected.name}</span>
            <span className="nu-add__chosen-kcal">
              {`${kcalText(chosenMacros?.kcal ?? 0)} ккал на ${gramsText(gramsValue ?? 0)}`}
            </span>
            <button className="nu-link" type="button" onClick={resetChoice}>
              Другой продукт
            </button>
          </div>
        ) : (
          <div className="nu-add__search">
            <Field label="Поиск по справочнику" htmlFor={`${listId}-search`}>
              <Input
                id={`${listId}-search`}
                role="combobox"
                aria-expanded={products.length > 0}
                aria-controls={listId}
                aria-autocomplete="list"
                aria-activedescendant={
                  products[activeIndex] ? `${listId}-${products[activeIndex].id}` : undefined
                }
                autoFocus
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={handleSearchKeys}
                placeholder="Например, гречка"
                autoComplete="off"
              />
            </Field>

            {productsError ? (
              <p className="nu-note" role="status">
                <span>{productsError}</span>
                <button
                  className="nu-link"
                  type="button"
                  onClick={() => void loadProducts(query)}
                >
                  Повторить
                </button>
              </p>
            ) : products.length > 0 ? (
              <ul className="nu-add__list" role="listbox" id={listId} aria-label="Продукты">
                {products.map((product, index) => (
                  <li
                    key={product.id}
                    id={`${listId}-${product.id}`}
                    role="option"
                    aria-selected={index === activeIndex}
                    className="nu-add__option"
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => choose(product)}
                  >
                    <span className="nu-add__option-name">
                      {product.name}
                      {product.brand ? (
                        <span className="nu-add__option-brand"> · {product.brand}</span>
                      ) : null}
                    </span>
                    <span className="nu-add__option-kcal">
                      {`${kcalText(product.kcal)} ккал / ${gramsText(product.baseGrams)}`}
                    </span>
                  </li>
                ))}
              </ul>
            ) : productsLoading ? (
              <p className="nu-add__hint" role="status">
                Ищу продукт…
              </p>
            ) : (
              <p className="nu-add__hint">
                Ничего не нашлось — добавьте запись вручную или измените запрос.
              </p>
            )}
          </div>
        )
      ) : (
        <div className="nu-add__manual">
          <Field label="Название" htmlFor={`${listId}-name`}>
            <Input
              id={`${listId}-name`}
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Например, домашний суп"
            />
          </Field>
          <div className="nu-add__manual-grid">
            <Field label="Ккал" htmlFor={`${listId}-kcal`}>
              <Input
                id={`${listId}-kcal`}
                inputMode="decimal"
                value={manualKcal}
                onChange={(event) => setManualKcal(event.target.value)}
                placeholder="0"
              />
            </Field>
            <Field label="Белки, г" htmlFor={`${listId}-protein`}>
              <Input
                id={`${listId}-protein`}
                inputMode="decimal"
                value={manualProtein}
                onChange={(event) => setManualProtein(event.target.value)}
                placeholder="0"
              />
            </Field>
            <Field label="Жиры, г" htmlFor={`${listId}-fat`}>
              <Input
                id={`${listId}-fat`}
                inputMode="decimal"
                value={manualFat}
                onChange={(event) => setManualFat(event.target.value)}
                placeholder="0"
              />
            </Field>
            <Field label="Углеводы, г" htmlFor={`${listId}-carbs`}>
              <Input
                id={`${listId}-carbs`}
                inputMode="decimal"
                value={manualCarbs}
                onChange={(event) => setManualCarbs(event.target.value)}
                placeholder="0"
              />
            </Field>
          </div>
          <p className="nu-add__hint">
            Что неизвестно — оставьте пустым, в дневник уйдёт 0. КБЖУ вводится на указанную порцию.
          </p>
        </div>
      )}

      <div className="nu-add__foot">
        <label className="nu-add__grams" htmlFor={`${listId}-grams`}>
          <span className="nu-add__grams-label">Порция</span>
          <Input
            id={`${listId}-grams`}
            key={selected ? `grams-${selected.id}` : 'grams'}
            size="sm"
            inputMode="decimal"
            autoFocus={selected !== null}
            value={grams}
            onChange={(event) => setGrams(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
            aria-label="Порция в граммах"
          />
          <span className="nu-add__grams-unit" aria-hidden="true">
            г
          </span>
        </label>
        <Button type="submit" variant="primary" size="sm" loading={busy}>
          Добавить
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Закрыть
        </Button>
      </div>

      {error ? (
        <p className="nu-add__error" role="alert">
          {error}
        </p>
      ) : null}
      <p className="nu-add__status" role="status" aria-live="polite">
        {status}
      </p>
    </form>
  );
}

/* ── Блок приёма пищи ────────────────────────────────────── */

interface MealCardProps {
  block: MealBlock;
  date: string;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  onSaveGrams: (entry: FoodEntry, grams: number) => Promise<void>;
  onDelete: (entry: FoodEntry) => void;
  onSubmit: (input: NewEntryInput) => Promise<void>;
}

function MealCard({
  block,
  date,
  open,
  onOpen,
  onClose,
  onSaveGrams,
  onDelete,
  onSubmit,
}: MealCardProps) {
  const addRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(open);

  useEffect(() => {
    if (wasOpen.current && !open) addRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  const empty = block.entries.length === 0;

  return (
    <article className="nu-meal" aria-labelledby={`nu-meal-${block.meal}`}>
      <header className="nu-meal__head">
        <h2 className="nu-meal__title" id={`nu-meal-${block.meal}`}>
          {block.label}
        </h2>
        <span className="nu-meal__kcal">
          {empty ? '—' : `${kcalText(block.totals.kcal)} ккал`}
        </span>
      </header>
      <p className="nu-meal__macros">{empty ? 'Ничего не записано' : macroLine(block.totals)}</p>

      {empty ? null : (
        <ul className="nu-meal__list">
          {block.entries.map((entry) => (
            <EntryRow
              key={entry.id}
              entry={entry}
              onSaveGrams={onSaveGrams}
              onDelete={onDelete}
            />
          ))}
        </ul>
      )}

      {open ? (
        <AddForm
          key={block.meal}
          date={date}
          meal={block.meal}
          onClose={onClose}
          onSubmit={onSubmit}
        />
      ) : (
        <button ref={addRef} className="nu-meal__add" type="button" onClick={onOpen}>
          <Icon name="plus" size={15} />
          <span>добавить</span>
        </button>
      )}
    </article>
  );
}

/* ── Вода ────────────────────────────────────────────────── */

function WaterBars({ points }: { points: readonly WaterPoint[] }) {
  const last = points.slice(-14);
  const ceiling = Math.max(...last.map((point) => point.value), 1);
  const today = toLocalIso();

  return (
    <figure className="nu-water__figure" aria-label="Вода по дням">
      <ul className="nu-water__bars">
        {last.map((point) => {
          const height = Math.max(3, Math.round((point.value / ceiling) * 100));
          return (
            <li
              className="nu-water__bar"
              key={point.date}
              data-today={point.date === today ? 'true' : undefined}
            >
              <span className="nu-water__bar-track">
                <span className="nu-water__bar-fill" style={{ height: `${height}%` }} />
              </span>
              <span className="nu-water__bar-label" aria-hidden="true">
                {weekdayShort(point.date)}
              </span>
              <span className="nu-visually-hidden">
                {`${formatDateShort(point.date)}: ${mlText(point.value)}`}
              </span>
            </li>
          );
        })}
      </ul>
    </figure>
  );
}

interface WaterPanelProps {
  water: WaterDay | null;
  target: number;
  series: WaterSeries | null;
  loading: boolean;
  error: string | null;
  busy: boolean;
  onAdd: (ml: number) => void;
  onRetry: () => void;
}

function WaterPanel({ water, target, series, loading, error, busy, onAdd, onRetry }: WaterPanelProps) {
  const ml = water?.ml ?? 0;
  const goal = target > 0 ? target : water?.target ?? 0;
  const percent = goal > 0 ? Math.round((ml / goal) * 100) : 0;

  return (
    <section className="nu-card" aria-labelledby="nu-water-title">
      <header className="nu-card__head">
        <div>
          <h2 className="nu-card__title" id="nu-water-title">
            Вода
          </h2>
          <p className="nu-card__sub">
            {series
              ? `Среднее за 7 дней: ${mlText(series.avg)} · порции ниже добавляются в выбранный день`
              : 'Порции ниже добавляются в выбранный день'}
          </p>
        </div>
        <span className="nu-card__meta">{goal > 0 ? `${mlText(ml)} из ${mlText(goal)}` : mlText(ml)}</span>
      </header>

      <div className="nu-water">
        <div className="nu-water__summary">
          <p className="nu-water__value">
            {formatNumber(Math.round(ml))}
            <span className="nu-water__unit">мл</span>
          </p>
          <p className="nu-water__meta">
            {goal > 0 ? `${percent}% нормы` : 'Норма воды не задана'}
          </p>
          <ProgressBar
            value={ml}
            max={goal || 1}
            tone="cat-4"
            ariaLabel={`Вода: ${mlText(ml)} из ${mlText(goal)}`}
          />
          <div className="nu-water__quick">
            {WATER_PORTS.map((portion) => (
              <Button
                key={portion}
                size="sm"
                variant="secondary"
                disabled={busy}
                onClick={() => onAdd(portion)}
              >
                {`+${portion} мл`}
              </Button>
            ))}
          </div>
        </div>

        <div className="nu-water__chart">
          {loading && !series ? (
            <Skeleton variant="block" label="Загружаю историю воды" />
          ) : series ? (
            <WaterBars points={series.points} />
          ) : error ? (
            <p className="nu-note" role="status">
              <span>История воды не загрузилась</span>
              <button className="nu-link" type="button" onClick={onRetry}>
                Повторить
              </button>
            </p>
          ) : (
            <p className="nu-water__empty">График появится после первой записи о воде.</p>
          )}
        </div>
      </div>
    </section>
  );
}

/* ── История КБЖУ ────────────────────────────────────────── */

interface HistoryPanelProps {
  summary: NutritionSummary | null;
  summaryDays: WindowDays;
  loading: boolean;
  error: string | null;
  onDays: (days: WindowDays) => void;
  onRetry: () => void;
}

function HistoryPanel({ summary, summaryDays, loading, error, onDays, onRetry }: HistoryPanelProps) {
  const rows = summary ? [...summary.daily].reverse() : [];
  const average = summary?.average;

  return (
    <section className="nu-card" aria-labelledby="nu-history-title">
      <header className="nu-card__head">
        <div>
          <h2 className="nu-card__title" id="nu-history-title">
            История КБЖУ
          </h2>
          <p className="nu-card__sub">
            {summary
              ? `${summary.daysWithEntries} из ${summary.days} дней с записями`
              : 'Дневные суммы за выбранное окно'}
          </p>
        </div>
        <Segmented
          options={WINDOW_OPTIONS}
          value={String(summaryDays)}
          onChange={(value) => onDays(Number(value) as WindowDays)}
          ariaLabel="Окно истории"
        />
      </header>

      {loading && rows.length === 0 ? (
        <Skeleton variant="row" label="Загружаю историю" />
      ) : error ? (
        <p className="nu-note" role="status">
          <span>История не загрузилась — {error}</span>
          <button className="nu-link" type="button" onClick={onRetry}>
            Повторить
          </button>
        </p>
      ) : rows.length === 0 ? (
        <EmptyState
          title="За этот период записей нет"
          description="Добавьте первый приём пищи выше — дневные суммы появятся здесь."
        />
      ) : (
        <div className="nu-history__scroll">
          <table className="nu-table">
            <caption className="nu-visually-hidden">
              Дневные суммы КБЖУ за {summaryDays} дней
            </caption>
            <thead>
              <tr>
                <th scope="col">День</th>
                <th scope="col" className="nu-table__num">
                  Ккал
                </th>
                {STRIP_MACROS.map((macro) => (
                  <th
                    scope="col"
                    className={
                      macro === 'fiber' || macro === 'sugar'
                        ? 'nu-table__num nu-table__cell--optional'
                        : 'nu-table__num'
                    }
                    key={macro}
                  >
                    {MACRO_SHORT[macro]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.date}>
                  <th scope="row">
                    {formatDateShort(row.date)}
                    <span className="nu-table__weekday">{weekdayShort(row.date)}</span>
                  </th>
                  <td className="nu-table__num">{kcalText(row.kcal)}</td>
                  {STRIP_MACROS.map((macro) => (
                    <td
                      className={
                        macro === 'fiber' || macro === 'sugar'
                          ? 'nu-table__num nu-table__cell--optional'
                          : 'nu-table__num'
                      }
                      key={macro}
                    >
                      {numberText(row[macro])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
            {average ? (
              <tfoot>
                <tr>
                  <th scope="row">Среднее</th>
                  <td className="nu-table__num">{kcalText(average.kcal)}</td>
                  {STRIP_MACROS.map((macro) => (
                    <td
                      className={
                        macro === 'fiber' || macro === 'sugar'
                          ? 'nu-table__num nu-table__cell--optional'
                          : 'nu-table__num'
                      }
                      key={macro}
                    >
                      {numberText(average[macro])}
                    </td>
                  ))}
                </tr>
              </tfoot>
            ) : null}
          </table>
        </div>
      )}
    </section>
  );
}

/* ── Нормы дня ───────────────────────────────────────────── */

const TARGET_FIELDS = [
  { key: 'kcal', label: 'Калории, ккал', min: 1 },
  { key: 'protein', label: 'Белки, г', min: 0 },
  { key: 'fat', label: 'Жиры, г', min: 0 },
  { key: 'carbs', label: 'Углеводы, г', min: 0 },
  { key: 'fiber', label: 'Клетчатка, г', min: 0 },
  { key: 'sugar', label: 'Сахара, г', min: 0 },
  { key: 'waterMl', label: 'Вода, мл', min: 0 },
] as const;

type TargetKey = (typeof TARGET_FIELDS)[number]['key'];

interface TargetsSheetProps {
  open: boolean;
  targets: NutritionTargets | null;
  onClose: () => void;
  onSave: (patch: TargetsPatch) => Promise<void>;
}

function TargetsSheet({ open, targets, onClose, onSave }: TargetsSheetProps) {
  const [draft, setDraft] = useState<Record<TargetKey, string>>({
    kcal: '',
    protein: '',
    fat: '',
    carbs: '',
    fiber: '',
    sugar: '',
    waterMl: '',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !targets) return;
    setDraft({
      kcal: String(targets.kcal).replace('.', ','),
      protein: String(targets.protein).replace('.', ','),
      fat: String(targets.fat).replace('.', ','),
      carbs: String(targets.carbs).replace('.', ','),
      fiber: String(targets.fiber).replace('.', ','),
      sugar: String(targets.sugar).replace('.', ','),
      waterMl: String(targets.waterMl).replace('.', ','),
    });
    setError(null);
  }, [open, targets]);

  async function save() {
    if (busy) return;
    const patch: TargetsPatch = {};
    for (const field of TARGET_FIELDS) {
      const value = parseAmount(draft[field.key]);
      if (value === null || value < field.min) {
        setError(`Проверьте поле «${field.label}»`);
        return;
      }
      patch[field.key] = value;
    }
    setBusy(true);
    setError(null);
    try {
      await onSave(patch);
    } catch (failure) {
      setError(apiErrorMessage(failure, 'Не удалось сохранить нормы'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Нормы дня"
      description="Цель по КБЖУ и воде: от неё считается остаток за день."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button variant="primary" loading={busy} onClick={() => void save()}>
            Сохранить
          </Button>
        </>
      }
    >
      <form
        className="nu-targets"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {TARGET_FIELDS.map((field) => (
          <Field key={field.key} label={field.label} htmlFor={`nu-target-${field.key}`}>
            <Input
              id={`nu-target-${field.key}`}
              inputMode="decimal"
              value={draft[field.key]}
              onChange={(event) =>
                setDraft((current) => ({ ...current, [field.key]: event.target.value }))
              }
            />
          </Field>
        ))}
        {error ? (
          <p className="nu-add__error nu-targets__error" role="alert">
            {error}
          </p>
        ) : null}
      </form>
    </Sheet>
  );
}

/* ── Экран ───────────────────────────────────────────────── */

export function Nutrition() {
  const today = toLocalIso();
  const [date, setDate] = useState<string>(today);
  const [dayState, setDayState] = useState<NutritionDay | null>(null);
  const [dayLoading, setDayLoading] = useState(true);
  const [dayError, setDayError] = useState<string | null>(null);

  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const [summary, setSummary] = useState<NutritionSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const [series, setSeries] = useState<WaterSeries | null>(null);
  const [waterLoading, setWaterLoading] = useState(true);
  const [waterError, setWaterError] = useState<string | null>(null);
  const [waterBusy, setWaterBusy] = useState(false);

  const [openMeal, setOpenMeal] = useState<Meal | null>(null);
  const [targetsOpen, setTargetsOpen] = useState(false);

  const dayTicket = useRef(0);
  const summaryTicket = useRef(0);
  const waterTicket = useRef(0);

  /** Показываем только тот день, который запрошен: никаких чужих чисел. */
  const day = dayState && (dayState.date === date || dayState.date === '') ? dayState : null;

  const loadDay = useCallback(async (iso: string) => {
    const ticket = ++dayTicket.current;
    setDayLoading(true);
    setDayError(null);
    try {
      const payload = await fetchNutritionDay(iso);
      if (ticket !== dayTicket.current) return;
      setDayState(payload);
    } catch (failure) {
      if (ticket !== dayTicket.current) return;
      setDayError(apiErrorMessage(failure, 'сервер не ответил'));
    } finally {
      if (ticket === dayTicket.current) setDayLoading(false);
    }
  }, []);

  const loadSummary = useCallback(async (days: number) => {
    const ticket = ++summaryTicket.current;
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const payload = await fetchNutritionSummary(days);
      if (ticket !== summaryTicket.current) return;
      setSummary(payload);
    } catch (failure) {
      if (ticket !== summaryTicket.current) return;
      setSummaryError(apiErrorMessage(failure, 'сервер не ответил'));
    } finally {
      if (ticket === summaryTicket.current) setSummaryLoading(false);
    }
  }, []);

  const loadWater = useCallback(async (days: number) => {
    const ticket = ++waterTicket.current;
    setWaterLoading(true);
    setWaterError(null);
    try {
      const payload = await fetchWaterSeries(days);
      if (ticket !== waterTicket.current) return;
      setSeries(payload);
    } catch (failure) {
      if (ticket !== waterTicket.current) return;
      setWaterError(apiErrorMessage(failure, 'сервер не ответил'));
    } finally {
      if (ticket === waterTicket.current) setWaterLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDay(date);
  }, [date, loadDay]);

  useEffect(() => {
    void loadSummary(windowDays);
    void loadWater(windowDays);
  }, [windowDays, loadSummary, loadWater]);

  async function handleAddEntry(input: NewEntryInput): Promise<void> {
    const entry = await createNutritionEntry(input);
    if (entry.date === date) {
      setDayState((current) => (current ? upsertEntry(current, entry) : current));
    } else {
      void loadDay(date);
    }
    void loadSummary(windowDays);
  }

  async function handleSaveGrams(entry: FoodEntry, grams: number): Promise<void> {
    // Оптимистично: макросы пересчитываем пропорционально весу.
    setDayState((current) =>
      current
        ? upsertEntry(current, {
            ...entry,
            grams,
            ...scaleMacros(entry, entry.grams, grams),
          })
        : current,
    );
    try {
      const updated = await patchNutritionEntry(entry.id, { grams });
      setDayState((current) => (current ? upsertEntry(current, updated) : current));
      void loadSummary(windowDays);
    } catch (failure) {
      setDayState((current) => (current ? upsertEntry(current, entry) : current));
      throw failure;
    }
  }

  async function handleDeleteEntry(entry: FoodEntry): Promise<void> {
    setDayState((current) => (current ? removeEntry(current, entry.id) : current));
    try {
      await deleteNutritionEntry(entry.id);
      toast(`Удалено: ${entry.text}`, 'default');
      void loadSummary(windowDays);
    } catch (failure) {
      setDayState((current) => (current ? upsertEntry(current, entry) : current));
      toast(apiErrorMessage(failure, 'Не удалось удалить запись'), 'error');
    }
  }

  async function handleAddWater(ml: number): Promise<void> {
    setWaterBusy(true);
    try {
      await addWater(ml, date);
      await loadDay(date);
      void loadWater(windowDays);
    } catch (failure) {
      toast(apiErrorMessage(failure, 'Не удалось добавить воду'), 'error');
    } finally {
      setWaterBusy(false);
    }
  }

  async function handleSaveTargets(patch: TargetsPatch): Promise<void> {
    const updated = await putNutritionTargets(patch);
    setDayState((current) =>
      current
        ? {
            ...current,
            targets: updated,
            remaining: subtractMacros(updated, current.totals),
            water: { ...current.water, target: updated.waterMl },
          }
        : current,
    );
    setTargetsOpen(false);
    toast('Нормы сохранены', 'success');
    void loadSummary(windowDays);
  }

  const canGoForward = date < today;

  return (
    <div className="page ledger-page nu-page">
      <header className="nu-head">
        <div className="nu-head__text">
          <h1 className="ledger-title">Питание</h1>
          <p className="ledger-context">Дневник еды, калории и вода</p>
        </div>
        <div className="nu-head__actions">
          <div className="nu-day" role="group" aria-label="День дневника">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setDate(shiftIso(date, -1))}
              aria-label="Предыдущий день"
              title="Предыдущий день"
            >
              <Icon name="chevron-left" size={16} />
            </Button>
            <span className="nu-day__label" aria-live="polite" title={formatDateWithYear(parseIsoDate(date))}>
              {dayTitle(date, today)}
            </span>
            <Button
              variant="ghost"
              size="sm"
              disabled={!canGoForward}
              onClick={() => setDate(shiftIso(date, 1))}
              aria-label="Следующий день"
              title="Следующий день"
            >
              <Icon name="chevron-right" size={16} />
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={date === today}
              onClick={() => setDate(today)}
            >
              Сегодня
            </Button>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setTargetsOpen(true)}
            leadingIcon="sliders"
          >
            Нормы
          </Button>
        </div>
      </header>

      <NutritionStrip
        day={day}
        loading={dayLoading}
        error={dayError}
        onRetry={() => void loadDay(date)}
      />

      {dayError && day ? (
        <p className="nu-note" role="status">
          <span>Дневник обновился не полностью — {dayError}</span>
          <button className="nu-link" type="button" onClick={() => void loadDay(date)}>
            Повторить
          </button>
        </p>
      ) : null}

      <div className="nu-meals" role="group" aria-label="Приёмы пищи">
        {day ? (
          MEAL_ORDER.map((meal) => {
            const block = day.meals.find((item) => item.meal === meal);
            if (!block) return null;
            return (
              <MealCard
                key={meal}
                block={block}
                date={date}
                open={openMeal === meal}
                onOpen={() => setOpenMeal(meal)}
                onClose={() => setOpenMeal(null)}
                onSaveGrams={handleSaveGrams}
                onDelete={(entry) => void handleDeleteEntry(entry)}
                onSubmit={handleAddEntry}
              />
            );
          })
        ) : (
          <Skeleton variant="block" label="Загружаю приёмы пищи" className="nu-meals__skeleton" />
        )}
      </div>

      <WaterPanel
        water={day?.water ?? null}
        target={day?.targets.waterMl ?? 0}
        series={series}
        loading={waterLoading}
        error={waterError}
        busy={waterBusy}
        onAdd={(ml) => void handleAddWater(ml)}
        onRetry={() => void loadWater(windowDays)}
      />

      <HistoryPanel
        summary={summary}
        summaryDays={windowDays}
        loading={summaryLoading}
        error={summaryError}
        onDays={setWindowDays}
        onRetry={() => void loadSummary(windowDays)}
      />

      <TargetsSheet
        open={targetsOpen}
        targets={day?.targets ?? null}
        onClose={() => setTargetsOpen(false)}
        onSave={handleSaveTargets}
      />
    </div>
  );
}
