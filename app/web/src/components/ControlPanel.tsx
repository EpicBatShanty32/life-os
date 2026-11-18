import type { DataSource, SyncStatus as SyncStatusModel } from '../api/types';
import { formatRelativeShort } from '../lib/format';
import { THEME_LABELS } from '../lib/theme';
import type { ThemeMode } from '../lib/theme';
import { Icon } from './Icon';
import { ThemeSwitch } from './ThemeSwitch';

interface ControlPanelProps {
  source: DataSource;
  sync: SyncStatusModel | null;
  refreshing: boolean;
  themeMode: ThemeMode;
  onThemeMode: (mode: ThemeMode) => void;
  onRefresh: () => void;
  onOpenSearch: () => void;
  onClose: () => void;
}

/**
 * «Управление» — панель внизу сайдбара вместо россыпи иконок (DESIGN-v4 §2).
 * Строки с иконкой, названием и второй строкой-пояснением: поиск,
 * синхронизация, тема, источник данных, настройки.
 */
export function ControlPanel({
  source,
  sync,
  refreshing,
  themeMode,
  onThemeMode,
  onRefresh,
  onOpenSearch,
  onClose,
}: ControlPanelProps) {
  const isDemo = source === 'mock';
  const state = refreshing ? 'syncing' : (sync?.state ?? 'syncing');
  const tone = isDemo ? 'demo' : state;
  const stamp = sync ? formatRelativeShort(sync.at) : null;

  const syncLabel = isDemo
    ? 'Демо-данные'
    : state === 'error'
      ? 'Нет связи'
      : state === 'syncing'
        ? 'Синхронизация'
        : 'Синхронизировано';
  const syncDetail = refreshing
    ? 'Обновляю данные…'
    : isDemo
      ? stamp
        ? `Локальный пример · ${stamp}`
        : 'Локальный пример'
      : stamp
        ? `Обновить · ${stamp}`
        : 'Обновить данные';

  return (
    <div className="cp" role="dialog" aria-label="Управление" aria-modal="false">
      <div className="cp__head">
        <span className="cp__title">Управление</span>
        <button type="button" className="cp__close" onClick={onClose} aria-label="Закрыть">
          <Icon name="close" size={15} />
        </button>
      </div>

      <div className="cp__rows">
        <button
          type="button"
          className="cp__row"
          onClick={() => {
            onOpenSearch();
            onClose();
          }}
        >
          <span className="cp__ico">
            <Icon name="search" size={17} />
          </span>
          <span className="cp__body">
            <span className="cp__name">Поиск</span>
            <span className="cp__sub">Разделы и команды</span>
          </span>
          <kbd className="cp__kbd">⌘K</kbd>
        </button>

        <button
          type="button"
          className="cp__row"
          onClick={onRefresh}
          disabled={refreshing}
          data-tone={tone}
        >
          <span className="cp__ico">
            <Icon name="sync" size={17} />
          </span>
          <span className="cp__body">
            <span className="cp__name">{syncLabel}</span>
            <span className="cp__sub">{syncDetail}</span>
          </span>
          <span className="cp__state" data-tone={tone} aria-hidden="true" />
        </button>

        <div className="cp__row cp__row--static">
          <span className="cp__ico">
            <Icon
              name={themeMode === 'dark' ? 'moon' : themeMode === 'auto' ? 'auto' : 'sun'}
              size={17}
            />
          </span>
          <span className="cp__body">
            <span className="cp__name">Тема</span>
            <span className="cp__sub">{THEME_LABELS[themeMode]}</span>
          </span>
          <span className="cp__switch">
            <ThemeSwitch mode={themeMode} collapsed={false} onSelect={onThemeMode} />
          </span>
        </div>

        <div className="cp__row cp__row--static">
          <span className="cp__ico">
            <Icon name="list" size={17} />
          </span>
          <span className="cp__body">
            <span className="cp__name">Данные</span>
            <span className="cp__sub">
              {isDemo ? 'Демонстрационная база' : 'Локальная база на этом компьютере'}
            </span>
          </span>
        </div>

        <a className="cp__row" href="#/settings" onClick={onClose}>
          <span className="cp__ico">
            <Icon name="settings" size={17} />
          </span>
          <span className="cp__body">
            <span className="cp__name">Настройки</span>
            <span className="cp__sub">Оформление, подключения, данные</span>
          </span>
          <span className="cp__chev" aria-hidden="true">
            <Icon name="chevron-right" size={15} />
          </span>
        </a>
      </div>
    </div>
  );
}
