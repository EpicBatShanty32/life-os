import type { DataSource } from '../api/types';
import { capitalize, formatDateLong, toLocalIso } from '../lib/format';
import type { RouteDef } from '../lib/router';
import { Icon, LogoMark } from './Icon';

interface TopbarProps {
  route: RouteDef | undefined;
  source: DataSource;
  refreshing: boolean;
  onOpenSearch: () => void;
  onQuickEntry: () => void;
}

export function Topbar({ route, source, refreshing, onOpenSearch, onQuickEntry }: TopbarProps) {
  const today = new Date();

  return (
    <header className="topbar">
      <div className="topbar__left">
        <a className="topbar__brand" href="#/" aria-label="Life OS — на главную">
          <LogoMark size={28} />
          <span>Life OS</span>
        </a>
        <div className="topbar__heading">
          <span className="topbar__crumb">Life OS</span>
          <Icon name="chevron-right" size={13} className="topbar__sep" />
          <span className="topbar__title">{route?.title ?? 'Страница не найдена'}</span>
        </div>
        <time className="topbar__date" dateTime={toLocalIso(today)}>
          {capitalize(formatDateLong(today))}
        </time>
      </div>

      <div className="topbar__actions">
        <span
          className={`source-chip source-chip--${source} ${refreshing ? 'is-refreshing' : ''}`}
          title={
            source === 'api'
              ? 'Данные приходят с /api/overview'
              : 'Сервер Ф4 ещё не подключён — показаны демонстрационные данные'
          }
        >
          <span className="source-chip__dot" aria-hidden="true" />
          {source === 'api' ? 'API' : 'Демо'}
        </span>

        <button
          type="button"
          className="search-trigger"
          onClick={onOpenSearch}
          aria-keyshortcuts="Meta+K Control+K"
          aria-label="Открыть поиск и команды, ⌘K"
        >
          <Icon name="search" size={17} />
          <span className="search-trigger__label">Поиск и команды</span>
          <kbd className="search-trigger__kbd">⌘K</kbd>
        </button>

        <button type="button" className="btn btn--primary topbar__quick" onClick={onQuickEntry}>
          <Icon name="plus" size={17} />
          <span>Быстрая запись</span>
        </button>
      </div>
    </header>
  );
}
