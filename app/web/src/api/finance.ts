/** Клиент Финансов (v4, `/api/finance/*`) + денежные форматтеры. */

import { apiRequest } from './client';

export type TxKind = 'income' | 'expense';
export type AccountKind = 'cash' | 'card' | 'savings' | 'broker';

export interface CategorySlice {
  id: number | null;
  name: string;
  color: string;
  kind: TxKind;
  sumKopecks: number;
  share: number;
}

export interface Budget {
  id: number;
  categoryId: number;
  name: string;
  color: string;
  month: string;
  limitKopecks: number;
  spentKopecks: number;
  pct: number;
  over: boolean;
  overKopecks: number;
}

export interface DailyPoint {
  date: string;
  incomeKopecks: number;
  expenseKopecks: number;
}

export interface FinanceSummary {
  month: string;
  incomeKopecks: number;
  expenseKopecks: number;
  balanceKopecks: number;
  byCategory: CategorySlice[];
  budgets: Budget[];
  daily: DailyPoint[];
}

export interface Transaction {
  id: number;
  date: string;
  amountKopecks: number;
  kind: TxKind;
  categoryId: number | null;
  categoryName: string;
  accountId: number | null;
  accountName: string;
  note: string;
  source: string;
}

export interface AccountsPayload {
  id: number;
  name: string;
  kind: AccountKind;
  currency: string;
  balanceStartKopecks: number;
  archived: boolean;
  sort: number;
}

export interface FinanceCategory {
  id: number;
  name: string;
  kind: TxKind;
  color: string;
  sort: number;
  archived: boolean;
}

export interface TransactionsPayload {
  items: Transaction[];
  total: number;
}

export interface TransactionsQuery {
  month: string;
  kind?: TxKind | '';
  categoryId?: number | '';
  q?: string;
  limit?: number;
}

export interface TransactionInput {
  date: string;
  amount: string | number;
  kind: TxKind;
  categoryId?: number | null;
  accountId?: number | null;
  note?: string;
}

export interface BudgetInput {
  categoryId: number;
  month: string;
  limit: string | number;
}

/* ── нормализация ─────────────────────────────────────────── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function readNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function readInt(value: unknown, fallback = 0): number {
  return Math.round(readNumber(value, fallback));
}

function readIntOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null;
}

function readKind(value: unknown): TxKind {
  return value === 'income' ? 'income' : 'expense';
}

function toKopecks(kopecksValue: unknown, rublesValue: unknown): number {
  if (typeof kopecksValue === 'number' && Number.isFinite(kopecksValue)) {
    return Math.round(kopecksValue);
  }
  return Math.round(readNumber(rublesValue, 0) * 100);
}

function normalizeSlice(raw: unknown): CategorySlice | null {
  if (!isRecord(raw)) return null;
  const sumKopecks = toKopecks(raw.sumKopecks, raw.sum);
  return {
    id: readIntOrNull(raw.id),
    name: readString(raw.name, 'Без категории'),
    color: readString(raw.color),
    kind: readKind(raw.kind),
    sumKopecks,
    share: Math.max(0, Math.min(1, readNumber(raw.share, 0))),
  };
}

function normalizeBudget(raw: unknown): Budget | null {
  if (!isRecord(raw)) return null;
  const categoryId = readInt(raw.categoryId, 0);
  if (categoryId <= 0) return null;
  const limitKopecks = toKopecks(raw.limitKopecks, raw.limit);
  const spentKopecks = toKopecks(raw.spentKopecks, raw.spent);
  const pct =
    typeof raw.pct === 'number' && Number.isFinite(raw.pct)
      ? raw.pct
      : limitKopecks > 0
        ? (spentKopecks / limitKopecks) * 100
        : 0;
  return {
    id: readInt(raw.id, 0),
    categoryId,
    name: readString(raw.name, 'Категория'),
    color: readString(raw.color),
    month: readString(raw.month),
    limitKopecks,
    spentKopecks,
    pct: Math.round(pct * 10) / 10,
    over: raw.over === true || spentKopecks > limitKopecks,
    overKopecks: Math.max(0, Math.round(readNumber(raw.overAmount, 0) * 100)),
  };
}

function normalizeDaily(raw: unknown): DailyPoint | null {
  if (!isRecord(raw)) return null;
  const date = readString(raw.date);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  return {
    date,
    incomeKopecks: toKopecks(raw.incomeKopecks, raw.income),
    expenseKopecks: toKopecks(raw.expenseKopecks, raw.expense),
  };
}

function normalizeSummary(raw: unknown): FinanceSummary {
  if (!isRecord(raw)) throw new Error('Финансы: ожидался объект сводки');
  return {
    month: readString(raw.month),
    incomeKopecks: toKopecks(raw.incomeKopecks, raw.income),
    expenseKopecks: toKopecks(raw.expenseKopecks, raw.expense),
    balanceKopecks: toKopecks(raw.balanceKopecks, raw.balance),
    byCategory: Array.isArray(raw.byCategory)
      ? raw.byCategory.flatMap((item) => {
          const slice = normalizeSlice(item);
          return slice ? [slice] : [];
        })
      : [],
    budgets: Array.isArray(raw.budgets)
      ? raw.budgets.flatMap((item) => {
          const budget = normalizeBudget(item);
          return budget ? [budget] : [];
        })
      : [],
    daily: Array.isArray(raw.daily)
      ? raw.daily.flatMap((item) => {
          const point = normalizeDaily(item);
          return point ? [point] : [];
        })
      : [],
  };
}

function normalizeTransaction(raw: unknown): Transaction | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  return {
    id,
    date: readString(raw.date),
    amountKopecks: toKopecks(raw.amountKopecks, raw.amount),
    kind: readKind(raw.kind),
    categoryId: readIntOrNull(raw.categoryId),
    categoryName: readString(raw.categoryName, 'Без категории'),
    accountId: readIntOrNull(raw.accountId),
    accountName: readString(raw.accountName),
    note: readString(raw.note),
    source: readString(raw.source, 'manual'),
  };
}

function normalizeAccount(raw: unknown): AccountsPayload | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  return {
    id,
    name: readString(raw.name, 'Счёт'),
    kind:
      raw.kind === 'cash' || raw.kind === 'savings' || raw.kind === 'broker'
        ? raw.kind
        : 'card',
    currency: readString(raw.currency, 'RUB'),
    balanceStartKopecks: toKopecks(raw.balanceStartKopecks, raw.balanceStart),
    archived: raw.archived === true,
    sort: readInt(raw.sort, 0),
  };
}

function normalizeCategory(raw: unknown): FinanceCategory | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  return {
    id,
    name: readString(raw.name, 'Категория'),
    kind: readKind(raw.kind),
    color: readString(raw.color),
    sort: readInt(raw.sort, 0),
    archived: raw.archived === true,
  };
}

/* ── API ──────────────────────────────────────────────────── */

export async function fetchFinanceSummary(month: string): Promise<FinanceSummary> {
  const raw = await apiRequest<unknown>(`/finance/summary?month=${encodeURIComponent(month)}`);
  return normalizeSummary(raw);
}

export async function fetchTransactions(query: TransactionsQuery): Promise<TransactionsPayload> {
  const params = new URLSearchParams({ month: query.month });
  if (query.kind) params.set('kind', query.kind);
  if (query.categoryId !== undefined && query.categoryId !== '') {
    params.set('categoryId', String(query.categoryId));
  }
  if (query.q?.trim()) params.set('q', query.q.trim());
  params.set('limit', String(query.limit ?? 200));
  const raw = await apiRequest<unknown>(`/finance/transactions?${params.toString()}`);
  if (!isRecord(raw)) throw new Error('Финансы: ожидался список операций');
  const items = Array.isArray(raw.items)
    ? raw.items.flatMap((item) => {
        const tx = normalizeTransaction(item);
        return tx ? [tx] : [];
      })
    : [];
  return { items, total: readInt(raw.total, items.length) };
}

export async function fetchAccounts(): Promise<AccountsPayload[]> {
  const raw = await apiRequest<unknown>('/finance/accounts');
  if (!isRecord(raw) || !Array.isArray(raw.items)) throw new Error('Финансы: нет счетов');
  return raw.items.flatMap((item) => {
    const account = normalizeAccount(item);
    return account ? [account] : [];
  });
}

export async function fetchFinanceCategories(): Promise<FinanceCategory[]> {
  const raw = await apiRequest<unknown>('/finance/categories');
  if (!isRecord(raw) || !Array.isArray(raw.items)) throw new Error('Финансы: нет категорий');
  return raw.items.flatMap((item) => {
    const category = normalizeCategory(item);
    return category ? [category] : [];
  });
}

export async function createTransaction(input: TransactionInput): Promise<Transaction> {
  const body = {
    date: input.date,
    amount: input.amount,
    kind: input.kind,
    categoryId: input.categoryId ?? null,
    accountId: input.accountId ?? null,
    note: input.note ?? '',
  };
  const raw = await apiRequest<unknown>('/finance/transactions', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  const tx = normalizeTransaction(raw);
  if (!tx) throw new Error('Финансы: операция не сохранилась');
  return tx;
}

export async function deleteTransaction(transactionId: number): Promise<void> {
  await apiRequest(`/finance/transactions/${transactionId}`, { method: 'DELETE' });
}

export async function putBudget(input: BudgetInput): Promise<Budget> {
  const raw = await apiRequest<unknown>('/finance/budgets', {
    method: 'PUT',
    body: JSON.stringify(input),
  });
  const budget = normalizeBudget(raw);
  if (!budget) throw new Error('Финансы: бюджет не сохранился');
  return budget;
}

/* ── Деньги и месяцы ──────────────────────────────────────── */

export const MONTHS_NOMINATIVE = [
  'Январь',
  'Февраль',
  'Март',
  'Апрель',
  'Май',
  'Июнь',
  'Июль',
  'Август',
  'Сентябрь',
  'Октябрь',
  'Ноябрь',
  'Декабрь',
] as const;

const MINUS = '−';

export interface FormatRubOptions {
  /** Всегда показывать знак плюса у положительных сумм. */
  signed?: boolean;
  /** Показывать копейки, даже если их нет. */
  alwaysCents?: boolean;
}

/** Копейки → «1 250,50 ₽»; нулевые копейки не показываются. */
export function formatRub(kopecks: number, options: FormatRubOptions = {}): string {
  const rounded = Math.round(kopecks);
  const absolute = Math.abs(rounded);
  const rubles = Math.floor(absolute / 100);
  const cents = absolute % 100;
  const grouped = new Intl.NumberFormat('ru-RU').format(rubles);
  const fraction = cents !== 0 || options.alwaysCents ? `,${String(cents).padStart(2, '0')}` : '';
  const sign = rounded < 0 ? MINUS : options.signed && rounded > 0 ? '+' : '';
  return `${sign}${grouped}${fraction} ₽`;
}

/** Разбор суммы из поля ввода: только рубли, не больше двух знаков после запятой. */
export function parseAmountToKopecks(value: string): { kopecks: number } | { error: string } {
  const text = value.trim().replace(/\s+/g, '').replace(',', '.');
  if (!text) return { error: 'Введите сумму' };
  if (!/^\d+(\.\d{1,2})?$/.test(text)) return { error: 'Сумма — число до копеек' };
  const [rubles, cents = ''] = text.split('.');
  const whole = Number(rubles);
  if (!Number.isFinite(whole) || whole > 1_000_000_000) {
    return { error: 'Слишком большая сумма' };
  }
  const kopecks = whole * 100 + Number(cents.padEnd(2, '0'));
  if (kopecks <= 0) return { error: 'Сумма должна быть больше нуля' };
  return { kopecks };
}

export function formatPercent(value: number, digits = 0): string {
  const safe = Number.isFinite(value) ? value : 0;
  return `${new Intl.NumberFormat('ru-RU', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(safe)}\u00A0%`;
}

export function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

export function shiftMonth(month: string, delta: number): string {
  const [yearRaw, monthRaw] = month.split('-').map(Number);
  const base = new Date(yearRaw ?? 2000, (monthRaw ?? 1) - 1 + delta, 1);
  return `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, '0')}`;
}

export function monthLabel(month: string): string {
  const [yearRaw, monthRaw] = month.split('-').map(Number);
  const index = Math.min(11, Math.max(0, (monthRaw ?? 1) - 1));
  return `${MONTHS_NOMINATIVE[index]} ${yearRaw ?? ''}`.trim();
}

export function daysInMonth(month: string): number {
  const [yearRaw, monthRaw] = month.split('-').map(Number);
  return new Date(yearRaw ?? 2000, monthRaw ?? 1, 0).getDate();
}

export function todayIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

/** Название дня месяца без ведущего нуля — для подписей оси. */
export function dayOfMonth(date: string): string {
  return String(Number(date.slice(8, 10)));
}
