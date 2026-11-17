import { useEffect, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent, MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import type { QuickEntryInput, QuickEntryType } from '../api/types';
import { trapFocus } from '../lib/focus';
import { Icon } from './Icon';
import type { IconName } from './Icon';

interface QuickEntryDialogProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (input: QuickEntryInput) => Promise<void>;
}

const TYPES: ReadonlyArray<{ value: QuickEntryType; label: string; hint: string; icon: IconName }> = [
  { value: 'note', label: 'Мысль', hint: 'Свободная запись в журнал', icon: 'journal' },
  { value: 'metric', label: 'Метрика', hint: 'Вес, вода, сон, настроение', icon: 'chart' },
  { value: 'event', label: 'Событие', hint: 'Тренировка, встреча, поездка', icon: 'today' },
];

const MAX_LENGTH = 1000;

export function QuickEntryDialog({ open, onClose, onSubmit }: QuickEntryDialogProps) {
  const [text, setText] = useState('');
  const [type, setType] = useState<QuickEntryType>('note');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setText('');
    setType('note');
    setError(null);
    setSaving(false);
    const frame = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      previousFocus.current?.focus({ preventScroll: true });
    };
  }, [open]);

  if (!open) return null;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSubmit({ text: trimmed, type });
      onClose();
    } catch {
      setError('Не удалось сохранить запись. Попробуйте ещё раз.');
    } finally {
      setSaving(false);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === 'Tab') trapFocus(event);
  };

  const handleBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose();
  };

  return createPortal(
    <div className="dialog-overlay" onMouseDown={handleBackdrop}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="quick-entry-title"
        onKeyDown={handleKeyDown}
      >
        <form onSubmit={handleSubmit}>
          <header className="dialog__head">
            <div>
              <p className="dialog__kicker">Журнал · быстрая запись</p>
              <h2 className="dialog__title" id="quick-entry-title">
                Что записать?
              </h2>
            </div>
            <button
              type="button"
              className="icon-btn dialog__close"
              onClick={onClose}
              aria-label="Закрыть окно быстрой записи"
            >
              <Icon name="close" size={18} />
            </button>
          </header>

          <label className="visually-hidden" htmlFor="quick-entry-text">
            Текст записи
          </label>
          <textarea
            id="quick-entry-text"
            ref={textareaRef}
            className="dialog__textarea"
            value={text}
            onChange={(event) => setText(event.target.value.slice(0, MAX_LENGTH))}
            placeholder="Например: «Выпил 2 стакана воды» или «Пробежал 5 км за 32 минуты»"
            rows={4}
          />

          <div className="dialog__row">
            <span className="dialog__label" id="quick-entry-type-label">
              Тип записи
            </span>
            <div className="chip-group" role="radiogroup" aria-labelledby="quick-entry-type-label">
              {TYPES.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={type === option.value}
                  className={`chip ${type === option.value ? 'is-active' : ''}`}
                  title={option.hint}
                  onClick={() => setType(option.value)}
                >
                  <Icon name={option.icon} size={15} />
                  <span>{option.label}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="dialog__meta">
            <span>{text.length} / {MAX_LENGTH}</span>
            <span>Запись попадёт в журнал и Obsidian</span>
          </div>

          {error ? (
            <p className="dialog__error" role="alert">
              {error}
            </p>
          ) : null}

          <footer className="dialog__foot">
            <button type="button" className="btn btn--ghost" onClick={onClose}>
              Отмена
            </button>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={text.trim().length === 0 || saving}
            >
              {saving ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </footer>
        </form>
      </div>
    </div>,
    document.body,
  );
}
