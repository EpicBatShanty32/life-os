/** Контракт данных для дашборда. Совпадает с MASTER_SPEC §2.2 (GET /api/overview). */

export type DataSource = 'api' | 'mock';

export type SyncState = 'ok' | 'syncing' | 'error';

export interface DayPoint {
  /** Локальная дата YYYY-MM-DD. */
  date: string;
  value: number;
}

export interface NavBadges {
  today: number;
  habits: number;
  goals: number;
  courses: number;
}

export interface StreakKpi {
  days: number;
  bestDays: number;
}

export interface CheckinsKpi {
  completed: number;
  total: number;
}

export interface WorkoutsKpi {
  week: number;
  target: number;
}

export interface SleepKpi {
  lastHours: number;
  avg7Hours: number;
}

export interface WaterKpi {
  ml: number;
  targetMl: number;
}

export interface MoodKpi {
  score: number;
  max: number;
  label: string;
  avg7: number;
}

export interface OverviewKpis {
  streak: StreakKpi;
  checkins: CheckinsKpi;
  workouts: WorkoutsKpi;
  sleep: SleepKpi;
  water: WaterKpi;
  mood: MoodKpi;
}

export interface WeightWidget {
  currentKg: number;
  targetKg: number;
  delta30Kg: number;
  series: DayPoint[];
}

export interface OverviewCourse {
  id: string;
  title: string;
  lessonTitle: string;
  doneLessons: number;
  totalLessons: number;
  nextLessonId?: string;
}

export type AgendaKind = 'habit' | 'goal' | 'workout' | 'journal';

export interface AgendaItem {
  id: string;
  title: string;
  meta: string;
  done: boolean;
  kind: AgendaKind;
}

export interface ActivityDay {
  date: string;
  label: string;
  minutes: number;
}

export interface Insight {
  title: string;
  text: string;
  footnote: string;
}

export interface SyncStatus {
  state: SyncState;
  /** ISO-дата последней синхронизации. */
  at: string;
}

export interface OverviewPayload {
  date: string;
  kpis: OverviewKpis;
  weight: WeightWidget;
  course: OverviewCourse | null;
  agenda: AgendaItem[];
  activity: ActivityDay[];
  insight: Insight | null;
  sync: SyncStatus;
  nav: NavBadges;
}

export type QuickEntryType = 'note' | 'metric' | 'event';

export interface QuickEntryInput {
  text: string;
  type: QuickEntryType;
}

export interface PostResult {
  ok: boolean;
  source: DataSource;
}
