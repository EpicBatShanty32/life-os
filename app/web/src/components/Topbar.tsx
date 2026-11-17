import type { RouteDef } from '../lib/router';
import { navGroupForPath } from '../nav';
import { BrandMark } from './BrandMark';
import { Icon } from './Icon';
import { Button } from './ui';

interface TopbarProps {
  route: RouteDef | undefined;
  onOpenSearch: () => void;
  onQuickEntry: () => void;
}

/**
 * Верхняя полоса 56 px: хлебная крошка, поиск и одна главная кнопка
 * (DESIGN-v4 §2). Дата, логотип и «источник данных» живут не здесь.
 */
export function Topbar({ route, onOpenSearch, onQuickEntry }: TopbarProps) {
  const group = route ? navGroupForPath(route.path) : undefined;
  const title = route?.title ?? 'Страница не найдена';

  return (
    <header className="tb">
      <div className="tb__lead">
        <a className="tb__home" href="#/" aria-label="Life OS — на главную">
          <BrandMark size={28} className="tb__mark" />
        </a>
        <nav className="tb__crumb" aria-label="Хлебная крошка">
          <a className="tb__crumb-link" href="#/">
            Life OS
          </a>
          <Icon name="chevron-right" size={12} className="tb__crumb-sep" />
          {group ? (
            <>
              <span className="tb__crumb-group">{group.title}</span>
              <Icon name="chevron-right" size={12} className="tb__crumb-sep" />
            </>
          ) : null}
          <span className="tb__crumb-current" aria-current="page">
            {title}
          </span>
        </nav>
      </div>

      <div className="tb__actions">
        <button
          type="button"
          className="tb__search"
          onClick={onOpenSearch}
          aria-keyshortcuts="Meta+K Control+K"
          aria-label="Поиск по разделам и командам, ⌘K"
          title="Поиск и команды, ⌘K"
        >
          <Icon name="search" size={16} />
          <span className="tb__search-label">Поиск</span>
          <kbd className="tb__kbd">⌘K</kbd>
        </button>

        <Button
          variant="primary"
          size="sm"
          leadingIcon="plus"
          className="tb__quick"
          onClick={onQuickEntry}
        >
          Быстрая запись
        </Button>
      </div>
    </header>
  );
}
