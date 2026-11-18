import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  Button,
  DateInput,
  EmptyState,
  Field,
  Input,
  ProgressBar,
  Segmented,
  Select,
  Sheet,
  Skeleton,
  Textarea,
} from '../components/ui';
import { Icon } from '../components/Icon';
import { toast } from '../lib/toast';
import { currentFilters, navigate, setFilters } from '../lib/router';
import {
  formatDateShort,
  formatDateWithYear,
  formatNumber,
  parseIsoDate,
  pluralRu,
} from '../lib/format';
import type {
  Book,
  BookAction,
  BookCategory,
  BookCounts,
  BookDetail,
  BookShelf,
  LearningCourse,
} from '../api/library';
import {
  actionToTask,
  addBookAction,
  addBookInsight,
  addBookQuote,
  createBook,
  deleteBook,
  deleteBookInsight,
  deleteBookQuote,
  fetchBook,
  fetchBooks,
  fetchLearning,
  patchBook,
  patchCourse,
  saveBookProgress,
} from '../api/library';
import '../styles/pages-life.css';

const SHELVES: ReadonlyArray<{ value: BookShelf; label: string }> = [
  { value: 'reading', label: 'Читаю сейчас' },
  { value: 'queue', label: 'В очереди' },
  { value: 'done', label: 'Прочитано' },
  { value: 'dropped', label: 'Отложено' },
];

const SHELF_LABEL: Record<BookShelf, string> = {
  reading: 'Читаю сейчас',
  queue: 'В очереди',
  done: 'Прочитано',
  dropped: 'Отложено',
};

const CATEGORY_LABEL: Record<BookCategory, string> = {
  pro: 'проф. литература',
  fiction: 'художка',
  nonfiction: 'нон-фикшн',
};

const CATEGORY_TONE: Record<BookCategory, 'cat-4' | 'cat-2' | 'cat-3'> = {
  pro: 'cat-4',
  nonfiction: 'cat-2',
  fiction: 'cat-3',
};

const CATEGORY_OPTIONS = [
  { value: 'nonfiction', label: 'Нон-фикшн' },
  { value: 'pro', label: 'Проф. литература' },
  { value: 'fiction', label: 'Художка' },
];

const SHELF_OPTIONS = SHELVES.map((shelf) => ({ value: shelf.value, label: shelf.label }));

function pluralBooks(count: number): string {
  return `${formatNumber(count)} ${pluralRu(count, ['книга', 'книги', 'книг'])}`;
}

function bookTone(book: Book): 'cat-4' | 'cat-2' | 'cat-3' {
  return CATEGORY_TONE[book.category];
}

function queueComparator(a: Book, b: Book): number {
  return b.priority - a.priority || b.created.localeCompare(a.created) || b.id - a.id;
}

function dateShort(value: string | null): string {
  return value ? formatDateShort(value) : '—';
}

function dateLong(value: string | null): string {
  return value ? formatDateWithYear(parseIsoDate(value)) : '';
}

function dateCompact(value: string | null): string {
  if (!value) return '—';
  const iso = value.slice(0, 10);
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(2, 4)}`;
}

/* ── Строка книги ─────────────────────────────────────────── */

interface BookRowProps {
  book: Book;
  reorderable: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onOpen: (book: Book) => void;
  onMove: (book: Book, direction: -1 | 1) => void;
}

function BookRow({ book, reorderable, canMoveUp, canMoveDown, onOpen, onMove }: BookRowProps) {
  const pct = book.pct === null ? 0 : Math.round(book.pct);
  const meta =
    book.shelf === 'done'
      ? { rating: book.rating, date: book.finishedAt }
      : book.shelf === 'dropped'
        ? { rating: book.rating, date: null }
        : null;

  return (
    <li className="lib-row">
      <span
        className="lib-row__strip"
        data-tone={bookTone(book)}
        aria-hidden="true"
      />
      <button
        type="button"
        className="lib-row__main"
        onClick={() => onOpen(book)}
        aria-label={`Открыть карточку книги «${book.title}»`}
      >
        <span className="lib-row__title">{book.title}</span>
        <span className="lib-row__author">
          {book.author || 'автор не указан'}
          <span className="lib-row__cat"> · {CATEGORY_LABEL[book.category]}</span>
        </span>
      </button>

      <span className="lib-row__pages">
        {book.pagesTotal > 0
          ? `${formatNumber(book.pagesRead)} / ${formatNumber(book.pagesTotal)}`
          : '—'}
      </span>

      <span className="lib-row__progress">
        <ProgressBar
          value={book.pagesTotal > 0 ? pct : 0}
          tone={bookTone(book)}
          valueLabel={book.pagesTotal > 0 ? `${pct}%` : 'нет объёма'}
          ariaLabel={
            book.pagesTotal > 0
              ? `Прогресс «${book.title}»: ${pct}%`
              : `У книги «${book.title}» не указан объём`
          }
        />
      </span>

      <span className="lib-row__meta">
        {meta ? (
          <>
            <span className="lib-row__rating">
              {meta.rating === null ? 'без оценки' : `${meta.rating} / 5`}
            </span>
            {meta.date ? (
              <span className="lib-row__date" title={dateLong(meta.date)}>
                {book.shelf === 'done' ? dateCompact(meta.date) : dateShort(meta.date)}
              </span>
            ) : null}
          </>
        ) : null}
        {reorderable ? (
          <span className="lib-row__order">
            <button
              type="button"
              className="lib-row__arrow"
              aria-label={`Поднять «${book.title}» выше в очереди`}
              onClick={() => onMove(book, -1)}
              disabled={!canMoveUp}
            >
              <Icon name="chevron-up" size={15} />
            </button>
            <button
              type="button"
              className="lib-row__arrow"
              aria-label={`Опустить «${book.title}» ниже в очереди`}
              onClick={() => onMove(book, 1)}
              disabled={!canMoveDown}
            >
              <Icon name="chevron-down" size={15} />
            </button>
          </span>
        ) : null}
      </span>
    </li>
  );
}

/* ── Строка обучения ──────────────────────────────────────── */

function LearningRow({
  course,
  onOpen,
}: {
  course: LearningCourse;
  onOpen: (course: LearningCourse) => void;
}) {
  return (
    <li className="lib-row lib-row--learn">
      <span className="lib-row__strip" data-tone="cat-1" aria-hidden="true" />
      <button
        type="button"
        className="lib-row__main"
        onClick={() => onOpen(course)}
        aria-label={`Открыть метаданные курса «${course.title}»`}
      >
        <span className="lib-row__title">{course.title}</span>
        <span className="lib-row__author">
          {course.organizer || 'организатор не указан'}
          {course.deadline ? ` · доступ до ${formatDateShort(course.deadline)}` : ''}
        </span>
      </button>
      <span className="lib-row__pages">
        {formatNumber(course.lessonsDone)} / {formatNumber(course.lessonsTotal)}
      </span>
      <span className="lib-row__progress">
        <ProgressBar
          value={course.pct}
          tone="cat-1"
          valueLabel={`${course.pct}%`}
          ariaLabel={`Пройдено уроков курса «${course.title}»: ${course.pct}%`}
        />
      </span>
      <span className="lib-row__meta">
        <span className="lib-row__date">
          {course.deadline ? dateShort(course.deadline) : 'без дедлайна'}
        </span>
      </span>
    </li>
  );
}

/* ── Форма новой книги ────────────────────────────────────── */

interface CreateFormState {
  title: string;
  author: string;
  category: BookCategory;
  pagesTotal: string;
  shelf: BookShelf;
  why: string;
}

const EMPTY_CREATE_FORM: CreateFormState = {
  title: '',
  author: '',
  category: 'nonfiction',
  pagesTotal: '',
  shelf: 'queue',
  why: '',
};

function CreateBookSheet({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (book: Book) => void;
}) {
  const [form, setForm] = useState<CreateFormState>(EMPTY_CREATE_FORM);
  const [errors, setErrors] = useState<Partial<Record<keyof CreateFormState, string>>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setForm(EMPTY_CREATE_FORM);
      setErrors({});
      setSaving(false);
    }
  }, [open]);

  const update = <K extends keyof CreateFormState>(key: K, value: CreateFormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: undefined }));
  };

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextErrors: typeof errors = {};
    const title = form.title.trim();
    if (!title) nextErrors.title = 'Название обязательно';
    if (title.length > 300) nextErrors.title = 'Слишком длинное название';
    if (form.author.trim().length > 200) nextErrors.author = 'Слишком длинный автор';
    const totalRaw = form.pagesTotal.trim().replace(/\s+/g, '');
    const pagesTotal = totalRaw === '' ? 0 : Number(totalRaw);
    if (!Number.isInteger(pagesTotal) || pagesTotal < 0 || pagesTotal > 1_000_000) {
      nextErrors.pagesTotal = 'Целое число страниц, 0 или больше';
    }
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      return;
    }
    setSaving(true);
    try {
      const book = await createBook({
        title,
        author: form.author.trim(),
        category: form.category,
        pagesTotal,
        shelf: form.shelf,
        why: form.why.trim(),
      });
      onCreated(book);
      onClose();
      toast('Книга добавлена', 'success');
    } catch {
      toast('Не удалось добавить книгу — проверьте поля', 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Новая книга"
      description="Добавьте то, что действительно собираетесь читать."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button variant="secondary" type="submit" form="lib-create-book" loading={saving}>
            Добавить
          </Button>
        </>
      }
    >
      <form id="lib-create-book" className="book-form" onSubmit={submit}>
        <Field label="Название" htmlFor="book-title" required error={errors.title}>
          <Input
            id="book-title"
            value={form.title}
            onChange={(event) => update('title', event.target.value)}
            autoFocus
            maxLength={300}
          />
        </Field>
        <div className="book-form__row">
          <Field label="Автор" htmlFor="book-author" error={errors.author}>
            <Input
              id="book-author"
              value={form.author}
              onChange={(event) => update('author', event.target.value)}
              maxLength={200}
            />
          </Field>
          <Field label="Категория" htmlFor="book-category">
            <Select
              id="book-category"
              value={form.category}
              onChange={(event) => update('category', event.target.value as BookCategory)}
              options={CATEGORY_OPTIONS}
            />
          </Field>
        </div>
        <div className="book-form__row">
          <Field label="Страниц всего" htmlFor="book-pages" error={errors.pagesTotal} hint="Можно указать позже">
            <Input
              id="book-pages"
              value={form.pagesTotal}
              onChange={(event) => update('pagesTotal', event.target.value)}
              inputMode="numeric"
            />
          </Field>
          <Field label="Полка" htmlFor="book-shelf">
            <Select
              id="book-shelf"
              value={form.shelf}
              onChange={(event) => update('shelf', event.target.value as BookShelf)}
              options={SHELF_OPTIONS}
            />
          </Field>
        </div>
        <Field label="Зачем я взял эту книгу" htmlFor="book-why">
          <Textarea
            id="book-why"
            rows={3}
            value={form.why}
            onChange={(event) => update('why', event.target.value)}
            placeholder="Запрос на чтение: что хочу понять или изменить"
          />
        </Field>
      </form>
    </Sheet>
  );
}

/* ── Карточка книги в drawer ──────────────────────────────── */

interface BookDrawerProps {
  bookId: number | null;
  onClose: () => void;
  onChanged: () => void;
}

function BookDrawer({ bookId, onClose, onChanged }: BookDrawerProps) {
  const [detail, setDetail] = useState<BookDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const [pagesInput, setPagesInput] = useState('');
  const [totalInput, setTotalInput] = useState('');
  const [whyInput, setWhyInput] = useState('');
  const [insightInput, setInsightInput] = useState('');
  const [quoteText, setQuoteText] = useState('');
  const [quotePage, setQuotePage] = useState('');
  const [actionInput, setActionInput] = useState('');

  const open = bookId !== null;

  useEffect(() => {
    if (bookId === null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(false);
    setConfirmDelete(false);
    fetchBook(bookId)
      .then((payload) => {
        if (cancelled) return;
        setDetail(payload);
        setPagesInput(String(payload.book.pagesRead));
        setTotalInput(payload.book.pagesTotal > 0 ? String(payload.book.pagesTotal) : '');
        setWhyInput(payload.book.why);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const book = detail?.book ?? null;

  function applyBook(next: Book) {
    setDetail((current) => (current ? { ...current, book: next } : current));
  }

  function fail(message = 'Не удалось сохранить — попробуйте ещё раз') {
    toast(message, 'error');
  }

  async function run<T>(key: string, action: () => Promise<T>): Promise<T | null> {
    setBusy(key);
    try {
      return await action();
    } catch {
      fail();
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function commitProgress(nextRead: number) {
    if (!book || book.pagesTotal <= 0) return;
    const clamped = Math.max(0, Math.min(book.pagesTotal, Math.round(nextRead)));
    if (!Number.isFinite(clamped)) return;
    const previous = book;
    applyBook({
      ...book,
      pagesRead: clamped,
      pct: Math.round((clamped / book.pagesTotal) * 1000) / 10,
    });
    setPagesInput(String(clamped));
    const updated = await run('progress', () => saveBookProgress(book.id, clamped));
    if (updated) {
      applyBook(updated);
      setPagesInput(String(updated.pagesRead));
      onChanged();
    } else {
      applyBook(previous);
      setPagesInput(String(previous.pagesRead));
    }
  }

  async function savePagesTotal() {
    if (!book) return;
    const total = Number(totalInput.trim().replace(/\s+/g, ''));
    if (!Number.isInteger(total) || total < 0 || total > 1_000_000) {
      toast('Объём — целое число страниц', 'error');
      return;
    }
    const updated = await run('total', () =>
      patchBook(book.id, {
        pagesTotal: total,
        pagesRead: Math.min(book.pagesRead, total),
      }),
    );
    if (updated) {
      applyBook(updated);
      setPagesInput(String(updated.pagesRead));
      setTotalInput(updated.pagesTotal > 0 ? String(updated.pagesTotal) : '');
      toast('Объём книги сохранён', 'success');
      onChanged();
    }
  }

  async function saveWhy() {
    if (!book) return;
    const why = whyInput.trim();
    const previous = book;
    applyBook({ ...book, why });
    const updated = await run('why', () => patchBook(book.id, { why }));
    if (updated) {
      applyBook(updated);
      onChanged();
    } else {
      applyBook(previous);
      setWhyInput(previous.why);
    }
  }

  async function saveRating(value: number) {
    if (!book) return;
    const rating = value === 0 ? null : value;
    const previous = book;
    applyBook({ ...book, rating });
    const updated = await run('rating', () => patchBook(book.id, { rating }));
    if (updated) {
      applyBook(updated);
      onChanged();
    } else {
      applyBook(previous);
    }
  }

  async function saveFinished(value: string) {
    if (!book) return;
    const previous = book;
    const finishedAt = value || null;
    applyBook({ ...book, finishedAt });
    const updated = await run('finished', () => patchBook(book.id, { finishedAt }));
    if (updated) {
      applyBook(updated);
      onChanged();
    } else {
      applyBook(previous);
    }
  }

  async function saveShelf(shelf: BookShelf) {
    if (!book || shelf === book.shelf) return;
    const previous = book;
    applyBook({ ...book, shelf });
    const updated = await run('shelf', () => patchBook(book.id, { shelf }));
    if (updated) {
      applyBook(updated);
      setPagesInput(String(updated.pagesRead));
      onChanged();
      if (updated.shelf !== shelf) {
        toast(`Полка изменилась на «${SHELF_LABEL[updated.shelf]}»`, 'default');
      }
    } else {
      applyBook(previous);
    }
  }

  async function submitInsight() {
    if (!book) return;
    const text = insightInput.trim();
    if (!text) return;
    const tempId = -Date.now();
    const temp = { id: tempId, bookId: book.id, text, sort: 999 };
    setDetail((current) =>
      current ? { ...current, insights: [...current.insights, temp] } : current,
    );
    setInsightInput('');
    try {
      const insight = await addBookInsight(book.id, text);
      setDetail((current) =>
        current
          ? { ...current, insights: current.insights.map((item) => (item.id === tempId ? insight : item)) }
          : current,
      );
    } catch {
      setDetail((current) =>
        current
          ? { ...current, insights: current.insights.filter((item) => item.id !== tempId) }
          : current,
      );
      setInsightInput(text);
      fail('Мысль не сохранилась');
    }
  }

  async function removeInsight(insightId: number) {
    if (!detail) return;
    const removed = detail.insights.find((item) => item.id === insightId);
    if (!removed) return;
    setDetail({ ...detail, insights: detail.insights.filter((item) => item.id !== insightId) });
    try {
      await deleteBookInsight(insightId);
    } catch {
      setDetail((current) =>
        current
          ? { ...current, insights: [...current.insights, removed].sort((a, b) => a.sort - b.sort || a.id - b.id) }
          : current,
      );
      fail('Не удалось удалить мысль');
    }
  }

  async function submitQuote() {
    if (!book) return;
    const text = quoteText.trim();
    if (!text) return;
    const pageRaw = quotePage.trim();
    const page = pageRaw === '' ? null : Number(pageRaw);
    if (page !== null && (!Number.isInteger(page) || page < 0)) {
      toast('Страница — целое число', 'error');
      return;
    }
    const tempId = -Date.now();
    const temp = { id: tempId, bookId: book.id, text, page, created: '' };
    setDetail((current) =>
      current
        ? {
            ...current,
            quotes: [...current.quotes, temp].sort(
              (a, b) => (a.page ?? 10 ** 9) - (b.page ?? 10 ** 9) || a.id - b.id,
            ),
          }
        : current,
    );
    setQuoteText('');
    setQuotePage('');
    try {
      const quote = await addBookQuote(book.id, text, page);
      setDetail((current) =>
        current
          ? { ...current, quotes: current.quotes.map((item) => (item.id === tempId ? quote : item)) }
          : current,
      );
    } catch {
      setDetail((current) =>
        current
          ? { ...current, quotes: current.quotes.filter((item) => item.id !== tempId) }
          : current,
      );
      setQuoteText(text);
      setQuotePage(page === null ? '' : String(page));
      fail('Цитата не сохранилась');
    }
  }

  async function removeQuote(quoteId: number) {
    if (!detail) return;
    const removed = detail.quotes.find((item) => item.id === quoteId);
    if (!removed) return;
    setDetail({ ...detail, quotes: detail.quotes.filter((item) => item.id !== quoteId) });
    try {
      await deleteBookQuote(quoteId);
    } catch {
      setDetail((current) =>
        current
          ? { ...current, quotes: [...current.quotes, removed].sort((a, b) => (a.page ?? 10 ** 9) - (b.page ?? 10 ** 9) || a.id - b.id) }
          : current,
      );
      fail('Не удалось удалить цитату');
    }
  }

  async function submitAction() {
    if (!book) return;
    const text = actionInput.trim();
    if (!text) return;
    const tempId = -Date.now();
    const temp: BookAction = { id: tempId, bookId: book.id, text, taskId: null, created: '' };
    setDetail((current) => (current ? { ...current, actions: [...current.actions, temp] } : current));
    setActionInput('');
    try {
      const action = await addBookAction(book.id, text);
      setDetail((current) =>
        current
          ? { ...current, actions: current.actions.map((item) => (item.id === tempId ? action : item)) }
          : current,
      );
    } catch {
      setDetail((current) =>
        current ? { ...current, actions: current.actions.filter((item) => item.id !== tempId) } : current,
      );
      setActionInput(text);
      fail('Действие не сохранилось');
    }
  }

  async function makeTask(action: BookAction) {
    if (busy) return;
    setBusy(`task-${action.id}`);
    try {
      const result = await actionToTask(action.id);
      setDetail((current) =>
        current
          ? {
              ...current,
              actions: current.actions.map((item) =>
                item.id === result.action.id ? result.action : item,
              ),
            }
          : current,
      );
      toast(`Задача №${result.taskId} создана`, 'success');
    } catch {
      fail('Задача не создалась');
    } finally {
      setBusy(null);
    }
  }

  async function removeBook() {
    if (!book) return;
    if (!confirmDelete) {
      setConfirmDelete(true);
      window.setTimeout(() => setConfirmDelete(false), 5000);
      return;
    }
    setBusy('delete');
    try {
      await deleteBook(book.id);
      toast('Книга удалена', 'success');
      onClose();
      onChanged();
    } catch {
      fail('Не удалось удалить книгу');
    } finally {
      setBusy(null);
    }
  }

  const ratingOptions = [
    { value: '0', label: '—' },
    { value: '1', label: '1' },
    { value: '2', label: '2' },
    { value: '3', label: '3' },
    { value: '4', label: '4' },
    { value: '5', label: '5' },
  ];

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={book?.title ?? 'Книга'}
      description={
        book
          ? `${book.author || 'автор не указан'} · ${CATEGORY_LABEL[book.category]} · ${SHELF_LABEL[book.shelf]}`
          : undefined
      }
      footer={
        book ? (
          <div className="book-foot">
            <Button
              variant="danger"
              onClick={removeBook}
              loading={busy === 'delete'}
            >
              {confirmDelete ? 'Точно удалить?' : 'Удалить книгу'}
            </Button>
            <Button variant="secondary" onClick={onClose}>
              Готово
            </Button>
          </div>
        ) : (
          <Button variant="secondary" onClick={onClose}>
            Закрыть
          </Button>
        )
      }
    >
      {loading ? (
        <div className="book-loading" role="status" aria-label="Загружаем карточку книги">
          <Skeleton variant="title" />
          <Skeleton variant="text" lines={2} />
          <Skeleton variant="block" />
        </div>
      ) : error ? (
        <p className="ledger-error">
          Карточка не загрузилась.
          <button
            type="button"
            className="ledger-retry"
            onClick={() => {
              if (bookId !== null) {
                setError(false);
                setLoading(true);
                fetchBook(bookId)
                  .then((payload) => {
                    setDetail(payload);
                    setPagesInput(String(payload.book.pagesRead));
                    setTotalInput(payload.book.pagesTotal > 0 ? String(payload.book.pagesTotal) : '');
                    setWhyInput(payload.book.why);
                  })
                  .catch(() => setError(true))
                  .finally(() => setLoading(false));
              }
            }}
          >
            Повторить
          </button>
        </p>
      ) : !book || !detail ? (
        <p className="book-empty">Книга не найдена.</p>
      ) : (
        <>
          <section className="book-sec">
            <header className="book-sec__head">
              <h3 className="book-sec__title">Прогресс</h3>
              <span className="book-sec__num">
                {book.pagesTotal > 0 ? `${Math.round(book.pct ?? 0)}%` : 'объём не задан'}
              </span>
            </header>
            <ProgressBar
              value={book.pagesTotal > 0 ? (book.pct ?? 0) : 0}
              tone={bookTone(book)}
              ariaLabel={`Прогресс книги: ${Math.round(book.pct ?? 0)}%`}
            />
            <p className="book-sec__line">
              {book.pagesTotal > 0
                ? `${formatNumber(book.pagesRead)} из ${formatNumber(book.pagesTotal)} страниц`
                : 'Сначала укажите общее число страниц — без него прогресс не сохраняется.'}
            </p>
            <div className="book-inline">
              <label className="book-inline__label" htmlFor="book-progress">
                Прочитано
              </label>
              <Input
                id="book-progress"
                value={pagesInput}
                onChange={(event) => setPagesInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void commitProgress(Number(pagesInput));
                  }
                }}
                inputMode="numeric"
                size="sm"
                disabled={book.pagesTotal <= 0}
              />
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void commitProgress(Number(pagesInput))}
                disabled={book.pagesTotal <= 0}
                loading={busy === 'progress'}
              >
                Сохранить
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void commitProgress(book.pagesRead + 10)}
                disabled={book.pagesTotal <= 0 || book.pagesRead >= book.pagesTotal}
              >
                +10
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void commitProgress(book.pagesRead + 25)}
                disabled={book.pagesTotal <= 0 || book.pagesRead >= book.pagesTotal}
              >
                +25
              </Button>
            </div>
            <div className="book-inline book-inline--total">
              <label className="book-inline__label" htmlFor="book-total">
                Страниц всего
              </label>
              <Input
                id="book-total"
                value={totalInput}
                onChange={(event) => setTotalInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void savePagesTotal();
                  }
                }}
                inputMode="numeric"
                size="sm"
              />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void savePagesTotal()}
                loading={busy === 'total'}
              >
                Сохранить объём
              </Button>
            </div>
          </section>

          <section className="book-sec">
            <header className="book-sec__head">
              <h3 className="book-sec__title">Зачем взял</h3>
            </header>
            <Textarea
              rows={3}
              value={whyInput}
              onChange={(event) => setWhyInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  void saveWhy();
                }
              }}
              aria-label="Зачем я взял эту книгу"
              placeholder="Что хочу понять или изменить после чтения"
            />
            <div className="book-sec__actions">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void saveWhy()}
                loading={busy === 'why'}
                disabled={whyInput.trim() === book.why}
              >
                Сохранить
              </Button>
              <span className="book-hint">Enter — сохранить, Shift+Enter — новая строка</span>
            </div>
          </section>

          <section className="book-sec">
            <header className="book-sec__head">
              <h3 className="book-sec__title">Главные мысли</h3>
              <span className="book-sec__num">
                {detail.insights.length === 0
                  ? 'три — достаточно'
                  : detail.insights.length <= 3
                    ? `${detail.insights.length} из 3`
                    : `${detail.insights.length} · оставьте 3`}
              </span>
            </header>
            {detail.insights.length === 0 ? (
              <p className="book-sec__empty">Сформулируйте три мысли, которые хотите унести.</p>
            ) : (
              <ol className="book-thoughts">
                {detail.insights.map((insight, index) => (
                  <li key={insight.id} className="book-thought">
                    <span className="book-thought__num" aria-hidden="true">
                      {index + 1}
                    </span>
                    <span className="book-thought__text">{insight.text}</span>
                    <button
                      type="button"
                      className="book-icon-btn"
                      aria-label={`Удалить мысль: ${insight.text}`}
                      onClick={() => void removeInsight(insight.id)}
                    >
                      <Icon name="trash" size={15} />
                    </button>
                  </li>
                ))}
              </ol>
            )}
            <div className="book-inline">
              <Input
                value={insightInput}
                onChange={(event) => setInsightInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void submitInsight();
                  }
                }}
                placeholder="Мысль, которая меняет взгляд"
                aria-label="Новая мысль"
                size="sm"
              />
              <Button variant="secondary" size="sm" onClick={() => void submitInsight()}>
                Добавить
              </Button>
            </div>
          </section>

          <section className="book-sec">
            <header className="book-sec__head">
              <h3 className="book-sec__title">Копилка цитат</h3>
              <span className="book-sec__num">{detail.quotes.length} шт.</span>
            </header>
            {detail.quotes.length === 0 ? (
              <p className="book-sec__empty">Сохраните цитату — текст и страницу, чтобы вернуться.</p>
            ) : (
              <ul className="book-quotes">
                {detail.quotes.map((quote) => (
                  <li key={quote.id} className="book-quote">
                    <blockquote>
                      <p>{quote.text}</p>
                      <footer>
                        {quote.page === null ? 'без страницы' : `с. ${quote.page}`}
                      </footer>
                    </blockquote>
                    <button
                      type="button"
                      className="book-icon-btn"
                      aria-label={`Удалить цитату${quote.page === null ? '' : ` со страницы ${quote.page}`}`}
                      onClick={() => void removeQuote(quote.id)}
                    >
                      <Icon name="trash" size={15} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="book-quote-add">
              <Textarea
                rows={2}
                value={quoteText}
                onChange={(event) => setQuoteText(event.target.value)}
                placeholder="Цитата"
                aria-label="Текст новой цитаты"
              />
              <div className="book-inline">
                <Input
                  value={quotePage}
                  onChange={(event) => setQuotePage(event.target.value)}
                  inputMode="numeric"
                  placeholder="Стр."
                  aria-label="Страница цитаты"
                  size="sm"
                />
                <Button variant="secondary" size="sm" onClick={() => void submitQuote()}>
                  Сохранить цитату
                </Button>
              </div>
            </div>
          </section>

          <section className="book-sec">
            <header className="book-sec__head">
              <h3 className="book-sec__title">Действие на внедрение</h3>
            </header>
            {detail.actions.length === 0 ? (
              <p className="book-sec__empty">
                Что сделаете иначе? Добавьте действие и превратите его в задачу.
              </p>
            ) : (
              <ul className="book-actions">
                {detail.actions.map((action) => (
                  <li key={action.id} className="book-action">
                    <span className="book-action__text">{action.text}</span>
                    {action.taskId !== null ? (
                      <span className="book-action__task">
                        <Icon name="check" size={14} />
                        в задаче №{action.taskId}
                      </span>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        leadingIcon="arrow-up-right"
                        onClick={() => void makeTask(action)}
                        loading={busy === `task-${action.id}`}
                        disabled={action.id < 0}
                      >
                        В задачу
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <div className="book-inline">
              <Input
                value={actionInput}
                onChange={(event) => setActionInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void submitAction();
                  }
                }}
                placeholder="Например: ввести 5 минут дыхания перед сном"
                aria-label="Новое действие"
                size="sm"
              />
              <Button variant="secondary" size="sm" onClick={() => void submitAction()}>
                Добавить
              </Button>
            </div>
          </section>

          <section className="book-sec">
            <header className="book-sec__head">
              <h3 className="book-sec__title">Оценка и завершение</h3>
            </header>
            <div className="book-rating">
              <span className="book-inline__label" id="book-rating-label">
                Оценка
              </span>
              <Segmented
                ariaLabel="Оценка книги от 1 до 5"
                value={String(book.rating ?? 0)}
                options={ratingOptions}
                onChange={(value) => void saveRating(Number(value))}
              />
            </div>
            <div className="book-inline">
              <label className="book-inline__label" htmlFor="book-finished">
                Завершена
              </label>
              <DateInput
                id="book-finished"
                value={book.finishedAt ? book.finishedAt.slice(0, 10) : ''}
                onChange={(event) => void saveFinished(event.target.value)}
                size="sm"
              />
              <span className="book-hint">Дата проставляется сама при 100 % страниц</span>
            </div>
            <div className="book-inline">
              <label className="book-inline__label" htmlFor="book-shelf-drawer">
                Полка
              </label>
              <Select
                id="book-shelf-drawer"
                value={book.shelf}
                options={SHELF_OPTIONS}
                onChange={(event) => void saveShelf(event.target.value as BookShelf)}
                disabled={busy === 'shelf'}
                size="sm"
              />
              <span className="book-hint">«Прочитано» проставит 100 % и дату завершения</span>
            </div>
          </section>
        </>
      )}
    </Sheet>
  );
}

/* ── Метаданные курса ─────────────────────────────────────── */

function CourseSheet({
  course,
  onClose,
  onSaved,
}: {
  course: LearningCourse | null;
  onClose: () => void;
  onSaved: (course: LearningCourse) => void;
}) {
  const [organizer, setOrganizer] = useState('');
  const [deadline, setDeadline] = useState('');
  const [notes, setNotes] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!course) return;
    setOrganizer(course.organizer);
    setDeadline(course.deadline ? course.deadline.slice(0, 10) : '');
    setNotes(course.notes);
  }, [course]);

  async function save() {
    if (!course) return;
    setSaving(true);
    try {
      const updated = await patchCourse(course.id, {
        organizer: organizer.trim(),
        deadline: deadline || null,
        notes,
      });
      onSaved(updated);
      toast('Метаданные курса сохранены', 'success');
      onClose();
    } catch {
      toast('Не удалось сохранить курс', 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Sheet
      open={course !== null}
      onClose={onClose}
      title={course?.title ?? 'Курс'}
      description={
        course
          ? `${formatNumber(course.lessonsDone)} из ${formatNumber(course.lessonsTotal)} уроков · ${course.pct}%`
          : undefined
      }
      footer={
        <>
          <Button variant="ghost" onClick={() => navigate('/courses')}>
            Открыть курс
          </Button>
          <Button variant="secondary" type="submit" form="course-meta" loading={saving}>
            Сохранить
          </Button>
        </>
      }
    >
      <form
        id="course-meta"
        className="book-form"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <Field label="Организатор" htmlFor="course-organizer">
          <Input
            id="course-organizer"
            value={organizer}
            onChange={(event) => setOrganizer(event.target.value)}
            maxLength={200}
            autoFocus
          />
        </Field>
        <Field label="Доступ до" htmlFor="course-deadline" hint="Пусто — без дедлайна">
          <DateInput
            id="course-deadline"
            value={deadline}
            onChange={(event) => setDeadline(event.target.value)}
          />
        </Field>
        <Field label="Конспект" htmlFor="course-notes">
          <Textarea
            id="course-notes"
            rows={5}
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </Field>
      </form>
    </Sheet>
  );
}

/* ── Страница ─────────────────────────────────────────────── */

function readShelfFilter(value: string | undefined): BookShelf {
  return value === 'queue' || value === 'done' || value === 'dropped' || value === 'reading'
    ? value
    : 'reading';
}

export function Library() {
  const initialFilters = useRef(currentFilters()).current;
  const initialQuery = (initialFilters.q ?? '').trim();
  const [tab, setTab] = useState<'books' | 'learning'>(
    initialFilters.tab === 'learning' ? 'learning' : 'books',
  );
  const [shelf, setShelf] = useState<BookShelf>(() =>
    readShelfFilter(initialFilters.status ?? initialFilters.shelf),
  );
  const [query, setQuery] = useState(initialQuery);
  const [appliedQuery, setAppliedQuery] = useState(initialQuery);
  const [books, setBooks] = useState<Book[]>([]);
  const [counts, setCounts] = useState<BookCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [openBookId, setOpenBookId] = useState<number | null>(null);

  const [courses, setCourses] = useState<LearningCourse[]>([]);
  const [coursesLoaded, setCoursesLoaded] = useState(false);
  const [coursesLoading, setCoursesLoading] = useState(false);
  const [coursesError, setCoursesError] = useState(false);
  const [editingCourse, setEditingCourse] = useState<LearningCourse | null>(null);

  const requestSeq = useRef(0);
  const firstLoad = useRef(true);

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedQuery(query.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    setFilters(
      {
        tab: tab === 'learning' ? 'learning' : '',
        status: tab === 'books' && shelf !== 'reading' ? shelf : '',
        q: tab === 'books' ? appliedQuery : '',
      },
      { replace: true },
    );
  }, [tab, shelf, appliedQuery]);

  const loadBooks = useCallback(async (nextShelf: BookShelf, nextQuery: string) => {
    const seq = ++requestSeq.current;
    if (firstLoad.current) setLoading(true);
    try {
      const payload = await fetchBooks(nextShelf, nextQuery);
      if (seq !== requestSeq.current) return;
      setBooks(payload.items);
      setCounts(payload.counts);
      setLoadError(false);
    } catch {
      if (seq !== requestSeq.current) return;
      setLoadError(true);
    } finally {
      if (seq === requestSeq.current) {
        firstLoad.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadBooks(shelf, appliedQuery);
  }, [shelf, appliedQuery, loadBooks]);

  const reloadBooks = useCallback(() => {
    void loadBooks(shelf, appliedQuery);
  }, [loadBooks, shelf, appliedQuery]);

  const loadLearning = useCallback(async () => {
    setCoursesLoading(true);
    try {
      const list = await fetchLearning();
      setCourses(list);
      setCoursesError(false);
      setCoursesLoaded(true);
    } catch {
      setCoursesError(true);
    } finally {
      setCoursesLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === 'learning' && !coursesLoaded && !coursesLoading) {
      void loadLearning();
    }
  }, [tab, coursesLoaded, coursesLoading, loadLearning]);

  function moveBook(book: Book, direction: -1 | 1) {
    if (appliedQuery) return;
    const ordered = [...books].sort(queueComparator);
    const index = ordered.findIndex((item) => item.id === book.id);
    const other = ordered[index + direction];
    if (!other) return;
    const swapped = books.map((item) => {
      if (item.id === book.id) return { ...item, priority: other.priority };
      if (item.id === other.id) return { ...item, priority: book.priority };
      return item;
    });
    setBooks(swapped.sort(queueComparator));
    Promise.all([
      patchBook(book.id, { priority: other.priority }),
      patchBook(other.id, { priority: book.priority }),
    ])
      .then(() => reloadBooks())
      .catch(() => {
        toast('Не удалось изменить порядок', 'error');
        reloadBooks();
      });
  }

  const context = counts
    ? [
        pluralBooks(counts.all),
        `${counts.reading} читаю сейчас`,
        `${counts.queue} в очереди`,
        `${counts.done} прочитано`,
      ].join(' · ')
    : 'Загружаем библиотеку';

  return (
    <div className="page ledger-page lib-page">
      <header className="ledger-head">
        <div className="ledger-head__text">
          <h1 className="ledger-title">Библиотека</h1>
          <p className="ledger-context">{context}</p>
        </div>
        <div className="ledger-head__actions">
          {tab === 'books' ? (
            <Button
              variant="secondary"
              leadingIcon="plus"
              onClick={() => setCreateOpen(true)}
            >
              Добавить книгу
            </Button>
          ) : (
            <Button
              variant="ghost"
              leadingIcon="arrow-up-right"
              onClick={() => navigate('/courses')}
            >
              Открыть курсы
            </Button>
          )}
        </div>
      </header>

      <div className="lib-toolbar">
        <Segmented
          ariaLabel="Раздел библиотеки"
          value={tab}
          options={[
            { value: 'books', label: 'Книги' },
            { value: 'learning', label: 'Обучение', icon: 'play' },
          ]}
          onChange={(value) => setTab(value)}
        />
        {tab === 'books' ? (
          <>
            <Segmented
              ariaLabel="Полка книг"
              value={shelf}
              options={SHELVES}
              onChange={(value) => setShelf(value)}
              className="lib-toolbar__shelves"
            />
            <div className="lib-search">
              <label htmlFor="lib-search" className="visually-hidden">
                Поиск книг
              </label>
              <Input
                id="lib-search"
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Название, автор или «зачем»"
                size="sm"
              />
              {query ? (
                <Button variant="ghost" size="sm" onClick={() => setQuery('')}>
                  Сбросить
                </Button>
              ) : null}
            </div>
          </>
        ) : (
          <p className="ledger-context lib-learning-note">
            Метаданные сохраняются в Библиотеке, уроки — в разделе «Курсы».
          </p>
        )}
      </div>

      {tab === 'books' ? (
        <section className="lib-ledger" aria-labelledby="lib-shelf-title">
          <h2 className="visually-hidden" id="lib-shelf-title">
            Книги на полке «{SHELF_LABEL[shelf]}»
          </h2>
          {shelf === 'reading' && counts && counts.reading > 3 ? (
            <p className="lib-note" role="status">
              <span className="lib-note__dot" aria-hidden="true" />
              Читаете {counts.reading} книг одновременно — обычно это распыление. Оставьте 2–3.
            </p>
          ) : null}
          {loadError && books.length === 0 ? (
            <div className="lib-state">
              <p className="ledger-error">
                Не удалось загрузить библиотеку.
                <button type="button" className="ledger-retry" onClick={reloadBooks}>
                  Повторить
                </button>
              </p>
            </div>
          ) : loading ? (
            <div className="lib-state" role="status" aria-label="Загружаем книги">
              <Skeleton variant="row" />
              <Skeleton variant="row" />
              <Skeleton variant="row" />
            </div>
          ) : books.length === 0 ? (
            <div className="lib-state">
              <EmptyState
                title={
                  appliedQuery
                    ? `По запросу «${appliedQuery}» ничего не нашлось`
                    : shelf === 'reading'
                      ? 'Сейчас ничего не читаете'
                      : shelf === 'queue'
                        ? 'Очередь пуста'
                        : shelf === 'done'
                          ? 'Прочитанных книг пока нет'
                          : 'Отложенных книг нет'
                }
                description={
                  appliedQuery
                    ? 'Попробуйте другое слово или сбросьте поиск.'
                    : shelf === 'reading'
                      ? 'Возьмите книгу из очереди или добавьте новую — полка покажет прогресс.'
                      : shelf === 'queue'
                        ? 'Добавьте, что читать следующим, и расставьте приоритет.'
                        : shelf === 'done'
                          ? 'Здесь появятся оценки и даты завершения.'
                          : 'Сюда попадут книги, которые не зашли.'
                }
                action={
                  appliedQuery ? (
                    <Button variant="ghost" size="sm" onClick={() => setQuery('')}>
                      Сбросить поиск
                    </Button>
                  ) : (
                    <Button variant="secondary" size="sm" onClick={() => setCreateOpen(true)}>
                      Добавить книгу
                    </Button>
                  )
                }
              />
            </div>
          ) : (
            <>
              {loadError ? (
                <p className="ledger-error ledger-error--inline">
                  Список мог устареть.
                  <button type="button" className="ledger-retry" onClick={reloadBooks}>
                    Обновить
                  </button>
                </p>
              ) : null}
              <div className="lib-row lib-row--head" aria-hidden="true">
                <span />
                <span>Книга</span>
                <span>Страницы</span>
                <span>Прогресс</span>
                <span>
                  {shelf === 'queue' ? 'Приоритет' : shelf === 'reading' ? '' : 'Отметка'}
                </span>
              </div>
              <ul className="lib-rows">
                {books.map((book, index) => (
                  <BookRow
                    key={book.id}
                    book={book}
                    reorderable={shelf === 'queue' && !appliedQuery}
                    canMoveUp={shelf === 'queue' && !appliedQuery && index > 0}
                    canMoveDown={
                      shelf === 'queue' && !appliedQuery && index < books.length - 1
                    }
                    onOpen={(item) => setOpenBookId(item.id)}
                    onMove={moveBook}
                  />
                ))}
              </ul>
            </>
          )}
        </section>
      ) : (
        <section className="lib-ledger" aria-labelledby="lib-learning-title">
          <h2 className="visually-hidden" id="lib-learning-title">
            Обучение
          </h2>
          {coursesError && courses.length === 0 ? (
            <div className="lib-state">
              <p className="ledger-error">
                Не удалось загрузить курсы.
                <button type="button" className="ledger-retry" onClick={() => void loadLearning()}>
                  Повторить
                </button>
              </p>
            </div>
          ) : coursesLoading ? (
            <div className="lib-state" role="status" aria-label="Загружаем курсы">
              <Skeleton variant="row" />
              <Skeleton variant="row" />
            </div>
          ) : courses.length === 0 ? (
            <div className="lib-state">
              <EmptyState
                title="Курсов пока нет"
                description="Добавьте курс в разделе «Курсы» — здесь появятся организатор, дедлайн и конспект."
                action={
                  <Button variant="secondary" size="sm" onClick={() => navigate('/courses')}>
                    Перейти к курсам
                  </Button>
                }
              />
            </div>
          ) : (
            <>
              <div className="lib-row lib-row--head lib-row--learn-head" aria-hidden="true">
                <span />
                <span>Курс</span>
                <span>Уроки</span>
                <span>Прогресс</span>
                <span>Доступ до</span>
              </div>
              <ul className="lib-rows">
                {courses.map((course) => (
                  <LearningRow
                    key={course.id}
                    course={course}
                    onOpen={(item) => setEditingCourse(item)}
                  />
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      <CreateBookSheet
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => reloadBooks()}
      />
      <BookDrawer
        bookId={openBookId}
        onClose={() => setOpenBookId(null)}
        onChanged={reloadBooks}
      />
      <CourseSheet
        course={editingCourse}
        onClose={() => setEditingCourse(null)}
        onSaved={(updated) => {
          setCourses((current) =>
            current.map((course) => (course.id === updated.id ? updated : course)),
          );
        }}
      />
    </div>
  );
}
