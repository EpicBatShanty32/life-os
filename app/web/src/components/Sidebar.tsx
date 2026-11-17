import { useEffect, useState } from 'react';
import type { FocusEvent, MouseEvent } from 'react';
import type { DataSource, NavBadges, SyncStatus as SyncStatusModel } from '../api/types';
import type { ThemeMode } from '../lib/theme';
import { pluralRu } from '../lib/format';
import { HOME_ITEM, NAV_GROUPS, SETTINGS_ITEM } from '../nav';
import type { NavItem } from '../nav';
import { Icon, LogoMark } from './Icon';
import { SyncStatus } from './SyncStatus';
import { ThemeSwitch } from './ThemeSwitch';

interface SidebarProps {
  path: string;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  badges: NavBadges | null;
  streakDays: number | null;
  themeMode: ThemeMode;
  onThemeMode: (mode: ThemeMode) => void;
  source: DataSource;
  sync: SyncStatusModel | null;
  refreshing: boolean;
  onRefresh: () => void;
  onQuickEntry: () => void;
}

interface TipState {
  label: string;
  top: number;
}

export function Sidebar({
  path,
  collapsed,
  onToggleCollapsed,
  badges,
  streakDays,
  themeMode,
  onThemeMode,
  source,
  sync,
  refreshing,
  onRefresh,
  onQuickEntry,
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

  const renderLink = (item: NavItem, extraClass = '') => {
    const active = path === item.path;
    const badge = item.badge && badges ? badges[item.badge] : 0;
    const ariaLabel = collapsed
      ? `${item.label}${badge > 0 ? `, ${badge}` : ''}`
      : undefined;

    return (
      <a
        key={item.path}
        href={`#${item.path}`}
        className={`nav-item ${active ? 'is-active' : ''} ${extraClass}`}
        aria-current={active ? 'page' : undefined}
        aria-label={ariaLabel}
        onMouseEnter={showTip(item.label)}
        onMouseLeave={hideTip}
        onFocus={showTip(item.label)}
        onBlur={hideTip}
        onClick={hideTip}
      >
        <span className="nav-item__icon">
          <Icon name={item.icon} size={19} />
        </span>
        <span className="nav-item__label">{item.label}</span>
        {badge > 0 ? (
          <span className="nav-item__badge" aria-hidden="true">
            {badge}
          </span>
        ) : null}
      </a>
    );
  };

  const streakText =
    streakDays === null
      ? 'Серия'
      : `${streakDays} ${pluralRu(streakDays, ['день', 'дня', 'дней'])}`;

  return (
    <aside className="sidebar" aria-label="Основная навигация">
      <div className="sidebar__head">
        <a
          className="brand"
          href="#/"
          aria-label="Life OS — на главную"
          onMouseEnter={showTip('Life OS — на главную')}
          onMouseLeave={hideTip}
          onClick={hideTip}
        >
          <LogoMark size={34} />
          <span className="brand__text">
            <span className="brand__title">Life OS</span>
            <span className="brand__sub">v3 · личный контур</span>
          </span>
        </a>
        <button
          type="button"
          className="icon-btn sidebar__collapse"
          onClick={onToggleCollapsed}
          aria-label={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
          aria-expanded={!collapsed}
          aria-controls="sidebar-nav"
          title={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
        >
          <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} size={18} />
        </button>
      </div>

      <div
        className="streak"
        title={collapsed ? `${streakText} · серия без пропусков` : undefined}
        aria-label={streakDays === null ? 'Серия' : `Серия: ${streakText}`}
      >
        <span className="streak__icon" aria-hidden="true">
          <Icon name="flame" size={18} />
          <span className="streak__count">{streakDays ?? '—'}</span>
        </span>
        <span className="streak__text">
          <strong>{streakText}</strong>
          <span>серия без пропусков</span>
        </span>
      </div>

      <button
        type="button"
        className="quick-add"
        onClick={onQuickEntry}
        aria-label="Быстрая запись"
        title={collapsed ? 'Быстрая запись' : undefined}
      >
        <span className="quick-add__icon">
          <Icon name="plus" size={18} />
        </span>
        <span className="quick-add__label">Быстрая запись</span>
      </button>

      <nav
        id="sidebar-nav"
        className="sidebar__nav"
        aria-label="Разделы Life OS"
        onScroll={hideTip}
      >
        {renderLink(HOME_ITEM, 'nav-item--home')}

        {NAV_GROUPS.map((group) => (
          <section className="nav-group" key={group.id} aria-labelledby={`nav-group-${group.id}`}>
            <h2 className="nav-group__title" id={`nav-group-${group.id}`}>
              {group.title}
            </h2>
            <div className="nav-group__rule" aria-hidden="true" />
            {group.items.map((item) => renderLink(item))}
          </section>
        ))}
      </nav>

      <div className="sidebar__foot">
        <SyncStatus
          source={source}
          sync={sync}
          collapsed={collapsed}
          refreshing={refreshing}
          onRefresh={onRefresh}
        />

        <div className="sidebar__divider" aria-hidden="true" />

        <div className="theme-block">
          {!collapsed ? <span className="sidebar__foot-label">Тема</span> : null}
          <ThemeSwitch mode={themeMode} collapsed={collapsed} onSelect={onThemeMode} />
        </div>

        {renderLink(SETTINGS_ITEM, 'nav-item--settings')}

        {!collapsed ? <p className="sidebar__version">Life OS v3 · сборка Ф5</p> : null}
      </div>

      {tip ? (
        <div className="sidebar-tip" role="tooltip" style={{ top: tip.top }} aria-hidden="true">
          {tip.label}
        </div>
      ) : null}
    </aside>
  );
}
