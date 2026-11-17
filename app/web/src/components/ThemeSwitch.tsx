import { Icon } from './Icon';
import type { IconName } from './Icon';
import { THEME_LABELS } from '../lib/theme';
import type { ThemeMode } from '../lib/theme';

interface ThemeSwitchProps {
  mode: ThemeMode;
  collapsed: boolean;
  onSelect: (mode: ThemeMode) => void;
}

interface ThemeOption {
  mode: ThemeMode;
  icon: IconName;
  /** Подпись для скринридера и тултипа: сегменты иконок идут без текста. */
  title: string;
}

const OPTIONS: readonly ThemeOption[] = [
  { mode: 'light', icon: 'sun', title: 'Светлая тема' },
  { mode: 'dark', icon: 'moon', title: 'Тёмная тема' },
  { mode: 'auto', icon: 'auto', title: 'Тема как в системе' },
];

/**
 * Тема — три иконки без подписей (DESIGN-v4 §2, HANDOFF §7.3).
 * В свёрнутом сайдбаре остаётся одна иконка, циклически меняющая режим.
 */
export function ThemeSwitch({ mode, collapsed, onSelect }: ThemeSwitchProps) {
  if (collapsed) {
    const index = OPTIONS.findIndex((option) => option.mode === mode);
    const current = OPTIONS[Math.max(index, 0)] ?? OPTIONS[0];
    const next = OPTIONS[(Math.max(index, 0) + 1) % OPTIONS.length] ?? OPTIONS[0];
    return (
      <button
        type="button"
        className="icon-btn sb-theme-cycle"
        onClick={() => onSelect(next.mode)}
        aria-label={`Тема: ${THEME_LABELS[mode]}. Переключить на «${THEME_LABELS[next.mode]}»`}
        title={`Тема: ${THEME_LABELS[mode]}`}
      >
        <Icon name={current.icon} size={16} />
      </button>
    );
  }

  return (
    <div className="sb-theme" role="radiogroup" aria-label="Тема оформления">
      {OPTIONS.map((option) => {
        const active = mode === option.mode;
        return (
          <button
            key={option.mode}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={option.title}
            title={option.title}
            tabIndex={active ? 0 : -1}
            className="sb-theme__btn"
            data-active={active ? 'true' : undefined}
            onClick={() => onSelect(option.mode)}
            onKeyDown={(event) => {
              if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
                return;
              }
              event.preventDefault();
              const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
              const index = OPTIONS.findIndex((item) => item.mode === mode);
              const target = OPTIONS[(index + step + OPTIONS.length) % OPTIONS.length];
              if (target) onSelect(target.mode);
            }}
          >
            <Icon name={option.icon} size={14} />
          </button>
        );
      })}
    </div>
  );
}
