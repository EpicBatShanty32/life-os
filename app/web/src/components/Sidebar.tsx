import { useEffect, useRef, useState } from 'react';
import type { FocusEvent, MouseEvent } from 'react';
import type { DataSource, NavBadges, SyncStatus as SyncStatusModel } from '../api/types';
import type { ThemeMode } from '../lib/theme';
import { SIDEBAR_NAV } from '../nav';
import type { NavItem } from '../nav';
import { BrandMark } from './BrandMark';
import { Icon } from './Icon';
import { ControlPanel } from './ControlPanel';

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
  onOpenSearch: () => void;
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
  onOpenSearch,
}: SidebarProps) {
  const [tip, setTip] = useState<TipState | null>(null);
  const [manageOpen, setManageOpen] = useState(false);
  const manageRef = useRef<HTMLDivElement>(null);
  const manageBtnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setTip(null);
    setManageOpen(false);
  }, [path, collapsed]);

  useEffect(() => {
    if (!manageOpen) return;
    const onDown = (event: globalThis.MouseEvent) => {
      const target = event.target as Node | null;
      if (target && manageRef.current?.contains(target)) return;
      if (target && manageBtnRef.current?.contains(target)) return;
      setManageOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setManageOpen(false);
      manageBtnRef.current?.focus();
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [manageOpen]);

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

      <div className="sb__foot" ref={manageRef}>
        <button
          ref={manageBtnRef}
          type="button"
          className="sb__manage"
          aria-expanded={manageOpen}
          aria-haspopup="dialog"
          aria-label={collapsed ? 'Управление' : undefined}
          title={collapsed ? 'Управление' : undefined}
          onMouseEnter={collapsed ? showTip('Управление') : undefined}
          onMouseLeave={collapsed ? hideTip : undefined}
          onFocus={collapsed ? showTip('Управление') : undefined}
          onBlur={collapsed ? hideTip : undefined}
          onClick={() => {
            hideTip();
            setManageOpen((open) => !open);
          }}
        >
          <span className="sb-row__icon">
            <Icon name="sliders" size={18} />
          </span>
          <span className="sb__manage-label">Управление</span>
        </button>

        {manageOpen ? (
          <ControlPanel
            source={source}
            sync={sync}
            refreshing={refreshing}
            themeMode={themeMode}
            onThemeMode={onThemeMode}
            onRefresh={onRefresh}
            onOpenSearch={onOpenSearch}
            onClose={() => setManageOpen(false)}
          />
        ) : null}
      </div>

      {tip ? (
        <div className="sb-tip" role="tooltip" style={{ top: tip.top }}>
          {tip.label}
        </div>
      ) : null}
    </aside>
  );
}
