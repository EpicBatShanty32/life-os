/** Клиент Библиотеки и Обучения (v4, `/api/books*`, `/api/learning`). */

import { apiRequest } from './client';

export type BookShelf = 'reading' | 'queue' | 'done' | 'dropped';
export type BookCategory = 'pro' | 'fiction' | 'nonfiction';

export interface Book {
  id: number;
  title: string;
  author: string;
  category: BookCategory;
  pagesTotal: number;
  pagesRead: number;
  shelf: BookShelf;
  rating: number | null;
  why: string;
  priority: number;
  finishedAt: string | null;
  created: string;
  updated: string | null;
  pct: number | null;
}

export interface BookCounts {
  reading: number;
  queue: number;
  done: number;
  dropped: number;
  all: number;
}

export interface BooksPayload {
  items: Book[];
  counts: BookCounts;
}

export interface BookInsight {
  id: number;
  bookId: number;
  text: string;
  sort: number;
}

export interface BookQuote {
  id: number;
  bookId: number;
  text: string;
  page: number | null;
  created: string;
}

export interface BookAction {
  id: number;
  bookId: number;
  text: string;
  taskId: number | null;
  created: string;
}

export interface BookDetail {
  book: Book;
  insights: BookInsight[];
  quotes: BookQuote[];
  actions: BookAction[];
}

export interface LearningCourse {
  id: number;
  title: string;
  organizer: string;
  deadline: string | null;
  projectId: number | null;
  goalId: number | null;
  notes: string;
  lessonsTotal: number;
  lessonsDone: number;
  pct: number;
  cover: string;
  category: string;
  created: string;
}

export interface BookInput {
  title: string;
  author?: string;
  category?: BookCategory;
  pagesTotal?: number;
  shelf?: BookShelf;
  why?: string;
  priority?: number;
}

export interface BookPatch {
  title?: string;
  author?: string;
  category?: BookCategory;
  pagesTotal?: number;
  pagesRead?: number;
  shelf?: BookShelf;
  rating?: number | null;
  why?: string;
  priority?: number;
  finishedAt?: string | null;
}

export interface CoursePatch {
  organizer?: string;
  deadline?: string | null;
  notes?: string;
}

/* ── нормализация ответов ─────────────────────────────────── */

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

function readStringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function readShelf(value: unknown): BookShelf {
  return value === 'queue' || value === 'done' || value === 'dropped' || value === 'reading'
    ? value
    : 'queue';
}

function readCategory(value: unknown): BookCategory {
  return value === 'pro' || value === 'fiction' || value === 'nonfiction'
    ? value
    : 'nonfiction';
}

function normalizeBook(raw: unknown): Book | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  const pagesTotal = Math.max(0, readInt(raw.pagesTotal, 0));
  const pagesRead = Math.max(0, Math.min(pagesTotal, readInt(raw.pagesRead, 0)));
  const pct =
    typeof raw.pct === 'number' && Number.isFinite(raw.pct)
      ? raw.pct
      : pagesTotal > 0
        ? Math.round((pagesRead / pagesTotal) * 1000) / 10
        : null;
  return {
    id,
    title: readString(raw.title, 'Без названия'),
    author: readString(raw.author),
    category: readCategory(raw.category),
    pagesTotal,
    pagesRead,
    shelf: readShelf(raw.shelf),
    rating: readIntOrNull(raw.rating),
    why: readString(raw.why),
    priority: readInt(raw.priority, 0),
    finishedAt: readStringOrNull(raw.finishedAt),
    created: readString(raw.created),
    updated: readStringOrNull(raw.updated),
    pct,
  };
}

function normalizeInsight(raw: unknown): BookInsight | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  return {
    id,
    bookId: readInt(raw.bookId, 0),
    text: readString(raw.text),
    sort: readInt(raw.sort, 0),
  };
}

function normalizeQuote(raw: unknown): BookQuote | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  return {
    id,
    bookId: readInt(raw.bookId, 0),
    text: readString(raw.text),
    page: readIntOrNull(raw.page),
    created: readString(raw.created),
  };
}

function normalizeAction(raw: unknown): BookAction | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  return {
    id,
    bookId: readInt(raw.bookId, 0),
    text: readString(raw.text),
    taskId: readIntOrNull(raw.taskId),
    created: readString(raw.created),
  };
}

function normalizeCourse(raw: unknown): LearningCourse | null {
  if (!isRecord(raw)) return null;
  const id = readInt(raw.id, 0);
  if (id <= 0) return null;
  const lessonsTotal = Math.max(0, readInt(raw.lessonsTotal, 0));
  const lessonsDone = Math.max(0, Math.min(lessonsTotal, readInt(raw.lessonsDone, 0)));
  return {
    id,
    title: readString(raw.title, 'Без названия'),
    organizer: readString(raw.organizer),
    deadline: readStringOrNull(raw.deadline),
    projectId: readIntOrNull(raw.projectId),
    goalId: readIntOrNull(raw.goalId),
    notes: readString(raw.notes),
    lessonsTotal,
    lessonsDone,
    pct: Math.round(readNumber(raw.pct, lessonsTotal > 0 ? (lessonsDone / lessonsTotal) * 100 : 0)),
    cover: readString(raw.cover),
    category: readString(raw.category),
    created: readString(raw.created),
  };
}

function mapList<T>(raw: unknown, normalize: (item: unknown) => T | null): T[] {
  return Array.isArray(raw) ? raw.flatMap((item) => {
    const value = normalize(item);
    return value ? [value] : [];
  }) : [];
}

function asBook(raw: unknown): Book {
  const book = normalizeBook(raw);
  if (!book) throw new Error('Библиотека: сервер вернул некорректную книгу');
  return book;
}

/* ── Книги ────────────────────────────────────────────────── */

export async function fetchBooks(shelf: BookShelf, query = ''): Promise<BooksPayload> {
  const params = new URLSearchParams({ shelf });
  if (query.trim()) params.set('q', query.trim());
  const raw = await apiRequest<unknown>(`/books?${params.toString()}`);
  if (!isRecord(raw)) throw new Error('Библиотека: ожидался объект');
  const items = mapList(raw.items, normalizeBook);
  const countsRaw = isRecord(raw.counts) ? raw.counts : {};
  const counts: BookCounts = {
    reading: readInt(countsRaw.reading, 0),
    queue: readInt(countsRaw.queue, 0),
    done: readInt(countsRaw.done, 0),
    dropped: readInt(countsRaw.dropped, 0),
    all: readInt(countsRaw.all, 0),
  };
  return { items, counts };
}

export async function fetchBook(bookId: number): Promise<BookDetail> {
  const raw = await apiRequest<unknown>(`/books/${bookId}`);
  if (!isRecord(raw)) throw new Error('Библиотека: ожидался объект книги');
  return {
    book: asBook(raw.book),
    insights: mapList(raw.insights, normalizeInsight).sort((a, b) => a.sort - b.sort || a.id - b.id),
    quotes: mapList(raw.quotes, normalizeQuote).sort((a, b) => (a.page ?? 10 ** 9) - (b.page ?? 10 ** 9) || a.id - b.id),
    actions: mapList(raw.actions, normalizeAction).sort((a, b) => a.id - b.id),
  };
}

export async function createBook(input: BookInput): Promise<Book> {
  const raw = await apiRequest<unknown>('/books', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return asBook(raw);
}

export async function patchBook(bookId: number, patch: BookPatch): Promise<Book> {
  const raw = await apiRequest<unknown>(`/books/${bookId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  return asBook(raw);
}

export async function deleteBook(bookId: number): Promise<void> {
  await apiRequest(`/books/${bookId}`, { method: 'DELETE' });
}

export async function saveBookProgress(bookId: number, pagesRead: number): Promise<Book> {
  const raw = await apiRequest<unknown>(`/books/${bookId}/progress`, {
    method: 'POST',
    body: JSON.stringify({ pagesRead }),
  });
  return asBook(raw);
}

/* ── Мысли, цитаты, действия ─────────────────────────────── */

export async function addBookInsight(bookId: number, text: string): Promise<BookInsight> {
  const raw = await apiRequest<unknown>(`/books/${bookId}/insights`, {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
  const insight = normalizeInsight(raw);
  if (!insight) throw new Error('Библиотека: пустая мысль');
  return insight;
}

export async function deleteBookInsight(insightId: number): Promise<void> {
  await apiRequest(`/book-insights/${insightId}`, { method: 'DELETE' });
}

export async function addBookQuote(
  bookId: number,
  text: string,
  page: number | null,
): Promise<BookQuote> {
  const raw = await apiRequest<unknown>(`/books/${bookId}/quotes`, {
    method: 'POST',
    body: JSON.stringify(page === null ? { text } : { text, page }),
  });
  const quote = normalizeQuote(raw);
  if (!quote) throw new Error('Библиотека: пустая цитата');
  return quote;
}

export async function deleteBookQuote(quoteId: number): Promise<void> {
  await apiRequest(`/book-quotes/${quoteId}`, { method: 'DELETE' });
}

export async function addBookAction(bookId: number, text: string): Promise<BookAction> {
  const raw = await apiRequest<unknown>(`/books/${bookId}/actions`, {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
  const action = normalizeAction(raw);
  if (!action) throw new Error('Библиотека: пустое действие');
  return action;
}

export interface ActionToTaskResult {
  taskId: number;
  action: BookAction;
}

export async function actionToTask(actionId: number): Promise<ActionToTaskResult> {
  const raw = await apiRequest<unknown>(`/book-actions/${actionId}/to-task`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
  if (!isRecord(raw)) throw new Error('Библиотека: задача не создалась');
  const task = isRecord(raw.task) ? raw.task : {};
  const action = normalizeAction(raw.action);
  const taskId = readInt(task.id, 0);
  if (!action || taskId <= 0) throw new Error('Библиотека: задача не создалась');
  return { taskId, action };
}

/* ── Обучение ─────────────────────────────────────────────── */

export async function fetchLearning(): Promise<LearningCourse[]> {
  const raw = await apiRequest<unknown>('/learning');
  if (!isRecord(raw)) throw new Error('Библиотека: ожидался объект обучения');
  return mapList(raw.courses, normalizeCourse);
}

export async function patchCourse(courseId: number, patch: CoursePatch): Promise<LearningCourse> {
  const raw = await apiRequest<unknown>(`/courses/${courseId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  const course = normalizeCourse(raw);
  if (!course) throw new Error('Обучение: курс не обновился');
  return course;
}
