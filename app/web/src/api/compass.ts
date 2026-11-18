import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { apiRequest } from './client';
import { apiErrorMessage } from './errors';

/* ─────────────────────────────────────────────────────────────
   Компас: сферы, цели, вехи и анти-цели (handoff §12.1).
   ───────────────────────────────────────────────────────────── */

export type Horizon = 'year' | 'quarter' | 'month';
export type GoalStatus = 'active' | 'done' | 'dropped';
export type AntiPeriod = 'month' | 'quarter';
export type SphereArchiveFilter = 'all' | 'active' | 'archived';

export interface Sphere {
  id: number;
  name: string;
  color: string;
  icon: string;
  sort: number;
  archived: boolean;
}

export interface GoalSphere {
  id: number;
  name: string;
  color: string;
}

export interface GoalStep {
  id: number;
  goalId: number;
  title: string;
  done: boolean;
  due: string | null;
  sort: number;
}

export interface GoalCounts {
  stepsDone: number;
  stepsTotal: number;
  tasksDone: number;
  tasksTotal: number;
}

export interface Goal {
  id: number;
  title: string;
  horizon: Horizon;
  sphereId: number | null;
  sphere: GoalSphere | null;
  parentId: number | null;
  deadline: string | null;
  status: GoalStatus;
  progress: number;
  progressPct: number;
  why: string;
  target: number | null;
  current: number;
  unit: string;
  steps: GoalStep[];
  counts: GoalCounts;
  created: string;
}

export interface AntiGoal {
  id: number;
  period: AntiPeriod;
  text: string;
  active: boolean;
  created: string;
}

export interface CompassPayload {
  date: string;
  spheres: Sphere[];
  goals: Goal[];
  antiGoals: Record<AntiPeriod, AntiGoal[]>;
}

export interface GoalInput {
  title: string;
  horizon?: Horizon;
  sphereId?: number | null;
  parentId?: number | null;
  why?: string;
  deadline?: string | null;
  target?: number | null;
  unit?: string;
  progress?: number;
  status?: GoalStatus;
}

export interface SphereInput {
  name: string;
  color?: string;
  icon?: string;
}

export interface GoalStepInput {
  title: string;
  due?: string | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function normalizePayload(raw: unknown): CompassPayload {
  const root = asRecord(raw);
  if (!root) throw new Error('compass: ответ не является объектом');
  const antiRaw = asRecord(root.antiGoals);
  return {
    date: typeof root.date === 'string' ? root.date : '',
    spheres: asArray(root.spheres) as Sphere[],
    goals: asArray(root.goals) as Goal[],
    antiGoals: {
      month: asArray(antiRaw?.month) as AntiGoal[],
      quarter: asArray(antiRaw?.quarter) as AntiGoal[],
    },
  };
}

/* ── Запросы ─────────────────────────────────────────────── */

export async function fetchCompass(date?: string): Promise<CompassPayload> {
  const query = date ? `?date=${encodeURIComponent(date)}` : '';
  const raw = await apiRequest<unknown>(`/compass${query}`, undefined, 6000);
  return normalizePayload(raw);
}

export async function fetchSpheres(archived: SphereArchiveFilter = 'active'): Promise<Sphere[]> {
  const raw = await apiRequest<unknown>(`/spheres?archived=${archived}`, undefined, 5000);
  const root = asRecord(raw);
  return asArray(root?.items) as Sphere[];
}

export async function createSphere(input: SphereInput): Promise<Sphere> {
  return apiRequest<Sphere>('/spheres', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function patchSphere(
  id: number,
  patch: Partial<SphereInput> & { archived?: boolean },
): Promise<Sphere> {
  return apiRequest<Sphere>(`/spheres/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export interface DeleteSphereResult {
  id: number;
  deleted: boolean;
  archived: boolean;
  usage: Record<string, number>;
}

export async function deleteSphere(id: number): Promise<DeleteSphereResult> {
  return apiRequest<DeleteSphereResult>(`/spheres/${id}`, { method: 'DELETE' });
}

export async function createGoal(input: GoalInput): Promise<Goal> {
  return apiRequest<Goal>('/goals', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function patchGoal(id: number, patch: Partial<GoalInput>): Promise<Goal> {
  return apiRequest<Goal>(`/goals/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export async function deleteGoal(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/goals/${id}`, { method: 'DELETE' });
}

export async function createGoalStep(goalId: number, input: GoalStepInput): Promise<GoalStep> {
  return apiRequest<GoalStep>(`/goals/${goalId}/steps`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function patchGoalStep(
  id: number,
  patch: { title?: string; done?: boolean },
): Promise<GoalStep> {
  return apiRequest<GoalStep>(`/goal-steps/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export async function deleteGoalStep(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/goal-steps/${id}`, { method: 'DELETE' });
}

export async function createAntiGoal(input: {
  period: AntiPeriod;
  text: string;
}): Promise<AntiGoal> {
  return apiRequest<AntiGoal>('/anti-goals', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function patchAntiGoal(
  id: number,
  patch: { text?: string; active?: boolean },
): Promise<AntiGoal> {
  return apiRequest<AntiGoal>(`/anti-goals/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export async function deleteAntiGoal(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/anti-goals/${id}`, { method: 'DELETE' });
}

/* ── Хуки ────────────────────────────────────────────────── */

export interface CompassState {
  data: CompassPayload | null;
  setData: Dispatch<SetStateAction<CompassPayload | null>>;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useCompass(date?: string): CompassState {
  const [data, setData] = useState<CompassPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fetchCompass(date)
      .then((payload) => {
        if (sequence.current === current) setData(payload);
      })
      .catch((problem: unknown) => {
        if (sequence.current === current) {
          setError(apiErrorMessage(problem, 'Не удалось загрузить компас'));
        }
      })
      .finally(() => {
        if (sequence.current === current) setLoading(false);
      });
    return () => {
      sequence.current += 1;
    };
  }, [date, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  return { data, setData, loading, error, reload };
}

export interface SpheresState {
  items: Sphere[];
  setItems: Dispatch<SetStateAction<Sphere[]>>;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useSpheres(archived: SphereArchiveFilter = 'active'): SpheresState {
  const [items, setItems] = useState<Sphere[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fetchSpheres(archived)
      .then((list) => {
        if (sequence.current === current) setItems(list);
      })
      .catch((problem: unknown) => {
        if (sequence.current === current) {
          setError(apiErrorMessage(problem, 'Не удалось загрузить сферы'));
        }
      })
      .finally(() => {
        if (sequence.current === current) setLoading(false);
      });
    return () => {
      sequence.current += 1;
    };
  }, [archived, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  return { items, setItems, loading, error, reload };
}
