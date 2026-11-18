import { useEffect, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { capitalize, formatDateLong, parseIsoDate, toLocalIso } from '../../lib/format';
import { Skeleton } from '../ui';

/* ─────────────────────────────────────────────────────────────
   Общие детали экранов «Компас» и «Дела»: шапка страницы,
   точки сфер/ресурсов, тихие ошибки и мелкие форматтеры.
   ───────────────────────────────────────────────────────────── */

type CssVars = CSSProperties & Record<`--${string}`, string>;

export interface PageHeadProps {
  date?: string;
  title: string;
  note?: ReactNode;
  actions?: ReactNode;
}

export function PageHead({ date, title, note, actions }: PageHeadProps) {
  return (
    <header className="pc-head">
      <div className="pc-head__text">
        {date ? <p className="pc-head__date">{date}</p> : null}
        <h1 className="pc-head__title">{title}</h1>
        {note ? <p className="pc-head__note">{note}</p> : null}
      </div>
      {actions ? <div className="pc-head__actions">{actions}</div> : null}
    </header>
  );
}

export function SphereDot({
  color,
  className,
}: {
  color?: string | null;
  className?: string;
}) {
  const style: CssVars = { '--pc-sphere': color?.trim() || 'var(--cat-1)' };
  return <span className={className ? `pc-dot ${className}` : 'pc-dot'} style={style} aria-hidden="true" />;
}

export function ResourceDot({ resource }: { resource: 'must' | 'important' | 'low' }) {
  return <span className="pc-dot pc-dot--resource" data-resource={resource} aria-hidden="true" />;
}

export function InlineError({
  text = 'Не удалось загрузить',
  onRetry,
}: {
  text?: string;
  onRetry?: () => void;
}) {
  return (
    <p className="pc-error" role="status">
      <span>{text}</span>
      {onRetry ? (
        <>
          <span aria-hidden="true"> · </span>
          <button type="button" className="pc-error__retry" onClick={onRetry}>
            Повторить
          </button>
        </>
      ) : null}
    </p>
  );
}

export function SkeletonRows({ rows = 3, label = 'Загрузка' }: { rows?: number; label?: string }) {
  return (
    <div className="pc-skeleton" aria-busy="true">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} variant="row" label={label} />
      ))}
    </div>
  );
}

/* ── Даты и время ────────────────────────────────────────── */

const MONTHS_SHORT = [
  'янв',
  'фев',
  'мар',
  'апр',
  'мая',
  'июн',
  'июл',
  'авг',
  'сен',
  'окт',
  'ноя',
  'дек',
];

export const WEEKDAY_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
export const WEEKDAY_FULL = [
  'понедельник',
  'вторник',
  'среда',
  'четверг',
  'пятница',
  'суббота',
  'воскресенье',
];

export function todayIso(): string {
  return toLocalIso();
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return 'без срока';
  const date = parseIsoDate(value);
  return `${date.getDate()} ${MONTHS_SHORT[date.getMonth()]}`;
}

export function dateWithWeekday(value: string | Date): string {
  const date = typeof value === 'string' ? parseIsoDate(value) : value;
  const index = (date.getDay() + 6) % 7;
  return `${WEEKDAY_SHORT[index]}, ${date.getDate()} ${MONTHS_SHORT[date.getMonth()]}`;
}

export function longToday(date: Date = new Date()): string {
  return capitalize(formatDateLong(date));
}

export function isOverdue(value: string | null | undefined): boolean {
  return Boolean(value && value < todayIso());
}

export function timeOf(value: string | null | undefined): string {
  if (!value || value.length < 16) return '';
  return value.slice(11, 16);
}

export function minutesOf(value: string | null | undefined): number | null {
  const time = timeOf(value);
  if (!time) return null;
  const [hours, minutes] = time.split(':').map(Number);
  if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return null;
  return hours * 60 + minutes;
}

/** Ближайший ровный час для нового события: 09:00–23:00, длительность — час. */
export function nextEventSlot(): { startAt: string; endAt: string } {
  const now = new Date();
  const hour = Math.min(Math.max(now.getHours() + 1, 9), 22);
  const date = toLocalIso(now);
  const pad = (value: number) => String(value).padStart(2, '0');
  return {
    startAt: `${date}T${pad(hour)}:00`,
    endAt: `${date}T${pad(hour + 1)}:00`,
  };
}

export function timeRange(startAt: string | null, endAt: string | null): string {
  const start = timeOf(startAt);
  const end = timeOf(endAt);
  if (!start) return '';
  return end ? `${start}–${end}` : start;
}

export function addIsoDays(value: string, days: number): string {
  const date = parseIsoDate(value);
  date.setDate(date.getDate() + days);
  return toLocalIso(date);
}

export function mondayOf(value: string): string {
  const date = parseIsoDate(value);
  const shift = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - shift);
  return toLocalIso(date);
}

/* ── Подписи ─────────────────────────────────────────────── */

export const RESOURCE_LABEL: Record<'must' | 'important' | 'low', string> = {
  must: 'Обязательно',
  important: 'Важно',
  low: 'Рутина',
};

export const PROJECT_STATUS_LABEL: Record<'idea' | 'active' | 'paused' | 'done', string> = {
  idea: 'Идея',
  active: 'В работе',
  paused: 'На паузе',
  done: 'Завершён',
};

export const HORIZON_LABEL: Record<'year' | 'quarter' | 'month', string> = {
  year: 'Год',
  quarter: 'Квартал',
  month: 'Месяц',
};

/* ── Цвета токенов для выбора сферы ──────────────────────── */

export function useTokenColors(): string[] {
  const [colors, setColors] = useState<string[]>([]);
  useEffect(() => {
    const styles = window.getComputedStyle(document.documentElement);
    const values = ['--cat-1', '--cat-2', '--cat-3', '--cat-4', '--cat-5']
      .map((name) => styles.getPropertyValue(name).trim())
      .filter(Boolean);
    setColors(values);
  }, []);
  return colors;
}
