import { Icon } from './Icon';
import { formatRelative } from '../lib/format';
import type { DataSource, SyncStatus as SyncStatusModel } from '../api/types';

interface SyncStatusProps {
  source: DataSource;
  sync: SyncStatusModel | null;
  collapsed: boolean;
  refreshing: boolean;
  onRefresh: () => void;
}

export function SyncStatus({ source, sync, collapsed, refreshing, onRefresh }: SyncStatusProps) {
  const isDemo = source === 'mock';
  const state = refreshing ? 'syncing' : sync?.state ?? 'syncing';
  const label = isDemo
    ? 'Демо-данные'
    : state === 'error'
      ? 'Нет связи с API'
      : state === 'syncing'
        ? 'Синхронизация…'
        : 'Синхронизировано';
  const detail = isDemo
    ? 'локальный мок'
    : sync
      ? formatRelative(sync.at)
      : 'ожидание';

  return (
    <button
      type="button"
      className={`sync sync--${isDemo ? 'demo' : state}`}
      onClick={onRefresh}
      disabled={refreshing}
      aria-label={`${label}, ${detail}. Обновить данные`}
      title={collapsed ? `${label} · ${detail}` : undefined}
    >
      <span
        className={`sync__dot ${refreshing ? 'sync__dot--pulse' : ''}`}
        aria-hidden="true"
      />
      {!collapsed && (
        <>
          <span className="sync__text">
            <span className="sync__label">{label}</span>
            <span className="sync__time">{detail}</span>
          </span>
          <Icon
            name="sync"
            size={15}
            className={`sync__refresh ${refreshing ? 'is-spinning' : ''}`}
          />
        </>
      )}
    </button>
  );
}
