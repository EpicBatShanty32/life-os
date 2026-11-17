import type { IconName } from './components/Icon';

export type NavBadgeKey = 'today' | 'habits' | 'goals' | 'courses';

export interface NavItem {
  path: string;
  label: string;
  icon: IconName;
  badge?: NavBadgeKey;
  /** Слова для поиска в ⌘K. */
  keywords: string[];
}

export interface NavGroup {
  id: string;
  title: string;
  items: NavItem[];
}

export const HOME_ITEM: NavItem = {
  path: '/',
  label: 'Обзор',
  icon: 'home',
  keywords: ['обзор', 'дашборд', 'главная', 'пульс', 'overview', 'home'],
};

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    id: 'focus',
    title: 'Фокус и день',
    items: [
      {
        path: '/today',
        label: 'Сегодня',
        icon: 'today',
        badge: 'today',
        keywords: ['сегодня', 'день', 'задачи', 'план', 'today'],
      },
      {
        path: '/habits',
        label: 'Привычки',
        icon: 'habits',
        badge: 'habits',
        keywords: ['привычки', 'серии', 'регулярность', 'habits'],
      },
      {
        path: '/goals',
        label: 'Цели',
        icon: 'target',
        badge: 'goals',
        keywords: ['цели', 'цель', 'прогресс', 'goals'],
      },
      {
        path: '/mood',
        label: 'Настроение',
        icon: 'mood',
        keywords: ['настроение', 'энергия', 'эмоции', 'mood'],
      },
    ],
  },
  {
    id: 'health',
    title: 'Здоровье и тело',
    items: [
      {
        path: '/body',
        label: 'Тело и весы',
        icon: 'scale',
        keywords: ['тело', 'вес', 'весы', 'жир', 'мышцы', 'замеры', 'body', 'weight'],
      },
      {
        path: '/sleep',
        label: 'Сон',
        icon: 'moon',
        keywords: ['сон', 'фазы', 'восстановление', 'sleep'],
      },
      {
        path: '/workouts',
        label: 'Тренировки',
        icon: 'dumbbell',
        keywords: ['тренировки', 'спорт', 'силовые', 'кардио', 'workouts'],
      },
      {
        path: '/nutrition',
        label: 'Питание',
        icon: 'apple',
        keywords: ['питание', 'калории', 'рацион', 'еда', 'nutrition'],
      },
    ],
  },
  {
    id: 'knowledge',
    title: 'База знаний',
    items: [
      {
        path: '/courses',
        label: 'Курсы',
        icon: 'book',
        badge: 'courses',
        keywords: ['курсы', 'обучение', 'уроки', 'конспекты', 'courses'],
      },
      {
        path: '/journal',
        label: 'Журнал',
        icon: 'journal',
        keywords: ['журнал', 'записи', 'заметки', 'дневник', 'journal'],
      },
    ],
  },
  {
    id: 'analytics',
    title: 'Аналитика',
    items: [
      {
        path: '/analytics',
        label: 'Аналитика',
        icon: 'chart',
        keywords: ['аналитика', 'корреляции', 'графики', 'динамика', 'analytics'],
      },
    ],
  },
] as const;

/** На мобиле сайдбар заменяется нижней навигацией из пяти главных пунктов. */
export const MOBILE_NAV: readonly NavItem[] = [
  HOME_ITEM,
  NAV_GROUPS[0].items[0],
  NAV_GROUPS[1].items[0],
  NAV_GROUPS[2].items[0],
  NAV_GROUPS[3].items[0],
] as const;

export const SETTINGS_ITEM: NavItem = {
  path: '/settings',
  label: 'Настройки',
  icon: 'settings',
  keywords: ['настройки', 'тема', 'синхронизация', 'профиль', 'settings'],
};

export const ALL_NAV_ITEMS: readonly NavItem[] = [
  HOME_ITEM,
  ...NAV_GROUPS.flatMap((group) => group.items),
  SETTINGS_ITEM,
] as const;
