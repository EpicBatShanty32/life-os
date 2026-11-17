import { useEffect, useState } from 'react';
import type { FocusEvent, MouseEvent } from 'react';
import type { DataSource, NavBadges, SyncStatus as SyncStatusModel } from '../api/types';
import type { ThemeMode } from '../lib/theme';
import { SIDEBAR_NAV, SETTINGS_ITEM } from '../nav';
import type { NavItem } from '../nav';
import { BrandMark } from './BrandMark';
import { Icon } from './Icon';
import { SyncStatus } from './SyncStatus';
import { ThemeSwitch } from './ThemeSwitch';

interface SidebarProps {
  path: string;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  badges: NavBadges | null;
  themeMode: ThemeMode;
  onThemeMode: (mode: ThemeMode) => void;
  source: DataSource;
  sync: SyncStatusModel | null;
  refreshing: boolean;
  onRefresh: () => void;
}

interface TipState {
  label: string;
  top: number;
}

function isActivePath(path: string, item: NavItem): boolean {
  if (item.path === '/') return path === '/';
  return path === item.path || path.startsWith(`${item.path}/`);
}

export function Sidebar({
  path,
  collapsed,
  onToggleCollapsed,
  badges,
  themeMode,
  onThemeMode,
  source,
  sync,
  refreshing,
  onRefresh,
}: SidebarProps) {
  const [tip, setTip] = useState<TipState | null>(null);

  useEffect(() => {
    setTip(null);
  }, [path, collapsed]);

  const showTip =
    (label: string) => (event: MouseEvent<HTMLElement> | FocusEvent<HTMLElement>) => {
      if (!collapsed) return;
      const rect = event.currentTarget.getBoundingClientRect();
      setTip({ label, top: rect.top + rect.height / 2 });
    };

  const hideTip = () => setTip(null);

  const renderLink = (item: NavItem) => {
    const active = isActivePath(path, item);
    const count = item.badge && badges ? badges[item.badge] : 0;
    const label = count > 0 ? `${item.label}, ${count}` : item.label;

    return (
      <a
        key={item.path}
        href={`#${item.path}`}
        className="sb-row"
        data-active={active ? 'true' : undefined}
        aria-current={active ? 'page' : undefined}
        aria-label={collapsed ? label : undefined}
        onMouseEnter={showTip(label)}
        onMouseLeave={hideTip}
        onFocus={showTip(label)}
        onBlur={hideTip}
        onClick={hideTip}
      >
        <span className="sb-row__icon">
          <Icon name={item.icon} size={18} />
        </span>
        <span className="sb-row__label">{item.label}</span>
        {count > 0 ? (
          <span className="sb-row__badge" aria-hidden="true">
            {count}
          </span>
        ) : null}
        {count > 0 ? <span className="sb-row__dot" aria-hidden="true" /> : null}
      </a>
    );
  };

  return (
    <aside className="sb" aria-label="Основная навигация">
      <div className="sb__head">
        <a
          className="sb__brand"
          href="#/"
          aria-label="Life OS — на главную"
          onMouseEnter={showTip('Life OS — на главную')}
          onMouseLeave={hideTip}
          onFocus={showTip('Life OS — на главную')}
          onBlur={hideTip}
          onClick={hideTip}
        >
          <BrandMark size={30} className="sb__mark" />
          <span className="sb__brand-text">
            <span className="sb__brand-title">Life OS</span>
            <span className="sb__brand-sub">личный контур</span>
          </span>
        </a>
        <button
          type="button"
          className="sb__collapse"
          onClick={onToggleCollapsed}
          aria-label={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
          aria-expanded={!collapsed}
          aria-controls="app-nav"
          title={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
          onMouseEnter={showTip(collapsed ? 'Развернуть меню' : 'Свернуть меню')}
          onMouseLeave={hideTip}
          onFocus={showTip(collapsed ? 'Развернуть меню' : 'Свернуть меню')}
          onBlur={hideTip}
        >
          <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} size={16} />
        </button>
      </div>

      <nav id="app-nav" className="sb__nav" aria-label="Разделы" onScroll={hideTip}>
        {SIDEBAR_NAV.map((node) => {
          if (node.kind === 'item') return renderLink(node.item);
          const { group } = node;
          const labelId = `sb-group-${group.id}`;
          return (
            <section className="sb-group" key={group.id} aria-labelledby={labelId}>
              <p
                className={collapsed ? 'visually-hidden' : 'sb-group__label'}
                id={labelId}
              >
                {group.title}
              </p>
              {collapsed ? <span className="sb-group__rule" aria-hidden="true" /> : null}
              {group.items.map((item) => renderLink(item))}
            </section>
          );
        })}
      </nav>

      <div className="sb__foot">
        <SyncStatus
          source={source}
          sync={sync}
          collapsed={collapsed}
          refreshing={refreshing}
          onRefresh={onRefresh}
        />
        <ThemeSwitch mode={themeMode} collapsed={collapsed} onSelect={onThemeMode} />
        {renderLink(SETTINGS_ITEM)}
      </div>

      {tip ? (
        <div className="sb-tip" role="tooltip" style={{ top: tip.top }}>
          {tip.label}
        </div>
      ) : null}
    </aside>
  );
}
