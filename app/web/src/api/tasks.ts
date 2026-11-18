import { useCallback, useEffect, useRef, useState } from 'react';
import { apiRequest } from './client';
import { apiErrorMessage, apiStatus } from './errors';
import { toast } from '../lib/toast';

/* ─────────────────────────────────────────────────────────────
   Дела: задачи, инбокс и проекты (handoff §12.2).
   Все мутации оптимистичны: сначала состояние, потом запрос;
   ошибка — тост и перезагрузка блока (без «поломанного» UI).
   ───────────────────────────────────────────────────────────── */

export type Resource = 'must' | 'important' | 'low';
export type TaskStatus = 'todo' | 'done' | 'cancelled';
export type TaskView = 'day' | 'week' | 'kanban' | 'calendar';
export type ListKind = 'work' | 'personal';
export type ProjectStatus = 'idea' | 'active' | 'paused' | 'done';

export interface Task {
  id: number;
  title: string;
  sphereId: number | null;
  sphereName: string | null;
  projectId: number | null;
  projectName: string | null;
  goalId: number | null;
  listKind: ListKind;
  tags: string[];
  dueDate: string | null;
  startAt: string | null;
  endAt: string | null;
  resource: Resource;
  status: TaskStatus;
  recurrence: string;
  notes: string;
  doneAt: string | null;
  created: string;
}

export interface TaskCounts {
  must: number;
  important: number;
  low: number;
  done: number;
  total: number;
}

export interface TasksPayload {
  items: Task[];
  counts: TaskCounts;
  view?: TaskView | string;
  date?: string | null;
  from?: string | null;
  to?: string | null;
}

export interface TaskInput {
  title: string;
  dueDate?: string | null;
  startAt?: string | null;
  endAt?: string | null;
  resource?: Resource;
  sphereId?: number | null;
  projectId?: number | null;
  goalId?: number | null;
  listKind?: ListKind;
  tags?: string | string[] | null;
  notes?: string;
  recurrence?: string;
}

export interface TaskPatch {
  title?: string;
  dueDate?: string | null;
  startAt?: string | null;
  endAt?: string | null;
  resource?: Resource;
  sphereId?: number | null;
  projectId?: number | null;
  goalId?: number | null;
  listKind?: ListKind;
  tags?: string | string[] | null;
  notes?: string;
  recurrence?: string;
  status?: TaskStatus;
}

export interface CompleteResult {
  task: Task;
  next: Task | null;
}

export interface TasksQuery {
  date?: string;
  view?: TaskView;
  from?: string;
  to?: string;
  sphereId?: number;
  projectId?: number;
  resource?: Resource;
  status?: TaskStatus | 'all';
  listKind?: ListKind;
  q?: string;
}

export interface InboxItem {
  id: number;
  text: string;
  status: 'new' | 'processed';
  convertedType: string;
  convertedId: number | null;
  created: string;
}

export interface InboxCounts {
  new: number;
  processed: number;
  total: number;
}

export interface InboxPayload {
  items: InboxItem[];
  counts: InboxCounts;
}

export type InboxConvertTo = 'task' | 'project' | 'personal' | 'book' | 'delete';

export interface Project {
  id: number;
  name: string;
  sphereId: number | null;
  sphereName: string | null;
  goalId: number | null;
  deadline: string | null;
  status: ProjectStatus;
  outcome: string;
  description: string;
  created: string;
  updated: string | null;
  stepsTotal: number;
  stepsDone: number;
  tasksTotal: number;
  tasksDone: number;
}

export interface ProjectCounts {
  idea: number;
  active: number;
  paused: number;
  done: number;
  total: number;
}

export interface ProjectsPayload {
  items: Project[];
  counts: ProjectCounts;
}

export interface ProjectStep {
  id: number;
  projectId: number;
  title: string;
  done: boolean;
  sort: number;
}

export interface ProjectNote {
  id: number;
  projectId: number;
  kind: 'note' | 'link';
  title: string;
  body: string;
  url: string;
  created: string;
}

export interface ProjectDetail {
  project: Project;
  steps: ProjectStep[];
  notes: ProjectNote[];
  tasks: { items: Task[]; counts: TaskCounts };
}

export interface ProjectInput {
  name: string;
  sphereId?: number | null;
  goalId?: number | null;
  deadline?: string | null;
  status?: ProjectStatus;
  outcome?: string;
  description?: string;
}

/* ── Нормализация ────────────────────────────────────────── */

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function countTasks(items: readonly Task[]): TaskCounts {
  const counts: TaskCounts = { must: 0, important: 0, low: 0, done: 0, total: 0 };
  for (const item of items) {
    if (item.status === 'cancelled') continue;
    counts.total += 1;
    if (item.status === 'done') {
      counts.done += 1;
    } else if (item.resource in counts) {
      counts[item.resource] += 1;
    }
  }
  return counts;
}

function normalizeTags(value: string | string[] | null | undefined): string[] {
  if (Array.isArray(value)) return value.map((tag) => tag.trim()).filter(Boolean);
  return (value ?? '')
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function normalizeTasks(raw: unknown): TasksPayload {
  const root = asRecord(raw);
  if (!root) throw new Error('tasks: ответ не является объектом');
  const items = asArray(root.items) as Task[];
  return {
    items,
    counts: countTasks(items),
    view: typeof root.view === 'string' ? root.view : undefined,
    date: typeof root.date === 'string' ? root.date : null,
    from: typeof root.from === 'string' ? root.from : null,
    to: typeof root.to === 'string' ? root.to : null,
  };
}

function normalizeInbox(raw: unknown): InboxPayload {
  const root = asRecord(raw);
  if (!root) throw new Error('inbox: ответ не является объектом');
  const items = asArray(root.items) as InboxItem[];
  const newCount = items.filter((item) => item.status === 'new').length;
  return {
    items,
    counts: { new: newCount, processed: items.length - newCount, total: items.length },
  };
}

function normalizeProjects(raw: unknown): ProjectsPayload {
  const root = asRecord(raw);
  if (!root) throw new Error('projects: ответ не является объектом');
  const items = asArray(root.items) as Project[];
  const counts: ProjectCounts = { idea: 0, active: 0, paused: 0, done: 0, total: items.length };
  for (const item of items) {
    if (item.status in counts) counts[item.status] += 1;
  }
  return { items, counts };
}

function normalizeDetail(raw: unknown): ProjectDetail {
  const root = asRecord(raw);
  const project = asRecord(root?.project);
  if (!project) throw new Error('project: ответ без проекта');
  const tasksRaw = asRecord(root?.tasks);
  const tasks = asArray(tasksRaw?.items) as Task[];
  return {
    project: project as unknown as Project,
    steps: asArray(root?.steps) as ProjectStep[],
    notes: asArray(root?.notes) as ProjectNote[],
    tasks: { items: tasks, counts: countTasks(tasks) },
  };
}

function todayIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(
    now.getDate(),
  ).padStart(2, '0')}`;
}

/* ── Запросы: задачи ─────────────────────────────────────── */

function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export async function fetchTasks(query: TasksQuery): Promise<TasksPayload> {
  const raw = await apiRequest<unknown>(
    `/tasks${buildQuery({
      date: query.date,
      view: query.view,
      from: query.from,
      to: query.to,
      sphereId: query.sphereId,
      projectId: query.projectId,
      resource: query.resource,
      status: query.status,
      listKind: query.listKind,
      q: query.q,
    })}`,
    undefined,
    6000,
  );
  return normalizeTasks(raw);
}

export async function createTask(input: TaskInput): Promise<Task> {
  return apiRequest<Task>('/tasks', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function patchTask(id: number, patch: TaskPatch): Promise<Task> {
  return apiRequest<Task>(`/tasks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export async function completeTask(id: number, done: boolean): Promise<CompleteResult> {
  return apiRequest<CompleteResult>(`/tasks/${id}/complete`, {
    method: 'POST',
    body: JSON.stringify({ done }),
  });
}

export async function rescheduleTask(id: number, dueDate: string): Promise<Task> {
  return apiRequest<Task>(`/tasks/${id}/reschedule`, {
    method: 'POST',
    body: JSON.stringify({ dueDate }),
  });
}

export async function deleteTask(id: number): Promise<Task> {
  return apiRequest<Task>(`/tasks/${id}`, { method: 'DELETE' });
}

/* ── Запросы: инбокс ─────────────────────────────────────── */

export async function fetchInbox(status: 'new' | 'processed' | 'all' = 'new'): Promise<InboxPayload> {
  const raw = await apiRequest<unknown>(`/inbox?status=${status}`, undefined, 5000);
  return normalizeInbox(raw);
}

export async function createInbox(text: string): Promise<InboxItem> {
  return apiRequest<InboxItem>('/inbox', {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
}

export interface ConvertInboxResult {
  item?: InboxItem;
  task?: Task;
  project?: Project;
  book?: unknown;
  ok?: boolean;
  id?: number;
}

export async function convertInbox(
  id: number,
  to: InboxConvertTo,
  payload?: Record<string, unknown>,
): Promise<ConvertInboxResult> {
  return apiRequest<ConvertInboxResult>(`/inbox/${id}/convert`, {
    method: 'POST',
    body: JSON.stringify(payload ? { to, payload } : { to }),
  });
}

export async function deleteInbox(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/inbox/${id}`, { method: 'DELETE' });
}

/* ── Запросы: проекты ────────────────────────────────────── */

export async function fetchProjects(query: {
  status?: ProjectStatus;
  sphereId?: number;
  q?: string;
}): Promise<ProjectsPayload> {
  const raw = await apiRequest<unknown>(
    `/projects${buildQuery({
      status: query.status,
      sphereId: query.sphereId,
      q: query.q,
    })}`,
    undefined,
    6000,
  );
  return normalizeProjects(raw);
}

export async function fetchProject(id: number): Promise<ProjectDetail> {
  const raw = await apiRequest<unknown>(`/projects/${id}`, undefined, 6000);
  return normalizeDetail(raw);
}

export async function createProject(input: ProjectInput): Promise<Project> {
  return apiRequest<Project>('/projects', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function patchProject(
  id: number,
  patch: Partial<ProjectInput>,
): Promise<Project> {
  return apiRequest<Project>(`/projects/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export async function deleteProject(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/projects/${id}`, { method: 'DELETE' });
}

export async function createProjectStep(projectId: number, title: string): Promise<ProjectStep> {
  return apiRequest<ProjectStep>(`/projects/${projectId}/steps`, {
    method: 'POST',
    body: JSON.stringify({ title }),
  });
}

export async function patchProjectStep(
  id: number,
  patch: { title?: string; done?: boolean },
): Promise<ProjectStep> {
  return apiRequest<ProjectStep>(`/project-steps/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export async function deleteProjectStep(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/project-steps/${id}`, { method: 'DELETE' });
}

export async function createProjectNote(
  projectId: number,
  input: { kind: 'note' | 'link'; title?: string; body?: string; url?: string },
): Promise<ProjectNote> {
  return apiRequest<ProjectNote>(`/projects/${projectId}/notes`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function deleteProjectNote(id: number): Promise<unknown> {
  return apiRequest<unknown>(`/project-notes/${id}`, { method: 'DELETE' });
}

/* ── Вспомогательное для оптимистичных обновлений ───────── */

function mergeTask(task: Task, patch: TaskPatch): Task {
  const next: Task = { ...task };
  if (patch.title !== undefined) next.title = patch.title;
  if (patch.dueDate !== undefined) next.dueDate = patch.dueDate;
  if (patch.startAt !== undefined) next.startAt = patch.startAt;
  if (patch.endAt !== undefined) next.endAt = patch.endAt;
  if (patch.resource !== undefined) next.resource = patch.resource;
  if (patch.sphereId !== undefined) next.sphereId = patch.sphereId;
  if (patch.projectId !== undefined) next.projectId = patch.projectId;
  if (patch.goalId !== undefined) next.goalId = patch.goalId;
  if (patch.listKind !== undefined) next.listKind = patch.listKind;
  if (patch.tags !== undefined) next.tags = normalizeTags(patch.tags);
  if (patch.notes !== undefined) next.notes = patch.notes;
  if (patch.recurrence !== undefined) next.recurrence = patch.recurrence;
  if (patch.status !== undefined) {
    next.status = patch.status;
    next.doneAt = patch.status === 'done' ? new Date().toISOString() : null;
  }
  return next;
}

function taskInQuery(task: Task, query: TasksQuery): boolean {
  const date = task.dueDate ?? task.startAt?.slice(0, 10) ?? null;
  if (query.date) {
    const matchesDay = task.dueDate === query.date || task.startAt?.slice(0, 10) === query.date;
    if (!matchesDay) return false;
  } else if (query.from && query.to) {
    const inRange = Boolean(date && date >= query.from && date <= query.to);
    if (!inRange) return false;
  }
  if (query.sphereId !== undefined && task.sphereId !== query.sphereId) return false;
  if (query.projectId !== undefined && task.projectId !== query.projectId) return false;
  if (query.resource !== undefined && task.resource !== query.resource) return false;
  if (query.listKind !== undefined && task.listKind !== query.listKind) return false;
  if (query.status !== undefined && query.status !== 'all' && task.status !== query.status) {
    return false;
  }
  if (query.status === undefined && task.status === 'cancelled') return false;
  const needle = query.q?.trim().toLocaleLowerCase('ru-RU');
  if (needle) {
    const haystack = `${task.title} ${task.notes} ${task.tags.join(',')}`.toLocaleLowerCase('ru-RU');
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

function optimisticList(payload: TasksPayload, items: Task[]): TasksPayload {
  return { ...payload, items, counts: countTasks(items) };
}

/* ── Хук задач ───────────────────────────────────────────── */

export interface TasksController {
  data: TasksPayload | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  create: (input: TaskInput) => Promise<Task | null>;
  update: (id: number, patch: TaskPatch) => Promise<Task | null>;
  toggle: (task: Task, done: boolean) => Promise<CompleteResult | null>;
  remove: (task: Task) => Promise<boolean>;
}

export function useTasks(query: TasksQuery): TasksController {
  const [data, setData] = useState<TasksPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);
  const queryRef = useRef(query);
  queryRef.current = query;
  const queryKey = JSON.stringify(query);

  useEffect(() => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fetchTasks(JSON.parse(queryKey) as TasksQuery)
      .then((payload) => {
        if (sequence.current === current) setData(payload);
      })
      .catch((problem: unknown) => {
        if (sequence.current === current) {
          setError(apiErrorMessage(problem, 'Не удалось загрузить задачи'));
        }
      })
      .finally(() => {
        if (sequence.current === current) setLoading(false);
      });
    return () => {
      sequence.current += 1;
    };
  }, [queryKey, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  const create = useCallback(async (input: TaskInput): Promise<Task | null> => {
    const currentQuery = queryRef.current;
    const tempId = -Date.now();
    const temp: Task = {
      id: tempId,
      title: input.title,
      sphereId: input.sphereId ?? null,
      sphereName: null,
      projectId: input.projectId ?? null,
      projectName: null,
      goalId: input.goalId ?? null,
      listKind: input.listKind ?? 'work',
      tags: normalizeTags(input.tags),
      dueDate: input.dueDate ?? null,
      startAt: input.startAt ?? null,
      endAt: input.endAt ?? null,
      resource: input.resource ?? 'important',
      status: 'todo',
      recurrence: input.recurrence ?? '',
      notes: input.notes ?? '',
      doneAt: null,
      created: new Date().toISOString(),
    };
    setData((previous) => {
      if (!previous) return previous;
      if (!taskInQuery(temp, currentQuery)) return previous;
      return optimisticList(previous, [...previous.items, temp]);
    });

    try {
      const saved = await createTask(input);
      setData((previous) => {
        if (!previous) return previous;
        const without = previous.items.filter((item) => item.id !== tempId && item.id !== saved.id);
        return optimisticList(
          previous,
          taskInQuery(saved, currentQuery) ? [...without, saved] : without,
        );
      });
      return saved;
    } catch (problem: unknown) {
      setData((previous) =>
        previous
          ? optimisticList(
              previous,
              previous.items.filter((item) => item.id !== tempId),
            )
          : previous,
      );
      toast(apiErrorMessage(problem, 'Не удалось добавить задачу'), 'error');
      return null;
    }
  }, []);

  const update = useCallback(async (id: number, patch: TaskPatch): Promise<Task | null> => {
    const currentQuery = queryRef.current;
    setData((previous) => {
      if (!previous) return previous;
      return optimisticList(
        previous,
        previous.items.map((item) => (item.id === id ? mergeTask(item, patch) : item)),
      );
    });
    try {
      const saved = await patchTask(id, patch);
      setData((previous) => {
        if (!previous) return previous;
        const without = previous.items.filter((item) => item.id !== saved.id);
        return optimisticList(
          previous,
          taskInQuery(saved, currentQuery) ? [...without, saved] : without,
        );
      });
      return saved;
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось сохранить задачу'), 'error');
      reload();
      return null;
    }
  }, [reload]);

  const toggle = useCallback(
    async (task: Task, done: boolean): Promise<CompleteResult | null> => {
      setData((previous) => {
        if (!previous) return previous;
        return optimisticList(
          previous,
          previous.items.map((item) =>
            item.id === task.id
              ? {
                  ...item,
                  status: done ? 'done' : 'todo',
                  doneAt: done ? new Date().toISOString() : null,
                }
              : item,
          ),
        );
      });
      try {
        const result = await completeTask(task.id, done);
        setData((previous) => {
          if (!previous) return previous;
          const without = previous.items.filter(
            (item) => item.id !== result.task.id && (!result.next || item.id !== result.next.id),
          );
          const items = [...without];
          if (taskInQuery(result.task, queryRef.current)) items.push(result.task);
          if (result.next && taskInQuery(result.next, queryRef.current)) items.push(result.next);
          return optimisticList(previous, items);
        });
        return result;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось изменить задачу'), 'error');
        reload();
        return null;
      }
    },
    [reload],
  );

  const remove = useCallback(
    async (task: Task): Promise<boolean> => {
      setData((previous) =>
        previous
          ? optimisticList(
              previous,
              previous.items.filter((item) => item.id !== task.id),
            )
          : previous,
      );
      try {
        await deleteTask(task.id);
        return true;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось удалить задачу'), 'error');
        reload();
        return false;
      }
    },
    [reload],
  );

  return { data, loading, error, reload, create, update, toggle, remove };
}

/* ── Хук инбокса ─────────────────────────────────────────── */

export interface InboxController {
  data: InboxPayload | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  add: (text: string) => Promise<boolean>;
  convert: (
    item: InboxItem,
    to: InboxConvertTo,
    payload?: Record<string, unknown>,
  ) => Promise<boolean>;
  remove: (item: InboxItem) => Promise<boolean>;
}

export function useInbox(): InboxController {
  const [data, setData] = useState<InboxPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fetchInbox('new')
      .then((payload) => {
        if (sequence.current === current) setData(payload);
      })
      .catch((problem: unknown) => {
        if (sequence.current === current) {
          setError(apiErrorMessage(problem, 'Не удалось загрузить инбокс'));
        }
      })
      .finally(() => {
        if (sequence.current === current) setLoading(false);
      });
    return () => {
      sequence.current += 1;
    };
  }, [tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  const add = useCallback(async (text: string): Promise<boolean> => {
    const temp: InboxItem = {
      id: -Date.now(),
      text,
      status: 'new',
      convertedType: '',
      convertedId: null,
      created: new Date().toISOString(),
    };
    setData((previous) => {
      if (!previous) return previous;
      const items = [temp, ...previous.items];
      return {
        items,
        counts: { new: items.length, processed: previous.counts.processed, total: items.length },
      };
    });
    try {
      const saved = await createInbox(text);
      setData((previous) => {
        if (!previous) return previous;
        return {
          ...previous,
          items: previous.items.map((item) => (item.id === temp.id ? saved : item)),
        };
      });
      return true;
    } catch (problem: unknown) {
      setData((previous) =>
        previous
          ? {
              ...previous,
              items: previous.items.filter((item) => item.id !== temp.id),
              counts: { ...previous.counts, new: previous.items.length - 1, total: previous.items.length - 1 },
            }
          : previous,
      );
      toast(apiErrorMessage(problem, 'Не удалось сохранить запись'), 'error');
      return false;
    }
  }, []);

  const dropLocally = useCallback((id: number) => {
    setData((previous) => {
      if (!previous) return previous;
      const items = previous.items.filter((item) => item.id !== id);
      return {
        ...previous,
        items,
        counts: { ...previous.counts, new: items.length, total: items.length },
      };
    });
  }, []);

  const convert = useCallback(
    async (
      item: InboxItem,
      to: InboxConvertTo,
      payload?: Record<string, unknown>,
    ): Promise<boolean> => {
      dropLocally(item.id);
      try {
        await convertInbox(item.id, to, payload);
        return true;
      } catch (problem: unknown) {
        if (apiStatus(problem) === 409) {
          toast('Запись уже разобрана', 'default');
          return true;
        }
        toast(apiErrorMessage(problem, 'Не удалось разобрать запись'), 'error');
        reload();
        return false;
      }
    },
    [dropLocally, reload],
  );

  const remove = useCallback(
    async (item: InboxItem): Promise<boolean> => {
      dropLocally(item.id);
      try {
        await deleteInbox(item.id);
        return true;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось удалить запись'), 'error');
        reload();
        return false;
      }
    },
    [dropLocally, reload],
  );

  return { data, loading, error, reload, add, convert, remove };
}

/* ── Хук списка проектов ─────────────────────────────────── */

export interface ProjectsController {
  data: ProjectsPayload | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  create: (input: ProjectInput) => Promise<Project | null>;
  update: (id: number, patch: Partial<ProjectInput>) => Promise<Project | null>;
}

export function useProjects(query: {
  status?: ProjectStatus;
  sphereId?: number;
  q?: string;
}): ProjectsController {
  const [data, setData] = useState<ProjectsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);
  const queryKey = JSON.stringify(query);

  useEffect(() => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fetchProjects(JSON.parse(queryKey) as { status?: ProjectStatus; sphereId?: number; q?: string })
      .then((payload) => {
        if (sequence.current === current) setData(payload);
      })
      .catch((problem: unknown) => {
        if (sequence.current === current) {
          setError(apiErrorMessage(problem, 'Не удалось загрузить проекты'));
        }
      })
      .finally(() => {
        if (sequence.current === current) setLoading(false);
      });
    return () => {
      sequence.current += 1;
    };
  }, [queryKey, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  const create = useCallback(
    async (input: ProjectInput): Promise<Project | null> => {
      const temp: Project = {
        id: -Date.now(),
        name: input.name,
        sphereId: input.sphereId ?? null,
        sphereName: null,
        goalId: input.goalId ?? null,
        deadline: input.deadline ?? null,
        status: input.status ?? 'idea',
        outcome: input.outcome ?? '',
        description: input.description ?? '',
        created: new Date().toISOString(),
        updated: null,
        stepsTotal: 0,
        stepsDone: 0,
        tasksTotal: 0,
        tasksDone: 0,
      };
      setData((previous) => {
        if (!previous) return previous;
        const items = [temp, ...previous.items];
        return normalizeProjects({ items });
      });
      try {
        const saved = await createProject(input);
        setData((previous) => {
          if (!previous) return previous;
          return normalizeProjects({
            items: previous.items.map((item) => (item.id === temp.id ? saved : item)),
          });
        });
        return saved;
      } catch (problem: unknown) {
        setData((previous) =>
          previous
            ? normalizeProjects({ items: previous.items.filter((item) => item.id !== temp.id) })
            : previous,
        );
        toast(apiErrorMessage(problem, 'Не удалось создать проект'), 'error');
        return null;
      }
    },
    [],
  );

  const update = useCallback(
    async (id: number, patch: Partial<ProjectInput>): Promise<Project | null> => {
      setData((previous) => {
        if (!previous) return previous;
        return {
          ...previous,
          items: previous.items.map((item) =>
            item.id === id ? { ...item, ...patch, deadline: patch.deadline ?? item.deadline } : item,
          ),
        };
      });
      try {
        const saved = await patchProject(id, patch);
        setData((previous) =>
          previous
            ? {
                ...previous,
                items: previous.items.map((item) => (item.id === saved.id ? saved : item)),
              }
            : previous,
        );
        return saved;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось сохранить проект'), 'error');
        reload();
        return null;
      }
    },
    [reload],
  );

  return { data, loading, error, reload, create, update };
}

/* ── Хук одного проекта для drawer ───────────────────────── */

export interface ProjectDetailController {
  detail: ProjectDetail | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  updateProject: (patch: Partial<ProjectInput>) => Promise<Project | null>;
  addStep: (title: string) => Promise<boolean>;
  toggleStep: (step: ProjectStep) => Promise<boolean>;
  removeStep: (step: ProjectStep) => Promise<void>;
  addNote: (input: { kind: 'note' | 'link'; title?: string; body?: string; url?: string }) => Promise<boolean>;
  removeNote: (note: ProjectNote) => Promise<void>;
  addTask: (input: TaskInput) => Promise<Task | null>;
  toggleTask: (task: Task, done: boolean) => Promise<void>;
}

export function useProjectDetail(projectId: number | null): ProjectDetailController {
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    if (projectId === null) {
      setDetail(null);
      setError(null);
      return;
    }
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fetchProject(projectId)
      .then((payload) => {
        if (sequence.current === current) setDetail(payload);
      })
      .catch((problem: unknown) => {
        if (sequence.current === current) {
          setError(apiErrorMessage(problem, 'Не удалось открыть проект'));
        }
      })
      .finally(() => {
        if (sequence.current === current) setLoading(false);
      });
    return () => {
      sequence.current += 1;
    };
  }, [projectId, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  const updateProject = useCallback(
    async (patch: Partial<ProjectInput>): Promise<Project | null> => {
      if (projectId === null) return null;
      setDetail((previous) =>
        previous ? { ...previous, project: { ...previous.project, ...patch } } : previous,
      );
      try {
        const saved = await patchProject(projectId, patch);
        setDetail((previous) => (previous ? { ...previous, project: saved } : previous));
        return saved;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось сохранить проект'), 'error');
        reload();
        return null;
      }
    },
    [projectId, reload],
  );

  const addStep = useCallback(
    async (title: string): Promise<boolean> => {
      if (projectId === null) return false;
      const temp: ProjectStep = {
        id: -Date.now(),
        projectId,
        title,
        done: false,
        sort: Number.MAX_SAFE_INTEGER,
      };
      setDetail((previous) =>
        previous
          ? {
              ...previous,
              steps: [...previous.steps, temp],
              project: {
                ...previous.project,
                stepsTotal: previous.project.stepsTotal + 1,
              },
            }
          : previous,
      );
      try {
        const saved = await createProjectStep(projectId, title);
        setDetail((previous) =>
          previous
            ? {
                ...previous,
                steps: previous.steps.map((step) => (step.id === temp.id ? saved : step)),
              }
            : previous,
        );
        return true;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось добавить этап'), 'error');
        reload();
        return false;
      }
    },
    [projectId, reload],
  );

  const toggleStep = useCallback(
    async (step: ProjectStep): Promise<boolean> => {
      const done = !step.done;
      setDetail((previous) =>
        previous
          ? {
              ...previous,
              steps: previous.steps.map((item) =>
                item.id === step.id ? { ...item, done } : item,
              ),
              project: {
                ...previous.project,
                stepsDone: previous.project.stepsDone + (done ? 1 : -1),
              },
            }
          : previous,
      );
      try {
        const saved = await patchProjectStep(step.id, { done });
        setDetail((previous) =>
          previous
            ? {
                ...previous,
                steps: previous.steps.map((item) => (item.id === saved.id ? saved : item)),
              }
            : previous,
        );
        return true;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось изменить этап'), 'error');
        reload();
        return false;
      }
    },
    [reload],
  );

  const removeStep = useCallback(
    async (step: ProjectStep): Promise<void> => {
      setDetail((previous) =>
        previous
          ? {
              ...previous,
              steps: previous.steps.filter((item) => item.id !== step.id),
              project: {
                ...previous.project,
                stepsTotal: Math.max(0, previous.project.stepsTotal - 1),
                stepsDone: Math.max(0, previous.project.stepsDone - (step.done ? 1 : 0)),
              },
            }
          : previous,
      );
      try {
        await deleteProjectStep(step.id);
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось удалить этап'), 'error');
        reload();
      }
    },
    [reload],
  );

  const addNote = useCallback(
    async (input: {
      kind: 'note' | 'link';
      title?: string;
      body?: string;
      url?: string;
    }): Promise<boolean> => {
      if (projectId === null) return false;
      const temp: ProjectNote = {
        id: -Date.now(),
        projectId,
        kind: input.kind,
        title: input.title ?? '',
        body: input.body ?? '',
        url: input.url ?? '',
        created: new Date().toISOString(),
      };
      setDetail((previous) =>
        previous ? { ...previous, notes: [temp, ...previous.notes] } : previous,
      );
      try {
        const saved = await createProjectNote(projectId, input);
        setDetail((previous) =>
          previous
            ? {
                ...previous,
                notes: previous.notes.map((note) => (note.id === temp.id ? saved : note)),
              }
            : previous,
        );
        return true;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось добавить заметку'), 'error');
        reload();
        return false;
      }
    },
    [projectId, reload],
  );

  const removeNote = useCallback(
    async (note: ProjectNote): Promise<void> => {
      setDetail((previous) =>
        previous
          ? { ...previous, notes: previous.notes.filter((item) => item.id !== note.id) }
          : previous,
      );
      try {
        await deleteProjectNote(note.id);
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось удалить заметку'), 'error');
        reload();
      }
    },
    [reload],
  );

  const addTask = useCallback(
    async (input: TaskInput): Promise<Task | null> => {
      if (projectId === null) return null;
      const tempId = -Date.now();
      const temp: Task = {
        id: tempId,
        title: input.title,
        sphereId: input.sphereId ?? null,
        sphereName: null,
        projectId,
        projectName: null,
        goalId: input.goalId ?? null,
        listKind: input.listKind ?? 'work',
        tags: normalizeTags(input.tags),
        dueDate: input.dueDate ?? todayIso(),
        startAt: input.startAt ?? null,
        endAt: input.endAt ?? null,
        resource: input.resource ?? 'important',
        status: 'todo',
        recurrence: input.recurrence ?? '',
        notes: input.notes ?? '',
        doneAt: null,
        created: new Date().toISOString(),
      };
      setDetail((previous) => {
        if (!previous) return previous;
        const items = [...previous.tasks.items, temp];
        return {
          ...previous,
          tasks: { items, counts: countTasks(items) },
          project: {
            ...previous.project,
            tasksTotal: previous.project.tasksTotal + 1,
          },
        };
      });
      try {
        const saved = await createTask({ ...input, projectId });
        setDetail((previous) => {
          if (!previous) return previous;
          const without = previous.tasks.items.filter((item) => item.id !== tempId);
          const items = [...without, saved];
          return {
            ...previous,
            tasks: { items, counts: countTasks(items) },
          };
        });
        return saved;
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось добавить задачу'), 'error');
        reload();
        return null;
      }
    },
    [projectId, reload],
  );

  const toggleTask = useCallback(
    async (task: Task, done: boolean): Promise<void> => {
      setDetail((previous) => {
        if (!previous) return previous;
        const items = previous.tasks.items.map((item) =>
          item.id === task.id
            ? {
                ...item,
                status: (done ? 'done' : 'todo') as TaskStatus,
                doneAt: done ? new Date().toISOString() : null,
              }
            : item,
        );
        return {
          ...previous,
          tasks: { items, counts: countTasks(items) },
          project: {
            ...previous.project,
            tasksDone: Math.max(0, previous.project.tasksDone + (done ? 1 : -1)),
          },
        };
      });
      try {
        const result = await completeTask(task.id, done);
        setDetail((previous) => {
          if (!previous) return previous;
          const without = previous.tasks.items.filter(
            (item) => item.id !== result.task.id && (!result.next || item.id !== result.next.id),
          );
          const items = [...without, result.task];
          if (result.next) items.push(result.next);
          return { ...previous, tasks: { items, counts: countTasks(items) } };
        });
      } catch (problem: unknown) {
        toast(apiErrorMessage(problem, 'Не удалось изменить задачу'), 'error');
        reload();
      }
    },
    [reload],
  );

  return {
    detail,
    loading,
    error,
    reload,
    updateProject,
    addStep,
    toggleStep,
    removeStep,
    addNote,
    removeNote,
    addTask,
    toggleTask,
  };
}
