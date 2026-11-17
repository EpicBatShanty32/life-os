import { formatRelativeShort } from '../lib/format';
import type { DataSource, SyncStatus as SyncStatusModel } from '../api/types';

interface SyncStatusProps {
  source: DataSource;
  sync: SyncStatusModel | null;
  collapsed: boolean;
  refreshing: boolean;
  onRefresh: () => void;
}

/**
 * Тихий статус синхронизации (DESIGN-v4 §2): точка 6 px и одна строка
 * 12 px без рамки и фона. Ошибка — цветом и тултипом, не плашкой.
 */
export function SyncStatus({ source, sync, collapsed, refreshing, onRefresh }: SyncStatusProps) {
  const isDemo = source === 'mock';
  const state = refreshing ? 'syncing' : (sync?.state ?? 'syncing');
  const label = isDemo
    ? 'Демо-данные'
    : state === 'error'
      ? 'Нет связи'
      : state === 'syncing'
        ? 'Синхронизация'
        : 'Синхронизировано';
  const detail = isDemo ? 'локальный пример' : sync ? formatRelativeShort(sync.at) : 'ожидание';
  const tone = isDemo ? 'demo' : state;
  const title = `${label} · ${detail}`;

  return (
    <button
      type="button"
      className={collapsed ? 'sb-sync sb-sync--dot' : 'sb-sync'}
      data-tone={tone}
      onClick={onRefresh}
      disabled={refreshing}
      aria-label={`${label}, ${detail}. Обновить данные`}
      title={title}
    >
      <span className="sb-sync__dot" aria-hidden="true" />
      {collapsed ? null : (
        <span className="sb-sync__label">
          {label}
          <span className="sb-sync__sep" aria-hidden="true">
            {' · '}
          </span>
          <span className="sb-sync__time">{detail}</span>
        </span>
      )}
    </button>
  );
}
