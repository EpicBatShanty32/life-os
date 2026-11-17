import { useCallback, useEffect, useRef, useState } from 'react';

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');
const TIMEOUT_MS = 4000;

export interface SectionKpi {
  label: string;
  value: number | string;
  unit: string;
  hint: string;
}

export interface SectionPoint {
  date: string;
  value: number;
}

export interface SectionSeries {
  key: string;
  label: string;
  unit: string;
  points: SectionPoint[];
  last: number;
  avg: number;
  total: number;
  days: number;
}

export interface SectionItem {
  id: number;
  date: string;
  time: string;
  kind: string;
  kindLabel: string;
  text: string;
  source: string;
}

export interface SectionHabit {
  id: number;
  name: string;
  emoji: string;
  target: number;
  marks: boolean[];
  week: string[];
  doneWeek: number;
  total: number;
  streak: number;
}

export interface SectionGoal {
  id: number;
  title: string;
  current: number;
  target: number | null;
  unit: string;
  deadline: string | null;
  status: 'active' | 'done' | 'dropped';
  percent: number;
}

export interface SectionPayload {
  name: string;
  title: string;
  note: string;
  days: number;
  since: string;
  today: string;
  kpis: SectionKpi[];
  series: SectionSeries[];
  items: SectionItem[];
  empty: boolean;
  habits?: SectionHabit[];
  goals?: SectionGoal[];
  count?: number;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: 'same-origin',
      headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

export interface SectionState {
  data: SectionPayload | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useSection(name: string, days: number, search = ''): SectionState {
  const [data, setData] = useState<SectionPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const query = search.trim() ? `&q=${encodeURIComponent(search.trim())}` : '';
    request<SectionPayload>(`/sections/${name}?days=${days}${query}`)
      .then((payload) => {
        if (!cancelled && alive.current) setData(payload);
      })
      .catch((problem: unknown) => {
        if (!cancelled && alive.current) {
          setError(problem instanceof Error ? problem.message : 'нет ответа');
        }
      })
      .finally(() => {
        if (!cancelled && alive.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [name, days, search, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  return { data, loading, error, reload };
}

export function markHabit(habitId: number, value: number, day?: string): Promise<unknown> {
  return request(`/habits/${habitId}/mark`, {
    method: 'POST',
    body: JSON.stringify({ value, day }),
  });
}

export function createGoal(input: { title: string; target?: number; unit?: string }): Promise<unknown> {
  return request('/goals', { method: 'POST', body: JSON.stringify(input) });
}

export function patchGoal(goalId: number, patch: { current?: number; status?: string }): Promise<unknown> {
  return request(`/goals/${goalId}`, { method: 'PATCH', body: JSON.stringify(patch) });
}

export function createHabit(input: { name: string; emoji?: string; target_per_week?: number }): Promise<unknown> {
  return request('/habits', { method: 'POST', body: JSON.stringify(input) });
}
