import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import type { NavBadges } from '../api/types';
import { trapFocus } from '../lib/focus';
import type { ThemeMode } from '../lib/theme';
import { THEME_LABELS } from '../lib/theme';
import { NAV_GROUPS, SETTINGS_ITEM, TOP_LEVEL_ITEMS } from '../nav';
import type { NavItem } from '../nav';
import { Icon } from './Icon';
import type { IconName } from './Icon';

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onNavigate: (path: string) => void;
  onQuickEntry: () => void;
  onAddWater: () => void;
  onToggleTheme: () => void;
  onToggleSidebar: () => void;
  onRefresh: () => void;
  badges: NavBadges | null;
  themeMode: ThemeMode;
}

interface PaletteEntry {
  label: string;
  hint: string;
  icon: IconName;
  keywords: string;
  badge?: number;
  action: () => void;
}

interface PaletteSection {
  id: string;
  title: string;
  entries: PaletteEntry[];
}

function normalizeQuery(value: string): string {
  return value.trim().toLocaleLowerCase('ru-RU');
}

function matches(entry: PaletteEntry, query: string): boolean {
  if (!query) return true;
  return `${entry.label} ${entry.hint} ${entry.keywords}`
    .toLocaleLowerCase('ru-RU')
    .includes(query);
}

export function CommandPalette({
  open,
  onClose,
  onNavigate,
  onQuickEntry,
  onAddWater,
  onToggleTheme,
  onToggleSidebar,
  onRefresh,
  badges,
  themeMode,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  const navEntry = (item: NavItem): PaletteEntry => ({
    label: item.label,
    hint: '',
    icon: item.icon,
    keywords: item.keywords.join(' '),
    badge: item.badge && badges ? badges[item.badge] : undefined,
    action: () => onNavigate(item.path),
  });

  const sections = useMemo<PaletteSection[]>(() => {
    const q = normalizeQuery(query);
    const common: PaletteSection = {
      id: 'common',
      title: 'Разделы',
      entries: [...TOP_LEVEL_ITEMS.map(navEntry), navEntry(SETTINGS_ITEM)],
    };
    const groups: PaletteSection[] = NAV_GROUPS.map((group) => ({
      id: group.id,
      title: group.title,
      entries: group.items.map(navEntry),
    }));
    const commands: PaletteSection = {
      id: 'commands',
      title: 'Быстрые команды',
      entries: [
        {
          label: 'Быстрая запись',
          hint: 'Заметка, метрика или событие',
          icon: 'plus',
          keywords: 'записать добавить мысль метрика событие',
          action: onQuickEntry,
        },
        {
          label: 'Добавить воду +250 мл',
          hint: 'В пульсе воды на Обзоре',
          icon: 'droplet',
          keywords: 'вода выпить стакан миллилитры',
          action: onAddWater,
        },
        {
          label: 'Сменить тему',
          hint: `Сейчас: ${THEME_LABELS[themeMode]}`,
          icon: themeMode === 'dark' ? 'moon' : 'sun',
          keywords: 'тема тёмная светлая оформление',
          action: onToggleTheme,
        },
        {
          label: 'Свернуть или развернуть меню',
          hint: 'Ширина 248 ↔ 64 px',
          icon: 'chevron-left',
          keywords: 'сайдбар меню свернуть развернуть',
          action: onToggleSidebar,
        },
        {
          label: 'Обновить данные',
          hint: 'Запросить /api/overview',
          icon: 'sync',
          keywords: 'обновить синхронизация api данные',
          action: onRefresh,
        },
      ],
    };

    return [common, ...groups, commands]
      .map((section) => ({
        ...section,
        entries: section.entries.filter((entry) => matches(entry, q)),
      }))
      .filter((section) => section.entries.length > 0);
  }, [query, badges, themeMode, onNavigate, onQuickEntry, onAddWater, onToggleTheme, onToggleSidebar, onRefresh]);

  const flat = useMemo(() => sections.flatMap((section) => section.entries), [sections]);

  useEffect(() => {
    if (!open) return undefined;
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery('');
    setActive(0);
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      previousFocus.current?.focus({ preventScroll: true });
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setActive(0);
  }, [query, open]);

  useEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    node?.scrollIntoView({ block: 'nearest' });
  }, [active, open, sections]);

  if (!open) return null;

  const runEntry = (entry: PaletteEntry) => {
    onClose();
    entry.action();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === 'Tab') {
      trapFocus(event);
      return;
    }
    if (flat.length === 0) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActive((index) => (index + 1) % flat.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActive((index) => (index - 1 + flat.length) % flat.length);
    } else if (event.key === 'Home') {
      event.preventDefault();
      setActive(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      setActive(flat.length - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const entry = flat[active];
      if (entry) runEntry(entry);
    }
  };

  const handleBackdropMouseDown = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose();
  };

  let entryIndex = -1;

  return createPortal(
    <div className="palette-overlay" onMouseDown={handleBackdropMouseDown}>
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Поиск и команды"
        onKeyDown={handleKeyDown}
      >
        <div className="palette__search">
          <span className="palette__search-icon" aria-hidden="true">
            <Icon name="search" size={19} />
          </span>
          <input
            ref={inputRef}
            className="palette__input"
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-autocomplete="list"
            aria-activedescendant={
              flat.length > 0 ? `palette-option-${Math.min(active, flat.length - 1)}` : undefined
            }
            aria-label="Поиск по разделам и командам"
            placeholder="Поиск по разделам и командам…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="palette__esc">Esc</kbd>
          <button
            type="button"
            className="icon-btn palette__close"
            onClick={onClose}
            aria-label="Закрыть поиск"
          >
            <Icon name="close" size={17} />
          </button>
        </div>

        <div className="palette__list" id="palette-list" role="listbox" aria-label="Результаты поиска" ref={listRef}>
          {sections.map((section) => (
            <div
              key={section.id}
              className="palette__group"
              role="group"
              aria-labelledby={`palette-group-${section.id}`}
            >
              <p className="palette__group-title" id={`palette-group-${section.id}`}>
                {section.title}
              </p>
              {section.entries.map((entry) => {
                entryIndex += 1;
                const index = entryIndex;
                const selected = index === active;
                return (
                  <button
                    key={`${section.id}-${entry.label}-${index}`}
                    type="button"
                    role="option"
                    id={`palette-option-${index}`}
                    data-index={index}
                    aria-selected={selected}
                    tabIndex={-1}
                    className={`palette__item ${selected ? 'is-active' : ''}`}
                    onMouseMove={() => setActive(index)}
                    onClick={() => runEntry(entry)}
                  >
                    <span className="palette__icon" aria-hidden="true">
                      <Icon name={entry.icon} size={18} />
                    </span>
                    <span className="palette__body">
                      <span className="palette__label">{entry.label}</span>
                      {entry.hint ? <span className="palette__hint">{entry.hint}</span> : null}
                    </span>
                    {entry.badge ? <span className="palette__badge">{entry.badge}</span> : null}
                    <Icon name="arrow-right" size={16} className="palette__go" />
                  </button>
                );
              })}
            </div>
          ))}

          {flat.length === 0 ? (
            <p className="palette__empty">
              Ничего не найдено. Попробуйте «вода», «сон» или «настройки».
            </p>
          ) : null}
        </div>

        <footer className="palette__foot">
          <span className="palette__hint-line">
            <kbd>↑</kbd>
            <kbd>↓</kbd> навигация
          </span>
          <span className="palette__hint-line">
            <kbd>Enter</kbd> открыть
          </span>
          <span className="palette__hint-line">
            <kbd>Esc</kbd> закрыть
          </span>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
