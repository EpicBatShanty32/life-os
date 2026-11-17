/** Свой хеш-роутер: без react-router и других зависимостей.
 *
 *  Формат адреса — `#/path?key=value`: фильтры экрана живут в hash,
 *  чтобы ссылку можно было сохранить и переслать (DESIGN-v4 §4).
 */

import { useEffect, useState } from 'react';

export interface RouteDef {
  path: string;
  title: string;
  /** Короткое пояснение для пустых разделов и поиска. */
  note?: string;
  /** Маршрут объявлен, но экран ещё не собран: оболочка показывает заглушку. */
  soon?: boolean;
}

export const ROUTES: readonly RouteDef[] = [
  {
    path: '/',
    title: 'Обзор',
    note: 'День одним взглядом: пульс, план, тело и учение',
  },
  {
    path: '/compass',
    title: 'Компас',
    note: 'Сферы, горизонты года, квартала и месяца, анти-цели',
    soon: true,
  },
  {
    path: '/today',
    title: 'Сегодня',
    note: 'Жёсткие события и гибкие задачи по ресурсу',
  },
  {
    path: '/week',
    title: 'Неделя',
    note: 'Канбан и календарная сетка недели',
    soon: true,
  },
  {
    path: '/inbox',
    title: 'Инбокс',
    note: 'Парковка идей и триаж',
    soon: true,
  },
  {
    path: '/projects',
    title: 'Проекты',
    note: 'Карточки проектов, этапы, заметки и ссылки',
    soon: true,
  },
  {
    path: '/library',
    title: 'Библиотека',
    note: 'Полки книг, цитаты и мысли',
    soon: true,
  },
  {
    path: '/courses',
    title: 'Курсы',
    note: 'Обучение и конспекты уроков',
  },
  {
    path: '/journal',
    title: 'Журнал',
    note: 'Записи, мысли, ежедневник',
  },
  {
    path: '/body',
    title: 'Тело',
    note: 'Вес, состав тела, замеры',
  },
  {
    path: '/sleep',
    title: 'Сон',
    note: 'Фазы, длительность, восстановление',
  },
  {
    path: '/workouts',
    title: 'Тренировки',
    note: 'Силовые, кардио, активность',
  },
  {
    path: '/nutrition',
    title: 'Питание',
    note: 'Калории, вода, рацион',
  },
  {
    path: '/habits',
    title: 'Привычки',
    note: 'Регулярные действия и серии',
  },
  {
    path: '/mood',
    title: 'Настроение',
    note: 'Энергия и эмоциональный фон',
  },
  {
    path: '/finance',
    title: 'Финансы',
    note: 'Обзор месяца, транзакции, бюджеты',
    soon: true,
  },
  {
    path: '/personal',
    title: 'Ресурс',
    note: 'Чек-ин энергии и личные списки',
    soon: true,
  },
  {
    path: '/review',
    title: 'Итоги',
    note: 'Мастер недели и месяца',
    soon: true,
  },
  {
    path: '/analytics',
    title: 'Аналитика',
    note: 'Энергия, сферы, чтение, деньги',
  },
  {
    path: '/settings',
    title: 'Настройки',
    note: 'Тема, подключения, данные, о программе',
  },
] as const;

/** Фильтры экрана: `#/library?status=reading`. */
export type RouteFilters = Record<string, string>;

export interface RouteLocation {
  path: string;
  filters: RouteFilters;
}

export function normalizePath(value: string): string {
  const raw = value.replace(/^#/, '').split('?')[0] ?? '';
  if (!raw || raw === '/') return '/';
  const withSlash = raw.startsWith('/') ? raw : `/${raw}`;
  return withSlash.replace(/\/+$/, '') || '/';
}

function readFilters(query: string): RouteFilters {
  const filters: RouteFilters = {};
  new URLSearchParams(query).forEach((value, key) => {
    const trimmed = value.trim();
    if (trimmed) filters[key] = trimmed;
  });
  return filters;
}

/** Разбирает hash в путь и фильтры; пустые значения отбрасываются. */
export function parseHash(hash: string): RouteLocation {
  const clean = hash.replace(/^#/, '');
  const separator = clean.indexOf('?');
  const rawPath = separator >= 0 ? clean.slice(0, separator) : clean;
  const query = separator >= 0 ? clean.slice(separator + 1) : '';
  return { path: normalizePath(rawPath), filters: readFilters(query) };
}

export function currentPath(): string {
  return parseHash(window.location.hash).path;
}

export function currentFilters(): RouteFilters {
  return parseHash(window.location.hash).filters;
}

export function findRoute(path: string): RouteDef | undefined {
  return ROUTES.find((route) => route.path === path);
}

function toQueryString(filters: RouteFilters | undefined): string {
  if (!filters) return '';
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      params.set(key, String(value));
    }
  });
  const query = params.toString();
  return query ? `?${query}` : '';
}

/** Ссылка для `<a href>`: всегда с ведущим `#`. */
export function hrefFor(path: string, filters?: RouteFilters): string {
  const target = path.startsWith('/') ? path : `/${path}`;
  return `#${target === '/' ? '/' : target}${toQueryString(filters)}`;
}

/**
 * Переход по hash. Повторный переход на тот же адрес всё равно шлёт
 * `hashchange`, чтобы экран мог перезапросить данные.
 */
export function navigate(path: string, filters?: RouteFilters): void {
  const hash = hrefFor(path, filters);
  if (window.location.hash === hash) {
    window.dispatchEvent(new Event('hashchange'));
    return;
  }
  window.location.hash = hash;
}

/**
 * Меняет фильтры текущего экрана, не теряя остальные.
 * `replace` не плодит записи в истории (для сегментов и сортировок).
 */
export function setFilters(patch: RouteFilters, options: { replace?: boolean } = {}): void {
  const location = parseHash(window.location.hash);
  const next: RouteFilters = { ...location.filters, ...patch };
  Object.keys(next).forEach((key) => {
    if (!next[key] || !String(next[key]).trim()) delete next[key];
  });
  const hash = hrefFor(location.path, next);

  if (options?.replace) {
    const url = `${window.location.pathname}${window.location.search}${hash}`;
    window.history.replaceState(null, '', url);
    window.dispatchEvent(new Event('hashchange'));
    return;
  }

  if (window.location.hash === hash) {
    window.dispatchEvent(new Event('hashchange'));
    return;
  }
  window.location.hash = hash;
}

/** Актуальный адрес из location.hash с перерисовкой при навигации. */
export function useHashLocation(): RouteLocation {
  const [location, setLocation] = useState<RouteLocation>(() =>
    parseHash(window.location.hash),
  );

  useEffect(() => {
    const onChange = () => setLocation(parseHash(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  return location;
}

/** Возвращает только путь: эффекты, которым не важны фильтры, не перезапускаются. */
export function useHashRoute(): string {
  return useHashLocation().path;
}

/** Фильтры текущего экрана (для экранов-модулей v4). */
export function useRouteFilters(): RouteFilters {
  return useHashLocation().filters;
}
