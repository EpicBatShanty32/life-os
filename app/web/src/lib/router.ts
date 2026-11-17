/** Свой хеш-роутер: без react-router и других зависимостей. */

import { useEffect, useState } from 'react';

export interface RouteDef {
  path: string;
  title: string;
  /** Короткое пояснение для пустых разделов и поиска. */
  note?: string;
}

export const ROUTES: readonly RouteDef[] = [
  { path: '/', title: 'Обзор', note: 'Пульс дня и ключевые метрики' },
  { path: '/today', title: 'Сегодня', note: 'Задачи, привычки и план дня' },
  { path: '/goals', title: 'Цели', note: 'Цели на месяц и год' },
  { path: '/habits', title: 'Привычки', note: 'Регулярные действия и серии' },
  { path: '/body', title: 'Тело и весы', note: 'Вес, состав тела, замеры' },
  { path: '/sleep', title: 'Сон', note: 'Фазы, длительность, восстановление' },
  { path: '/workouts', title: 'Тренировки', note: 'Силовые, кардио, активность' },
  { path: '/nutrition', title: 'Питание', note: 'Калории, вода, рацион' },
  { path: '/courses', title: 'Курсы', note: 'Обучение и конспекты уроков' },
  { path: '/journal', title: 'Журнал', note: 'Записи, мысли, ежедневник' },
  { path: '/mood', title: 'Настроение', note: 'Энергия и эмоциональный фон' },
  { path: '/analytics', title: 'Аналитика', note: 'Корреляции и динамика' },
  { path: '/settings', title: 'Настройки', note: 'Тема, синхронизация, профиль' },
] as const;

export function normalizePath(value: string): string {
  const raw = value.replace(/^#/, '').split('?')[0] ?? '';
  if (!raw || raw === '/') return '/';
  const withSlash = raw.startsWith('/') ? raw : `/${raw}`;
  return withSlash.replace(/\/+$/, '') || '/';
}

export function currentPath(): string {
  return normalizePath(window.location.hash);
}

export function findRoute(path: string): RouteDef | undefined {
  return ROUTES.find((route) => route.path === path);
}

export function navigate(path: string): void {
  const target = path.startsWith('/') ? path : `/${path}`;
  const hash = target === '/' ? '#/' : `#${target}`;
  if (window.location.hash === hash) {
    window.dispatchEvent(new Event('hashchange'));
    return;
  }
  window.location.hash = hash;
}

/** Возвращает актуальный путь из location.hash и перерисовывает при навигации. */
export function useHashRoute(): string {
  const [path, setPath] = useState<string>(() => currentPath());

  useEffect(() => {
    const onHashChange = () => setPath(currentPath());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  return path;
}
