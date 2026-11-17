import { Icon } from './Icon';
import { THEME_LABELS } from '../lib/theme';
import type { ThemeMode } from '../lib/theme';

interface ThemeSwitchProps {
  mode: ThemeMode;
  collapsed: boolean;
  onSelect: (mode: ThemeMode) => void;
}

const OPTIONS: ReadonlyArray<{ mode: ThemeMode; icon: 'sun' | 'moon' | 'auto' }> = [
  { mode: 'light', icon: 'sun' },
  { mode: 'dark', icon: 'moon' },
  { mode: 'auto', icon: 'auto' },
];

export function ThemeSwitch({ mode, collapsed, onSelect }: ThemeSwitchProps) {
  if (collapsed) {
    const index = OPTIONS.findIndex((option) => option.mode === mode);
    const next = OPTIONS[(index + 1) % OPTIONS.length];
    const current = OPTIONS[Math.max(index, 0)];
    return (
      <button
        type="button"
        className="icon-btn theme-cycle"
        onClick={() => onSelect(next.mode)}
        aria-label={`Тема: ${THEME_LABELS[mode]}. Переключить на «${THEME_LABELS[next.mode]}»`}
        title={`Тема: ${THEME_LABELS[mode]}`}
      >
        <Icon name={current.icon} size={19} />
      </button>
    );
  }

  return (
    <div className="theme-switch" role="radiogroup" aria-label="Тема оформления">
      {OPTIONS.map((option) => (
        <button
          key={option.mode}
          type="button"
          role="radio"
          aria-checked={mode === option.mode}
          className={`theme-switch__btn ${mode === option.mode ? 'is-active' : ''}`}
          onClick={() => onSelect(option.mode)}
        >
          <Icon name={option.icon} size={16} />
          <span>{THEME_LABELS[option.mode]}</span>
        </button>
      ))}
    </div>
  );
}
