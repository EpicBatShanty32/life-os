import { addDays, startOfDay, toLocalIso, weekdayShort } from '../lib/format';
import type { ActivityDay, DayPoint, OverviewPayload } from './types';

const WEIGHT_14 = [
  80.4, 80.2, 80.3, 80.0, 79.9, 80.1, 79.7, 79.9, 79.6, 79.8, 79.5, 79.7, 79.6, 79.8,
];

function isoDaysAgo(days: number, now: Date = new Date()): string {
  return toLocalIso(addDays(startOfDay(now), -days));
}

function buildWeightSeries(now: Date): DayPoint[] {
  return WEIGHT_14.map((value, index) => ({
    date: isoDaysAgo(WEIGHT_14.length - 1 - index, now),
    value,
  }));
}

function buildActivity(now: Date): ActivityDay[] {
  const minutes = [46, 62, 38, 71, 55, 83, 34];
  return minutes.map((value, index) => {
    const date = addDays(startOfDay(now), index - (minutes.length - 1));
    return { date: toLocalIso(date), label: weekdayShort(date), minutes: value };
  });
}

/**
 * Демо-данные дашборда. Используются, пока app/server (Ф4) не отдаёт /api/overview.
 * Все значения правдоподобны и не выдаются за реальные: интерфейс помечает источник.
 */
export function createMockOverview(now: Date = new Date()): OverviewPayload {
  return {
    date: toLocalIso(now),
    kpis: {
      streak: { days: 12, bestDays: 21 },
      checkins: { completed: 4, total: 6 },
      workouts: { week: 3, target: 4 },
      sleep: { lastHours: 7.4, avg7Hours: 7.1 },
      water: { ml: 1250, targetMl: 2000 },
      mood: { score: 7, max: 10, label: 'спокойно', avg7: 6.8 },
    },
    weight: {
      currentKg: 79.8,
      targetKg: 76,
      delta30Kg: -1.4,
      series: buildWeightSeries(now),
    },
    course: {
      id: 'sleep-recovery',
      title: 'Сон и восстановление',
      lessonTitle: 'Урок 6 · Как считать циклы сна',
      doneLessons: 5,
      totalLessons: 12,
      nextLessonId: 'lesson-06',
    },
    agenda: [
      {
        id: 'agenda-stretch',
        title: 'Утренняя растяжка',
        meta: '10 минут · привычка',
        done: true,
        kind: 'habit',
      },
      {
        id: 'agenda-steps',
        title: '10 000 шагов',
        meta: 'прогулка · 7 400 из 10 000',
        done: false,
        kind: 'habit',
      },
      {
        id: 'agenda-workout',
        title: 'Силовая тренировка',
        meta: '18:30 · спина и плечи',
        done: false,
        kind: 'workout',
      },
      {
        id: 'agenda-lesson',
        title: 'Конспект урока «Как считать циклы сна»',
        meta: 'обучение · 15 минут',
        done: false,
        kind: 'goal',
      },
    ],
    activity: buildActivity(now),
    insight: {
      title: 'Сон → энергия',
      text: 'Когда сон длится 7 часов и больше, энергия в течение дня выше на 18%.',
      footnote: 'Корреляция r = 0,42 · n = 21 день',
    },
    sync: {
      state: 'ok',
      at: new Date(now.getTime() - 4 * 60 * 1000).toISOString(),
    },
    nav: { today: 3, habits: 2, goals: 2, courses: 1 },
  };
}
