import { useEffect, useState } from 'react';
import type { NavBadges } from '../api/types';
import type { ThemeMode } from '../lib/theme';
import { MOBILE_NAV, SIDEBAR_NAV, SETTINGS_ITEM } from '../nav';
import type { NavItem } from '../nav';
import { Icon } from './Icon';
import { ThemeSwitch } from './ThemeSwitch';
import { Sheet } from './ui';

interface MobileNavProps {
  path: string;
  badges: NavBadges | null;
  themeMode: ThemeMode;
  onThemeMode: (mode: ThemeMode) => void;
}

function itemBadge(item: NavItem, badges: NavBadges | null): number {
  return item.badge && badges ? badges[item.badge] : 0;
}

/**
 * Мобильная навигация: пять разделов и «Ещё» со шторкой, где лежат
 * остальные группы (HANDOFF §9). Тема тоже переезжает в шторку.
 */
export function MobileNav({ path, badges, themeMode, onThemeMode }: MobileNavProps) {
  const [moreOpen, setMoreOpen] = useState(false);

  useEffect(() => {
    setMoreOpen(false);
  }, [path]);

  const renderSheetRow = (item: NavItem) => {
    const active = item.path === '/' ? path === '/' : path === item.path;
    const count = itemBadge(item, badges);
    return (
      <a
        key={item.path}
        href={`#${item.path}`}
        className="more-row"
        data-active={active ? 'true' : undefined}
        aria-current={active ? 'page' : undefined}
        onClick={() => setMoreOpen(false)}
      >
        <span className="more-row__icon">
          <Icon name={item.icon} size={18} />
        </span>
        <span className="more-row__label">{item.label}</span>
        {count > 0 ? (
          <span className="more-row__badge" aria-hidden="true">
            {count}
          </span>
        ) : null}
      </a>
    );
  };

  return (
    <>
      <nav className="mb-nav" aria-label="Основные разделы">
        {MOBILE_NAV.map((item) => {
          const active = item.path === '/' ? path === '/' : path === item.path;
          const count = itemBadge(item, badges);
          return (
            <a
              key={item.path}
              href={`#${item.path}`}
              className="mb-nav__item"
              data-active={active ? 'true' : undefined}
              aria-current={active ? 'page' : undefined}
              aria-label={count > 0 ? `${item.label}, ${count}` : item.label}
            >
              <span className="mb-nav__icon">
                <Icon name={item.icon} size={20} />
                {count > 0 ? <span className="mb-nav__dot" aria-hidden="true" /> : null}
              </span>
              <span className="mb-nav__label">{item.label}</span>
            </a>
          );
        })}
        <button
          type="button"
          className="mb-nav__item"
          aria-haspopup="dialog"
          aria-expanded={moreOpen}
          onClick={() => setMoreOpen(true)}
        >
          <span className="mb-nav__icon">
            <Icon name="more" size={20} />
          </span>
          <span className="mb-nav__label">Ещё</span>
        </button>
      </nav>

      <Sheet
        open={moreOpen}
        onClose={() => setMoreOpen(false)}
        title="Все разделы"
        description="Полный список экранов Life OS"
        footer={
          <div className="more-theme">
            <span className="more-theme__label">Тема</span>
            <ThemeSwitch mode={themeMode} collapsed={false} onSelect={onThemeMode} />
          </div>
        }
      >
        <div className="more-list">
          {SIDEBAR_NAV.map((node) => {
            if (node.kind === 'item') return renderSheetRow(node.item);
            return (
              <section className="more-group" key={node.group.id} aria-labelledby={`more-${node.group.id}`}>
                <p className="more-group__label" id={`more-${node.group.id}`}>
                  {node.group.title}
                </p>
                {node.group.items.map((item) => renderSheetRow(item))}
              </section>
            );
          })}
          <section className="more-group" aria-labelledby="more-settings">
            <p className="more-group__label" id="more-settings">
              Приложение
            </p>
            {renderSheetRow(SETTINGS_ITEM)}
          </section>
        </div>
      </Sheet>
    </>
  );
}
