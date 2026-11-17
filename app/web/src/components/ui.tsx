import {
  Fragment,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  InputHTMLAttributes,
  KeyboardEvent as ReactKeyboardEvent,
  ReactElement,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './Icon';
import type { IconName } from './Icon';

/* ─────────────────────────────────────────────────────────────
   Life OS v4 — примитивы интерфейса (DESIGN-v4 §3).
   Только токены и var(--…); никаких hex и «магических» цветов.
   ───────────────────────────────────────────────────────────── */

function cx(...values: ReadonlyArray<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ');
}

/* ── Card ────────────────────────────────────────────────── */

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** 20 px по умолчанию, 12 px — плотные списки, none — таблицы внутри. */
  padding?: 'none' | 'sm' | 'md';
}

function CardBase({ padding = 'md', className, children, ...rest }: CardProps) {
  return (
    <div
      className={cx('ui-card', padding !== 'md' && `ui-card--pad-${padding}`, className)}
      {...rest}
    >
      {children}
    </div>
  );
}

export interface CardTitleProps {
  children: ReactNode;
  /** Уровень заголовка: карточка верхнего уровня — h2, вложенная — h3. */
  as?: 'h2' | 'h3' | 'h4' | 'p';
  className?: string;
}

export function CardTitle({ children, as = 'h2', className }: CardTitleProps) {
  const Heading = as;
  return <Heading className={cx('ui-card__title', className)}>{children}</Heading>;
}

export interface CardActionsProps {
  children: ReactNode;
  className?: string;
}

export function CardActions({ children, className }: CardActionsProps) {
  return <div className={cx('ui-card__actions', className)}>{children}</div>;
}

export interface CardHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  titleAs?: 'h2' | 'h3' | 'h4' | 'p';
  className?: string;
}

export function CardHeader({
  title,
  subtitle,
  actions,
  titleAs = 'h2',
  className,
}: CardHeaderProps) {
  return (
    <div className={cx('ui-card__head', className)}>
      <div className="ui-card__heading">
        <CardTitle as={titleAs}>{title}</CardTitle>
        {subtitle ? <p className="ui-card__subtitle">{subtitle}</p> : null}
      </div>
      {actions ? <CardActions>{actions}</CardActions> : null}
    </div>
  );
}

export interface CardBodyProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

export function CardBody({ className, children, ...rest }: CardBodyProps) {
  return (
    <div className={cx('ui-card__body', className)} {...rest}>
      {children}
    </div>
  );
}

export interface CardFooterProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

export function CardFooter({ className, children, ...rest }: CardFooterProps) {
  return (
    <div className={cx('ui-card__foot', className)} {...rest}>
      {children}
    </div>
  );
}

export const Card = Object.assign(CardBase, {
  Header: CardHeader,
  Title: CardTitle,
  Actions: CardActions,
  Body: CardBody,
  Footer: CardFooter,
});

/* ── Section ─────────────────────────────────────────────── */

export interface SectionProps {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
  titleAs?: 'h2' | 'h3';
}

export function Section({
  title,
  description,
  actions,
  children,
  className,
  id,
  titleAs = 'h2',
}: SectionProps) {
  const generatedId = useId();
  const baseId = id ?? `section-${generatedId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const titleId = `${baseId}-title`;
  const Heading = titleAs;

  return (
    <section className={cx('ui-section', className)} id={baseId} aria-labelledby={titleId}>
      <header className="ui-section__head">
        <div className="ui-section__heading">
          <Heading className="ui-section__title" id={titleId}>
            {title}
          </Heading>
          {description ? <p className="ui-section__description">{description}</p> : null}
        </div>
        {actions ? <div className="ui-section__actions">{actions}</div> : null}
      </header>
      <div className="ui-section__body">{children}</div>
    </section>
  );
}

/* ── Segmented ───────────────────────────────────────────── */

export interface SegmentedOption<T extends string = string> {
  value: T;
  label: ReactNode;
  icon?: IconName;
  title?: string;
  disabled?: boolean;
}

export interface SegmentedProps<T extends string = string> {
  options: ReadonlyArray<SegmentedOption<T>>;
  value: T;
  onChange: (value: T) => void;
  /** Обязательная подпись группы для скринридера. */
  ariaLabel: string;
  block?: boolean;
  className?: string;
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  block = false,
  className,
}: SegmentedProps<T>) {
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const step = (from: number, direction: 1 | -1): number => {
    let index = from;
    for (let attempt = 0; attempt < options.length; attempt += 1) {
      index = (index + direction + options.length) % options.length;
      if (!options[index]?.disabled) return index;
    }
    return from;
  };

  const edge = (direction: 'first' | 'last'): number => {
    if (direction === 'first') {
      return options.findIndex((option) => !option.disabled);
    }
    for (let index = options.length - 1; index >= 0; index -= 1) {
      if (!options[index]?.disabled) return index;
    }
    return 0;
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = -1;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      next = step(index, 1);
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      next = step(index, -1);
    } else if (event.key === 'Home') {
      next = edge('first');
    } else if (event.key === 'End') {
      next = edge('last');
    }

    if (next < 0) return;
    event.preventDefault();
    const option = options[next];
    if (!option) return;
    onChange(option.value);
    buttonRefs.current[next]?.focus();
  };

  return (
    <div
      className={cx('ui-segmented', block && 'ui-segmented--block', className)}
      role="group"
      aria-label={ariaLabel}
    >
      {options.map((option, index) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            ref={(node) => {
              buttonRefs.current[index] = node;
            }}
            type="button"
            className="ui-segmented__item"
            aria-pressed={active}
            title={option.title}
            disabled={option.disabled}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {option.icon ? <Icon name={option.icon} size={15} /> : null}
            <span>{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ── Button ──────────────────────────────────────────────── */

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  children?: ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
  leadingIcon?: IconName;
  trailingIcon?: IconName;
  loading?: boolean;
  fullWidth?: boolean;
}

export function Button({
  children,
  variant = 'secondary',
  size = 'md',
  leadingIcon,
  trailingIcon,
  loading = false,
  fullWidth = false,
  className,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  const iconSize = size === 'sm' ? 14 : 16;
  const showLeading = Boolean(leadingIcon) || loading;

  return (
    <button
      type={type}
      className={cx(
        'ui-btn',
        `ui-btn--${variant}`,
        size === 'sm' && 'ui-btn--sm',
        fullWidth && 'ui-btn--full',
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {showLeading ? (
        <Icon
          name={loading ? 'sync' : (leadingIcon as IconName)}
          size={iconSize}
          className={loading ? 'is-spinning' : undefined}
        />
      ) : null}
      {children !== undefined && children !== null ? (
        <span className="ui-btn__label">{children}</span>
      ) : null}
      {trailingIcon ? <Icon name={trailingIcon} size={iconSize} /> : null}
    </button>
  );
}

/* ── Field ───────────────────────────────────────────────── */

export interface FieldProps {
  label: string;
  htmlFor?: string;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  className?: string;
  children: ReactNode;
}

interface FieldControlProps {
  id?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
}

export function Field({
  label,
  htmlFor,
  hint,
  error,
  required = false,
  className,
  children,
}: FieldProps) {
  const generatedId = useId();
  const hintId = `${generatedId}-hint`;
  const errorId = `${generatedId}-error`;
  const controlId = htmlFor ?? `${generatedId}-control`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(' ');

  let control = children;
  if (isValidElement(children) && typeof children.type === 'string') {
    const element = children as ReactElement<FieldControlProps>;
    const existing = element.props['aria-describedby'];
    control = cloneElement(element, {
      id: element.props.id ?? controlId,
      'aria-describedby': [existing, describedBy].filter(Boolean).join(' ') || undefined,
      'aria-invalid': error ? true : element.props['aria-invalid'],
    });
  }

  return (
    <div className={cx('ui-field', className)}>
      <label className="ui-field__label" htmlFor={controlId}>
        {label}
        {required ? (
          <span className="ui-field__required" aria-hidden="true">
            *
          </span>
        ) : null}
      </label>
      {control}
      {hint && !error ? (
        <span className="ui-field__hint" id={hintId}>
          {hint}
        </span>
      ) : null}
      {error ? (
        <span className="ui-field__error" id={errorId}>
          {error}
        </span>
      ) : null}
    </div>
  );
}

/* ── ProgressBar ─────────────────────────────────────────── */

export type ProgressTone =
  | 'accent'
  | 'cat-1'
  | 'cat-2'
  | 'cat-3'
  | 'cat-4'
  | 'cat-5'
  | 'success'
  | 'warning'
  | 'danger';

export interface ProgressBarProps {
  value: number;
  max?: number;
  tone?: ProgressTone;
  label?: ReactNode;
  /** Подпись справа; по умолчанию — процент при showValue. */
  valueLabel?: ReactNode;
  showValue?: boolean;
  /** Подпись для скринридера, если label не строка. */
  ariaLabel?: string;
  className?: string;
}

export function ProgressBar({
  value,
  max = 100,
  tone = 'cat-1',
  label,
  valueLabel,
  showValue = false,
  ariaLabel,
  className,
}: ProgressBarProps) {
  const safeMax = max > 0 ? max : 1;
  const percent = Math.max(0, Math.min(100, (value / safeMax) * 100));
  const rounded = Math.round(percent);
  const rightLabel =
    valueLabel !== undefined ? valueLabel : showValue ? `${rounded}%` : undefined;
  const accessibleName =
    ariaLabel ?? (typeof label === 'string' ? label : 'Прогресс');

  return (
    <div
      className={cx('ui-progress', className)}
      data-tone={tone}
      role="progressbar"
      aria-label={accessibleName}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={rounded}
      aria-valuetext={`${rounded}%`}
    >
      {label !== undefined || rightLabel !== undefined ? (
        <div className="ui-progress__meta">
          {label !== undefined ? <span className="ui-progress__label">{label}</span> : <span />}
          {rightLabel !== undefined ? (
            <span className="ui-progress__value">{rightLabel}</span>
          ) : null}
        </div>
      ) : null}
      <span className="ui-progress__track">
        <span className="ui-progress__fill" style={{ width: `${percent}%` }} />
      </span>
    </div>
  );
}

/* ── EmptyState ──────────────────────────────────────────── */

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cx('ui-empty', className)}>
      <p className="ui-empty__title">{title}</p>
      {description ? <p className="ui-empty__text">{description}</p> : null}
      {action ? <div className="ui-empty__action">{action}</div> : null}
    </div>
  );
}

/* ── Sheet ───────────────────────────────────────────────── */

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: 'default' | 'wide';
  className?: string;
}

export function Sheet({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'default',
  className,
}: SheetProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;

    const panel = panelRef.current;
    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const focusFrame = window.requestAnimationFrame(() => {
      panelRef.current?.focus();
    });

    const onKeyDown = (event: KeyboardEvent) => {
      // Меню действий живёт в портале вне панели: Esc/Tab обрабатывает оно.
      if (event.target instanceof Element && event.target.closest('[role="menu"]')) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !panel) return;

      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (node) => node.offsetWidth > 0 || node.offsetHeight > 0 || node === document.activeElement,
      );
      if (nodes.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }

      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (!first || !last) return;
      const active = document.activeElement;
      const inside = panel.contains(active);

      if (event.shiftKey) {
        if (!inside || active === first || active === panel) {
          event.preventDefault();
          last.focus();
        }
        return;
      }

      if (!inside || active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);

    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = previousOverflow;
      restoreFocusRef.current?.focus();
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      className="ui-sheet-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        className={cx('ui-sheet', size === 'wide' && 'ui-sheet--wide', className)}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
      >
        <header className="ui-sheet__head">
          <div className="ui-sheet__heading">
            <h2 className="ui-sheet__title" id={titleId}>
              {title}
            </h2>
            {description ? (
              <p className="ui-sheet__description" id={descriptionId}>
                {description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            className="ui-sheet__close"
            onClick={onClose}
            aria-label="Закрыть панель"
            title="Закрыть, Esc"
          >
            <Icon name="close" size={18} />
          </button>
        </header>
        <div className="ui-sheet__body">{children}</div>
        {footer ? <footer className="ui-sheet__foot">{footer}</footer> : null}
      </div>
    </div>,
    document.body,
  );
}

/* ── Table ───────────────────────────────────────────────── */

export interface TableColumn<T> {
  key: string;
  header: ReactNode;
  cell: (row: T, index: number) => ReactNode;
  numeric?: boolean;
  width?: string;
  className?: string;
}

export interface TableProps<T> {
  columns: ReadonlyArray<TableColumn<T>>;
  rows: readonly T[];
  rowKey: (row: T, index: number) => string;
  /** Пустое состояние обязательно: это подсказка, а не «нет данных». */
  empty: ReactNode;
  caption?: string;
  ariaLabel?: string;
  className?: string;
  onRowClick?: (row: T) => void;
  rowActionLabel?: (row: T) => string;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  empty,
  caption,
  ariaLabel,
  className,
  onRowClick,
  rowActionLabel,
}: TableProps<T>) {
  const hasRows = rows.length > 0;

  return (
    <div className={cx('ui-table-wrap', className)}>
      <table
        className="ui-table"
        aria-label={caption ? undefined : (ariaLabel ?? 'Таблица')}
      >
        <caption className="visually-hidden">{caption ?? ariaLabel ?? 'Таблица'}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cx(column.numeric && 'is-numeric', column.className)}
                style={column.width ? { width: column.width } : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {hasRows ? (
            rows.map((row, index) => {
              const key = rowKey(row, index);
              const interactive = Boolean(onRowClick);
              return (
                <tr
                  key={key}
                  className={interactive ? 'is-clickable' : undefined}
                  tabIndex={interactive ? 0 : undefined}
                  aria-label={interactive ? rowActionLabel?.(row) : undefined}
                  onClick={interactive ? () => onRowClick?.(row) : undefined}
                  onKeyDown={
                    interactive
                      ? (event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            onRowClick?.(row);
                          }
                        }
                      : undefined
                  }
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={cx(column.numeric && 'is-numeric', column.className)}
                    >
                      {column.cell(row, index)}
                    </td>
                  ))}
                </tr>
              );
            })
          ) : (
            <tr>
              <td className="ui-table__empty-cell" colSpan={columns.length}>
                {empty}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/* ── Chip ────────────────────────────────────────────────── */

export type ChipTone =
  | 'accent'
  | 'cat-1'
  | 'cat-2'
  | 'cat-3'
  | 'cat-4'
  | 'cat-5'
  | 'success'
  | 'warning'
  | 'danger';

export interface ChipProps {
  children: ReactNode;
  tone?: ChipTone;
  showDot?: boolean;
  active?: boolean;
  onClick?: () => void;
  title?: string;
  className?: string;
}

export function Chip({
  children,
  tone,
  showDot,
  active,
  onClick,
  title,
  className,
}: ChipProps) {
  const withDot = showDot ?? Boolean(tone);
  const content = (
    <>
      {withDot ? <span className="ui-chip__dot" aria-hidden="true" /> : null}
      <span className="ui-chip__label">{children}</span>
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        className={cx('ui-chip', className)}
        data-tone={tone}
        title={title}
        aria-pressed={active}
        onClick={onClick}
      >
        {content}
      </button>
    );
  }

  return (
    <span className={cx('ui-chip', className)} data-tone={tone} title={title}>
      {content}
    </span>
  );
}

/* ── Kpi ─────────────────────────────────────────────────── */

export interface KpiDelta {
  value: string;
  direction?: 'up' | 'down' | 'flat';
  tone?: 'good' | 'bad' | 'neutral';
  /** Пояснение для title, например «к прошлой неделе». */
  label?: string;
}

export interface KpiProps {
  label: string;
  value: ReactNode;
  unit?: string;
  delta?: KpiDelta;
  hint?: ReactNode;
  className?: string;
}

export function Kpi({ label, value, unit, delta, hint, className }: KpiProps) {
  const direction = delta?.direction ?? 'flat';
  return (
    <div className={cx('ui-kpi', className)}>
      <span className="ui-kpi__label">{label}</span>
      <p className="ui-kpi__value">
        <span className="ui-kpi__number">{value}</span>
        {unit ? <span className="ui-kpi__unit">{unit}</span> : null}
      </p>
      {delta ? (
        <span
          className="ui-kpi__delta"
          data-tone={delta.tone ?? 'neutral'}
          title={delta.label}
        >
          {direction !== 'flat' ? (
            <Icon name={direction === 'up' ? 'trend-up' : 'trend-down'} size={13} />
          ) : null}
          <span>{delta.value}</span>
        </span>
      ) : null}
      {hint ? <p className="ui-kpi__hint">{hint}</p> : null}
    </div>
  );
}

/* ── Поля: нативные обёртки поверх .ui-* ─────────────────── */

export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> & {
  size?: 'sm' | 'md';
};

export function Input({ className, size = 'md', type = 'text', ...rest }: InputProps) {
  return (
    <input
      type={type}
      className={cx('ui-input', size === 'sm' && 'ui-input--sm', className)}
      {...rest}
    />
  );
}

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

export function Textarea({ className, rows = 3, ...rest }: TextareaProps) {
  return <textarea rows={rows} className={cx('ui-textarea', className)} {...rest} />;
}

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size' | 'children'> {
  options: ReadonlyArray<SelectOption>;
  placeholder?: string;
  size?: 'sm' | 'md';
}

export function Select({
  options,
  placeholder,
  size = 'md',
  className,
  ...rest
}: SelectProps) {
  return (
    <select
      className={cx('ui-select', size === 'sm' && 'ui-select--sm', className)}
      {...rest}
    >
      {placeholder ? (
        <option value="" disabled>
          {placeholder}
        </option>
      ) : null}
      {options.map((option) => (
        <option key={option.value} value={option.value} disabled={option.disabled}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export type DateInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'size'> & {
  size?: 'sm' | 'md';
};

export function DateInput({ className, size = 'md', ...rest }: DateInputProps) {
  return (
    <input
      type="date"
      className={cx('ui-input ui-input--date', size === 'sm' && 'ui-input--sm', className)}
      {...rest}
    />
  );
}

/* ── Checkbox ────────────────────────────────────────────── */

export interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'size'> {
  label: ReactNode;
  description?: ReactNode;
  indeterminate?: boolean;
  size?: 'sm' | 'md';
  className?: string;
}

export function Checkbox({
  label,
  description,
  indeterminate = false,
  size = 'md',
  className,
  disabled,
  ...rest
}: CheckboxProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const descriptionId = useId();

  useEffect(() => {
    if (inputRef.current) inputRef.current.indeterminate = indeterminate;
  }, [indeterminate]);

  return (
    <label
      className={cx('ui-checkbox', size === 'sm' && 'ui-checkbox--sm', className)}
      data-indeterminate={indeterminate ? 'true' : undefined}
    >
      <input
        ref={inputRef}
        type="checkbox"
        className="ui-checkbox__input"
        disabled={disabled}
        aria-describedby={description ? descriptionId : undefined}
        {...rest}
      />
      <span className="ui-checkbox__box" aria-hidden="true">
        <Icon name="check" size={size === 'sm' ? 10 : 11} className="ui-checkbox__check" />
        <span className="ui-checkbox__dash" />
      </span>
      <span className="ui-checkbox__text">
        <span className="ui-checkbox__label">{label}</span>
        {description ? (
          <span className="ui-checkbox__description" id={descriptionId}>
            {description}
          </span>
        ) : null}
      </span>
    </label>
  );
}

/* ── Skeleton ────────────────────────────────────────────── */

export interface SkeletonProps {
  variant?: 'text' | 'title' | 'block' | 'row';
  /** Для variant="text": 1–3 строки. */
  lines?: 1 | 2 | 3;
  label?: string;
  className?: string;
}

export function Skeleton({
  variant = 'text',
  lines = 3,
  label = 'Загрузка',
  className,
}: SkeletonProps) {
  if (variant !== 'text') {
    return (
      <div
        className={cx('ui-skeleton', `ui-skeleton--${variant}`, className)}
        role="status"
        aria-label={label}
      >
        <span className="ui-skeleton__shape" />
      </div>
    );
  }

  const count = Math.min(3, Math.max(1, lines));
  return (
    <div
      className={cx('ui-skeleton', 'ui-skeleton--text', className)}
      role="status"
      aria-label={label}
    >
      {Array.from({ length: count }, (_, index) => (
        <span key={index} className="ui-skeleton__line" />
      ))}
    </div>
  );
}

/* ── Menu: действия строки (WAI-ARIA menu) ───────────────── */

const MENU_WIDTH = 208;

export interface MenuAction {
  id: string;
  label: string;
  icon?: IconName;
  shortcut?: string;
  danger?: boolean;
  disabled?: boolean;
  /** Разделитель перед пунктом. */
  separatorBefore?: boolean;
  onSelect: () => void;
}

export interface MenuProps {
  items: ReadonlyArray<MenuAction>;
  /** Обязательная подпись кнопки-триггера. */
  label: string;
  trigger?: ReactNode;
  align?: 'start' | 'end';
  className?: string;
  triggerClassName?: string;
}

export function Menu({
  items,
  label,
  trigger,
  align = 'end',
  className,
  triggerClassName,
}: MenuProps) {
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);

  const enabledIndexes = items.flatMap((item, index) => (item.disabled ? [] : [index]));

  const openAt = (edge: 'first' | 'last') => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const estimatedHeight = Math.min(items.length * 32 + 12, 320);
    const top =
      rect.bottom + 4 + estimatedHeight > window.innerHeight - 8
        ? Math.max(8, rect.top - estimatedHeight - 4)
        : rect.bottom + 4;
    const rawLeft = align === 'end' ? rect.right - MENU_WIDTH : rect.left;
    const left = Math.max(8, Math.min(rawLeft, window.innerWidth - MENU_WIDTH - 8));
    const target = edge === 'first' ? enabledIndexes[0] : enabledIndexes[enabledIndexes.length - 1];
    setPosition({ top, left });
    setActiveIndex(target ?? 0);
    setOpen(true);
  };

  const closeAndFocusTrigger = () => {
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const moveFocus = (direction: 1 | -1) => {
    if (enabledIndexes.length === 0) return;
    const currentPosition = enabledIndexes.indexOf(activeIndex);
    const nextPosition =
      currentPosition < 0
        ? 0
        : (currentPosition + direction + enabledIndexes.length) % enabledIndexes.length;
    const next = enabledIndexes[nextPosition];
    if (next === undefined) return;
    setActiveIndex(next);
    itemRefs.current[next]?.focus();
  };

  const focusEdge = (edge: 'first' | 'last') => {
    const next = edge === 'first' ? enabledIndexes[0] : enabledIndexes[enabledIndexes.length - 1];
    if (next === undefined) return;
    setActiveIndex(next);
    itemRefs.current[next]?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      itemRefs.current[activeIndex]?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open, activeIndex]);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onViewportChange = () => setOpen(false);

    document.addEventListener('pointerdown', onPointerDown, true);
    window.addEventListener('resize', onViewportChange);
    window.addEventListener('scroll', onViewportChange, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      window.removeEventListener('resize', onViewportChange);
      window.removeEventListener('scroll', onViewportChange, true);
    };
  }, [open]);

  const onMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeAndFocusTrigger();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveFocus(1);
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveFocus(-1);
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      focusEdge('first');
      return;
    }
    if (event.key === 'End') {
      event.preventDefault();
      focusEdge('last');
      return;
    }
    if (event.key === 'Tab') {
      setOpen(false);
    }
  };

  const onTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (open) moveFocus(1);
      else openAt('first');
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (open) moveFocus(-1);
      else openAt('last');
    }
  };

  return (
    <div ref={rootRef} className={cx('ui-menu', className)}>
      <button
        ref={triggerRef}
        type="button"
        className={cx('ui-menu__trigger', open && 'is-open', triggerClassName)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={label}
        title={label}
        onClick={() => {
          if (open) setOpen(false);
          else openAt('first');
        }}
        onKeyDown={onTriggerKeyDown}
      >
        {trigger ?? <Icon name="more" size={18} />}
      </button>

      {open && position
        ? createPortal(
            <div
              ref={menuRef}
              id={menuId}
              role="menu"
              aria-label={label}
              className="ui-menu__popover"
              style={{ top: position.top, left: position.left }}
              onKeyDown={onMenuKeyDown}
            >
              {items.map((item, index) => (
                <Fragment key={item.id}>
                  {item.separatorBefore ? (
                    <div className="ui-menu__separator" role="separator" />
                  ) : null}
                  <button
                    ref={(node) => {
                      itemRefs.current[index] = node;
                    }}
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    className={cx('ui-menu__item', item.danger && 'ui-menu__item--danger')}
                    disabled={item.disabled}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => {
                      triggerRef.current?.focus();
                      setOpen(false);
                      item.onSelect();
                    }}
                  >
                    {item.icon ? <Icon name={item.icon} size={16} /> : null}
                    <span className="ui-menu__label">{item.label}</span>
                    {item.shortcut ? (
                      <span className="ui-menu__shortcut">{item.shortcut}</span>
                    ) : null}
                  </button>
                </Fragment>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
