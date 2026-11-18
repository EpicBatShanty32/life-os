import type { IconName } from '../components/Icon';

/**
 * Иконка привычки вместо эмодзи (DESIGN-v4 §3: в интерфейсе нет эмодзи).
 *
 * Данные приходят из базы, где у привычки может лежать эмодзи или ничего.
 * Сначала пробуем точное совпадение по эмодзи, потом по смыслу названия,
 * в конце — нейтральная иконка «привычки».
 */
const BY_EMOJI: Readonly<Record<string, IconName>> = {
  '💧': 'droplet',
  '🚰': 'droplet',
  '🥤': 'droplet',
  '🚶': 'walk',
  '🏃': 'walk',
  '👟': 'walk',
  '📖': 'book',
  '📚': 'book',
  '🧘': 'stretch',
  '🤸': 'stretch',
  '💪': 'dumbbell',
  '🏋': 'dumbbell',
  '🏋️': 'dumbbell',
  '😴': 'moon',
  '🌙': 'moon',
  '🍎': 'apple',
  '🥗': 'apple',
  '💰': 'wallet',
  '💸': 'wallet',
  '✍️': 'journal',
  '📝': 'journal',
  '🎯': 'target',
  '❤️': 'heart',
  '🔥': 'flame',
  '🧠': 'sparkles',
  '💻': 'briefcase',
  '🧹': 'list',
  '☀️': 'sun',
};

const BY_NAME: readonly (readonly [RegExp, IconName])[] = [
  [/вод|пить|гидрат/i, 'droplet'],
  [/прогул|шаг|ходьб|бег|гуля/i, 'walk'],
  [/чтен|книг|чтени/i, 'book'],
  [/растяж|йог|медит|дыхан|осанк/i, 'stretch'],
  [/трениров|зал|силов|отжим|присед|планк/i, 'dumbbell'],
  [/сон|спать|режим сна|экран до/i, 'moon'],
  [/еда|питан|калор|сахар|фастфуд|перекус/i, 'apple'],
  [/деньг|финанс|бюджет|расход|накоплен/i, 'wallet'],
  [/запис|дневник|утренн|вечерн|писат/i, 'journal'],
  [/фокус|цель|план|приоритет/i, 'target'],
  [/заряд|растяжк|сердц|здоров/i, 'heart'],
  [/код|работ|проект|учеб/i, 'briefcase'],
  [/убор|порядок|разбор|список/i, 'list'],
  [/язык|слов|урок|курс|учить/i, 'library'],
];

export function habitIcon(emoji?: string | null, name?: string | null): IconName {
  const mark = (emoji ?? '').trim();
  if (mark) {
    const exact = BY_EMOJI[mark];
    if (exact) return exact;
  }
  const title = name ?? '';
  for (const [pattern, icon] of BY_NAME) {
    if (pattern.test(title)) return icon;
  }
  return 'habits';
}
