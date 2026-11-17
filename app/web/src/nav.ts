import type { IconName } from './components/Icon';

/* ─────────────────────────────────────────────────────────────
   Информационная архитектура v4 (HANDOFF §9).
   Один источник правды: сайдбар, мобильная навигация, поиск ⌘K
   и хлебная крошка берут пункты отсюда.
   ───────────────────────────────────────────────────────────── */

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

/** Узел левого меню: одиночная строка или группа с меткой. */
export type SidebarNode =
  | { kind: 'item'; item: NavItem }
  | { kind: 'group'; group: NavGroup };

export const HOME_ITEM: NavItem = {
  path: '/',
  label: 'Обзор',
  icon: 'home',
  keywords: ['обзор', 'дашборд', 'главная', 'пульс', 'день', 'overview', 'home'],
};

export const COMPASS_ITEM: NavItem = {
  path: '/compass',
  label: 'Компас',
  icon: 'compass',
  badge: 'goals',
  keywords: [
    'компас',
    'цели',
    'сферы',
    'горизонты',
    'анти-цели',
    'вехи',
    'compass',
    'goals',
  ],
};

export const TODAY_ITEM: NavItem = {
  path: '/today',
  label: 'Сегодня',
  icon: 'today',
  badge: 'today',
  keywords: ['сегодня', 'день', 'задачи', 'план', 'расписание', 'today'],
};

export const WEEK_ITEM: NavItem = {
  path: '/week',
  label: 'Неделя',
  icon: 'calendar',
  keywords: ['неделя', 'канбан', 'календарь', 'расписание', 'week'],
};

export const INBOX_ITEM: NavItem = {
  path: '/inbox',
  label: 'Инбокс',
  icon: 'inbox',
  keywords: ['инбокс', 'входящие', 'триаж', 'мысли', 'идеи', 'inbox'],
};

export const PROJECTS_ITEM: NavItem = {
  path: '/projects',
  label: 'Проекты',
  icon: 'briefcase',
  keywords: ['проекты', 'этапы', 'заметки', 'ссылки', 'projects'],
};

export const LIBRARY_ITEM: NavItem = {
  path: '/library',
  label: 'Библиотека',
  icon: 'library',
  keywords: ['библиотека', 'книги', 'полки', 'цитаты', 'чтение', 'library', 'books'],
};

export const COURSES_ITEM: NavItem = {
  path: '/courses',
  label: 'Курсы',
  icon: 'book',
  badge: 'courses',
  keywords: ['курсы', 'обучение', 'уроки', 'конспекты', 'courses'],
};

export const JOURNAL_ITEM: NavItem = {
  path: '/journal',
  label: 'Журнал',
  icon: 'journal',
  keywords: ['журнал', 'записи', 'заметки', 'дневник', 'journal'],
};

export const BODY_ITEM: NavItem = {
  path: '/body',
  label: 'Тело',
  icon: 'scale',
  keywords: ['тело', 'вес', 'весы', 'жир', 'мышцы', 'замеры', 'body', 'weight'],
};

export const SLEEP_ITEM: NavItem = {
  path: '/sleep',
  label: 'Сон',
  icon: 'moon',
  keywords: ['сон', 'фазы', 'восстановление', 'sleep'],
};

export const WORKOUTS_ITEM: NavItem = {
  path: '/workouts',
  label: 'Тренировки',
  icon: 'dumbbell',
  keywords: ['тренировки', 'спорт', 'силовые', 'кардио', 'workouts'],
};

export const NUTRITION_ITEM: NavItem = {
  path: '/nutrition',
  label: 'Питание',
  icon: 'apple',
  keywords: ['питание', 'калории', 'рацион', 'еда', 'nutrition'],
};

export const HABITS_ITEM: NavItem = {
  path: '/habits',
  label: 'Привычки',
  icon: 'habits',
  badge: 'habits',
  keywords: ['привычки', 'серии', 'регулярность', 'habits'],
};

export const MOOD_ITEM: NavItem = {
  path: '/mood',
  label: 'Настроение',
  icon: 'mood',
  keywords: ['настроение', 'энергия', 'эмоции', 'mood'],
};

export const FINANCE_ITEM: NavItem = {
  path: '/finance',
  label: 'Финансы',
  icon: 'wallet',
  keywords: [
    'финансы',
    'деньги',
    'расходы',
    'доходы',
    'бюджет',
    'транзакции',
    'finance',
    'money',
  ],
};

export const PERSONAL_ITEM: NavItem = {
  path: '/personal',
  label: 'Ресурс',
  icon: 'battery',
  keywords: ['ресурс', 'энергия', 'чек-ин', 'силы', 'личное', 'personal', 'energy'],
};

export const REVIEW_ITEM: NavItem = {
  path: '/review',
  label: 'Итоги',
  icon: 'list',
  keywords: ['итоги', 'обзор недели', 'обзор месяца', 'рефлексия', 'review'],
};

export const ANALYTICS_ITEM: NavItem = {
  path: '/analytics',
  label: 'Аналитика',
  icon: 'chart',
  keywords: ['аналитика', 'корреляции', 'графики', 'динамика', 'analytics'],
};

export const SETTINGS_ITEM: NavItem = {
  path: '/settings',
  label: 'Настройки',
  icon: 'settings',
  keywords: ['настройки', 'тема', 'синхронизация', 'профиль', 'о программе', 'settings'],
};

/* ── Группы ─────────────────────────────────────────────── */

export const DEALS_GROUP: NavGroup = {
  id: 'deals',
  title: 'Дела',
  items: [TODAY_ITEM, WEEK_ITEM, INBOX_ITEM, PROJECTS_ITEM],
};

export const KNOWLEDGE_GROUP: NavGroup = {
  id: 'knowledge',
  title: 'База знаний',
  items: [LIBRARY_ITEM, COURSES_ITEM, JOURNAL_ITEM],
};

export const HEALTH_GROUP: NavGroup = {
  id: 'health',
  title: 'Здоровье',
  items: [
    BODY_ITEM,
    SLEEP_ITEM,
    WORKOUTS_ITEM,
    NUTRITION_ITEM,
    HABITS_ITEM,
    MOOD_ITEM,
  ],
};

export const PERSONAL_GROUP: NavGroup = {
  id: 'personal',
  title: 'Личное',
  items: [PERSONAL_ITEM, REVIEW_ITEM],
};

export const NAV_GROUPS: readonly NavGroup[] = [
  DEALS_GROUP,
  KNOWLEDGE_GROUP,
  HEALTH_GROUP,
  PERSONAL_GROUP,
];

/** Порядок левого меню: строки без метки идут как есть, группы — с подписью. */
export const SIDEBAR_NAV: readonly SidebarNode[] = [
  { kind: 'item', item: HOME_ITEM },
  { kind: 'item', item: COMPASS_ITEM },
  { kind: 'group', group: DEALS_GROUP },
  { kind: 'group', group: KNOWLEDGE_GROUP },
  { kind: 'group', group: HEALTH_GROUP },
  { kind: 'item', item: FINANCE_ITEM },
  { kind: 'group', group: PERSONAL_GROUP },
  { kind: 'item', item: ANALYTICS_ITEM },
];

/** Разделы верхнего уровня — для поиска и шторки «Ещё». */
export const TOP_LEVEL_ITEMS: readonly NavItem[] = [
  HOME_ITEM,
  COMPASS_ITEM,
  FINANCE_ITEM,
  ANALYTICS_ITEM,
];

/** Пять пунктов нижней навигации; шестой — «Ещё» со шторкой. */
export const MOBILE_NAV: readonly NavItem[] = [
  HOME_ITEM,
  TODAY_ITEM,
  COMPASS_ITEM,
  FINANCE_ITEM,
  ANALYTICS_ITEM,
];

export const ALL_NAV_ITEMS: readonly NavItem[] = [
  HOME_ITEM,
  COMPASS_ITEM,
  ...DEALS_GROUP.items,
  ...KNOWLEDGE_GROUP.items,
  ...HEALTH_GROUP.items,
  FINANCE_ITEM,
  ...PERSONAL_GROUP.items,
  ANALYTICS_ITEM,
  SETTINGS_ITEM,
];

/** Активный пункт по пути: детальные маршруты наследуют раздел через router. */
export function navItemForPath(path: string): NavItem | undefined {
  return ALL_NAV_ITEMS.find((item) => item.path === path);
}

/** Группа, к которой относится путь — для хлебной крошки. */
export function navGroupForPath(path: string): NavGroup | undefined {
  return NAV_GROUPS.find((group) => group.items.some((item) => item.path === path));
}
