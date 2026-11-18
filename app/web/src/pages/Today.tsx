import '../styles/pages-compass.css';
import { useEffect, useMemo, useState } from 'react';
import { markHabit, useSection } from '../api/sections';
import type { SectionHabit } from '../api/sections';
import { useSpheres } from '../api/compass';
import { useProjects, useTasks } from '../api/tasks';
import type { Resource, Task, TaskInput } from '../api/tasks';
import { apiErrorMessage } from '../api/errors';
import { toast } from '../lib/toast';
import { navigate } from '../lib/router';
import { TaskComposer } from '../components/pages-compass/TaskComposer';
import { TaskEditor } from '../components/pages-compass/TaskEditor';
import {
  Button,
  Checkbox,
  EmptyState,
  Icon,
  Menu,
  Section,
} from '../components/ui';
import {
  InlineError,
  PageHead,
  RESOURCE_LABEL,
  ResourceDot,
  SkeletonRows,
  SphereDot,
  addIsoDays,
  dateWithWeekday,
  nextEventSlot,
  longToday,
  minutesOf,
  shortDate,
  timeOf,
  timeRange,
  todayIso,
} from '../components/pages-compass/shared';

const GROUPS: Resource[] = ['must', 'important', 'low'];

function sortByTime(left: Task, right: Task): number {
  return (minutesOf(left.startAt) ?? 0) - (minutesOf(right.startAt) ?? 0);
}

function taskMeta(task: Task): string[] {
  const parts: string[] = [];
  if (task.sphereName) parts.push(task.sphereName);
  if (task.projectName) parts.push(task.projectName);
  if (task.dueDate && task.dueDate !== todayIso()) parts.push(`до ${shortDate(task.dueDate)}`);
  if (task.tags.length > 0) parts.push(task.tags.slice(0, 3).join(', '));
  return parts;
}

export function Today() {
  const today = useMemo(() => todayIso(), []);
  const tasks = useTasks({ date: today, view: 'day' });
  const spheres = useSpheres('active');
  const projects = useProjects({});
  const habits = useSection('habits', 7);
  const [editorTask, setEditorTask] = useState<Task | null | undefined>(undefined);
  const editorDefaults = useMemo(
    () => (editorTask === null ? { dueDate: today, resource: 'important' as Resource, ...nextEventSlot() } : { dueDate: today }),
    [editorTask, today],
  );
  const [focusSignal, setFocusSignal] = useState(0);
  const [habitOverrides, setHabitOverrides] = useState<Record<number, boolean>>({});

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key.toLowerCase() !== 'n') return;
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (editorTask !== undefined) return;
      event.preventDefault();
      setFocusSignal((value) => value + 1);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [editorTask]);

  const items = tasks.data?.items ?? [];
  const hard = items.filter((task) => task.startAt && task.status !== 'done').sort(sortByTime);
  const done = items.filter((task) => task.status === 'done');
  const flexible = items.filter((task) => !task.startAt && task.status === 'todo');
  const grouped: Record<Resource, Task[]> = {
    must: flexible.filter((task) => task.resource === 'must'),
    important: flexible.filter((task) => task.resource === 'important'),
    low: flexible.filter((task) => task.resource === 'low'),
  };
  const projectOptions = (projects.data?.items ?? []).map((project) => ({
    id: project.id,
    name: project.name,
  }));
  const counts = tasks.data?.counts;

  function openTask(task: Task) {
    setEditorTask(task);
  }

  async function saveTask(input: TaskInput): Promise<Task | null> {
    if (editorTask) return tasks.update(editorTask.id, input);
    return tasks.create(input);
  }

  async function deleteTask(): Promise<boolean> {
    if (!editorTask) return false;
    return tasks.remove(editorTask);
  }

  function taskMenu(task: Task) {
    return [
      {
        id: 'edit',
        label: 'Редактировать',
        icon: 'edit' as const,
        onSelect: () => openTask(task),
      },
      ...(task.startAt
        ? []
        : [
            {
              id: 'tomorrow',
              label: 'Перенести на завтра',
              icon: 'calendar' as const,
              onSelect: () => {
                void tasks.update(task.id, { dueDate: addIsoDays(today, 1) });
              },
            },
          ]),
      {
        id: 'delete',
        label: 'Удалить',
        icon: 'trash' as const,
        danger: true,
        separatorBefore: true,
        onSelect: () => {
          void tasks.remove(task);
        },
      },
    ];
  }

  async function toggleHabit(habit: SectionHabit) {
    const last = habit.marks.length - 1;
    const current = habitOverrides[habit.id] ?? habit.marks[last] === true;
    setHabitOverrides((previous) => ({ ...previous, [habit.id]: !current }));
    try {
      await markHabit(habit.id, current ? 0 : 1);
      habits.reload();
    } catch (problem: unknown) {
      setHabitOverrides((previous) => ({ ...previous, [habit.id]: current }));
      toast(apiErrorMessage(problem, 'Не удалось отметить привычку'), 'error');
    }
  }

  return (
    <div className="pc-page">
      <PageHead
        title="Сегодня"
        date={longToday()}
        note={
          counts
            ? `${counts.must} обязательно · ${counts.important} важно · ${counts.done} сделано`
            : `${dateWithWeekday(today)} · план дня`
        }
        actions={
          <>
            <Button
              variant="ghost"
              size="sm"
              leadingIcon="plus"
              onClick={() => setEditorTask(null)}
            >
              Событие
            </Button>
            <Button
              variant="secondary"
              size="sm"
              leadingIcon="plus"
              onClick={() => setFocusSignal((value) => value + 1)}
            >
              Задача
            </Button>
          </>
        }
      />

      <div className="pc-today">
        <section className="pc-panel pc-today__hard" aria-labelledby="today-hard-title">
          <div className="pc-block__head">
            <h2 className="pc-block__title" id="today-hard-title">
              Жёсткие события
            </h2>
            <span className="pc-block__count">{hard.length}</span>
          </div>
          {tasks.loading && !tasks.data ? (
            <SkeletonRows rows={3} label="Загрузка событий" />
          ) : tasks.error ? (
            <InlineError text={tasks.error} onRetry={tasks.reload} />
          ) : hard.length === 0 ? (
            <EmptyState
              title="Событий с временем нет"
              description="Добавьте встречу, тренировку или блок работы — время появится в таймлайне."
              action={
                <Button variant="secondary" size="sm" leadingIcon="plus" onClick={() => setEditorTask(null)}>
                  Добавить событие
                </Button>
              }
            />
          ) : (
            <ol className="pc-timeline">
              {hard.map((task) => (
                <li className="pc-timeline__item" key={task.id}>
                  <time className="pc-timeline__time" dateTime={task.startAt ?? undefined}>
                    {timeOf(task.startAt)}
                  </time>
                  <span className="pc-timeline__rule" aria-hidden="true" />
                  <div className="pc-timeline__body">
                    <button
                      type="button"
                      className="pc-timeline__title"
                      onClick={() => openTask(task)}
                    >
                      {task.title}
                    </button>
                    <span className="pc-timeline__meta">
                      {timeRange(task.startAt, task.endAt)}
                      {task.sphereName ? (
                        <>
                          <span aria-hidden="true"> · </span>
                          <SphereDot color={null} />
                          {task.sphereName}
                        </>
                      ) : null}
                      {task.projectName ? ` · ${task.projectName}` : null}
                    </span>
                  </div>
                  <Checkbox
                    size="sm"
                    checked={task.status === 'done'}
                    onChange={() => {
                      void tasks.toggle(task, task.status !== 'done');
                    }}
                    label={
                      <span className="visually-hidden">Отметить «{task.title}» выполненной</span>
                    }
                  />
                  <Menu
                    items={taskMenu(task)}
                    label={`Действия: ${task.title}`}
                    triggerClassName="pc-row-action"
                  />
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="pc-panel pc-today__flex" aria-labelledby="today-flex-title">
          <div className="pc-block__head">
            <h2 className="pc-block__title" id="today-flex-title">
              Гибкие задачи
            </h2>
            <span className="pc-block__count">{grouped.must.length + grouped.important.length + grouped.low.length}</span>
          </div>

          <TaskComposer
            date={today}
            onAdd={tasks.create}
            spheres={spheres.items}
            projects={projectOptions}
            focusSignal={focusSignal}
          />

          {tasks.loading && !tasks.data ? (
            <SkeletonRows rows={4} label="Загрузка задач" />
          ) : tasks.error ? (
            <InlineError text={tasks.error} onRetry={tasks.reload} />
          ) : flexible.length === 0 ? (
            <EmptyState
              title="Гибких задач нет"
              description="Начните с одной строки — Enter сохранит и оставит курсор в поле."
            />
          ) : (
            GROUPS.map((resource) => {
              const group = grouped[resource];
              if (group.length === 0) return null;
              return (
                <div className="pc-taskgroup" key={resource}>
                  <h3 className="pc-taskgroup__title">
                    <ResourceDot resource={resource} />
                    {RESOURCE_LABEL[resource]}
                    <span className="pc-taskgroup__count">{group.length}</span>
                  </h3>
                  {resource === 'must' && group.length > 3 ? (
                    <p className="pc-taskgroup__warning" role="status">
                      Рекомендуем не больше 2–3 обязательных. Сейчас {group.length} — это предупреждение, не блокировка.
                    </p>
                  ) : null}
                  <ul className="pc-tasks">
                    {group.map((task) => (
                      <li className="pc-task" key={task.id}>
                        <Checkbox
                          size="sm"
                          checked={false}
                          onChange={() => {
                            void tasks.toggle(task, true);
                          }}
                          label={
                            <span className="visually-hidden">
                              Отметить «{task.title}» выполненной
                            </span>
                          }
                        />
                        <button
                          type="button"
                          className="pc-task__main"
                          onClick={() => openTask(task)}
                        >
                          <span className="pc-task__title-row">
                            <ResourceDot resource={task.resource} />
                            <span className="pc-task__title">{task.title}</span>
                          </span>
                          {taskMeta(task).length > 0 ? (
                            <span className="pc-task__meta">{taskMeta(task).join(' · ')}</span>
                          ) : null}
                        </button>
                        <Menu
                          items={taskMenu(task)}
                          label={`Действия: ${task.title}`}
                          triggerClassName="pc-row-action"
                        />
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })
          )}

          {done.length > 0 ? (
            <details className="pc-done">
              <summary className="pc-done__summary">
                Сделано сегодня · {done.length}
                <Icon name="chevron-down" size={15} />
              </summary>
              <ul className="pc-tasks pc-tasks--done">
                {done.map((task) => (
                  <li className="pc-task" key={task.id}>
                    <Checkbox
                      size="sm"
                      checked
                      onChange={() => {
                        void tasks.toggle(task, false);
                      }}
                      label={<span className="visually-hidden">Вернуть «{task.title}»</span>}
                    />
                    <button type="button" className="pc-task__main" onClick={() => openTask(task)}>
                      <span className="pc-task__title is-done">{task.title}</span>
                      <span className="pc-task__meta">
                        {task.startAt ? timeRange(task.startAt, task.endAt) : 'закрыта'}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </section>
      </div>

      <Section
        title="Привычки"
        description="Неделя одним взглядом. Отметка — сегодняшний день."
        actions={
          <Button variant="ghost" size="sm" onClick={() => navigate('/habits')}>
            Все привычки
          </Button>
        }
      >
        {habits.loading && !habits.data ? (
          <SkeletonRows rows={2} label="Загрузка привычек" />
        ) : habits.error ? (
          <InlineError text={habits.error} onRetry={habits.reload} />
        ) : (habits.data?.habits?.length ?? 0) === 0 ? (
          <p className="pc-muted">Привычек пока нет — добавьте первую в разделе «Привычки».</p>
        ) : (
          <ul className="pc-habits">
            {habits.data?.habits?.map((habit) => {
              const last = habit.marks.length - 1;
              const todayDone = habitOverrides[habit.id] ?? habit.marks[last] === true;
              return (
                <li className="pc-habit" key={habit.id}>
                  <span className="pc-habit__name">{habit.name}</span>
                  <span className="pc-habit__meta">
                    {habit.doneWeek > habit.target
                      ? `${habit.doneWeek} отметок · цель ${habit.target}`
                      : `${habit.doneWeek} из ${habit.target} за неделю`}
                    {habit.streak > 1 ? ` · серия ${habit.streak} дн.` : ''}
                  </span>
                  <span className="pc-habit__week" aria-hidden="true">
                    {habit.marks.map((doneMark, index) => (
                      <span
                        className={
                          index === last && habitOverrides[habit.id] !== undefined
                            ? todayDone
                              ? 'pc-habit__dot is-on is-today'
                              : 'pc-habit__dot is-today'
                            : doneMark
                              ? 'pc-habit__dot is-on'
                              : 'pc-habit__dot'
                        }
                        key={`${habit.id}-${habit.week[index] ?? index}`}
                        title={habit.week[index] ?? ''}
                      />
                    ))}
                  </span>
                  <button
                    type="button"
                    className="pc-habit__check"
                    aria-pressed={todayDone}
                    aria-label={`${todayDone ? 'Снять отметку' : 'Отметить'} привычку ${habit.name}`}
                    title={todayDone ? 'Сделано сегодня' : 'Отметить сегодня'}
                    onClick={() => {
                      void toggleHabit(habit);
                    }}
                  >
                    <Icon name={todayDone ? 'check-circle' : 'check'} size={16} />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      {editorTask !== undefined ? (
        <TaskEditor
          open
          task={editorTask}
          defaults={editorDefaults}
          spheres={spheres.items}
          projects={projectOptions}
          onClose={() => setEditorTask(undefined)}
          onSave={saveTask}
          onDelete={editorTask ? deleteTask : undefined}
        />
      ) : null}
    </div>
  );
}
