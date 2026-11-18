import '../styles/pages-compass.css';
import { useEffect, useMemo, useState } from 'react';
import { pluralRu } from '../lib/format';
import { useSpheres } from '../api/compass';
import { useProjects, useTasks } from '../api/tasks';
import type { Resource, Task, TaskInput } from '../api/tasks';
import { TaskComposer } from '../components/pages-compass/TaskComposer';
import { TaskEditor } from '../components/pages-compass/TaskEditor';
import { WeekCalendar, WeekCard } from '../components/pages-compass/WeekCalendar';
import { Button, Icon, Menu, Segmented, Select } from '../components/ui';
import {
  InlineError,
  PageHead,
  SkeletonRows,
  WEEKDAY_SHORT,
  addIsoDays,
  minutesOf,
  mondayOf,
  shortDate,
  todayIso,
} from '../components/pages-compass/shared';

const RESOURCE_ORDER: Record<Resource, number> = { must: 0, important: 1, low: 2 };

function taskDay(task: Task): string | null {
  return task.dueDate ?? task.startAt?.slice(0, 10) ?? null;
}

function sortDayTasks(left: Task, right: Task): number {
  const leftTime = minutesOf(left.startAt);
  const rightTime = minutesOf(right.startAt);
  if (leftTime !== null && rightTime !== null) return leftTime - rightTime;
  if (leftTime !== null) return -1;
  if (rightTime !== null) return 1;
  const byResource = RESOURCE_ORDER[left.resource] - RESOURCE_ORDER[right.resource];
  return byResource !== 0 ? byResource : left.title.localeCompare(right.title, 'ru');
}

export function Week() {
  const today = todayIso();
  const [anchor, setAnchor] = useState(() => mondayOf(today));
  const [view, setView] = useState<'kanban' | 'grid'>('kanban');
  const [sphereFilter, setSphereFilter] = useState('all');
  const [editorTask, setEditorTask] = useState<Task | null | undefined>(undefined);
  const [composerDay, setComposerDay] = useState<string | null>(null);
  const [composerFocus, setComposerFocus] = useState(0);

  const days = useMemo(
    () => Array.from({ length: 7 }, (_, index) => addIsoDays(anchor, index)),
    [anchor],
  );
  const end = days[6];
  const tasks = useTasks({
    view: 'week',
    from: anchor,
    to: end,
    sphereId: sphereFilter === 'all' ? undefined : Number(sphereFilter),
  });
  const spheres = useSpheres('active');
  const projects = useProjects({});

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key.toLowerCase() !== 'n') return;
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (editorTask !== undefined) return;
      event.preventDefault();
      setComposerDay(days.includes(today) ? today : days[0] ?? anchor);
      setComposerFocus((value) => value + 1);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [anchor, days, editorTask, today]);

  const items = tasks.data?.items ?? [];
  const projectOptions = (projects.data?.items ?? []).map((project) => ({
    id: project.id,
    name: project.name,
  }));

  function dayTasks(day: string): Task[] {
    return items.filter((task) => taskDay(task) === day).sort(sortDayTasks);
  }

  function menuFor(task: Task) {
    return [
      {
        id: 'edit',
        label: 'Редактировать',
        icon: 'edit' as const,
        onSelect: () => setEditorTask(task),
      },
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

  async function saveTask(input: TaskInput): Promise<Task | null> {
    if (editorTask) return tasks.update(editorTask.id, input);
    return tasks.create(input);
  }

  const weekNumber = items.filter((task) => task.startAt).length;
  const isCurrentWeek = anchor === mondayOf(today);

  return (
    <div className="pc-page">
      <PageHead
        title="Неделя"
        date={`${shortDate(anchor)} — ${shortDate(end)}`}
        note={`${items.length} ${pluralRu(items.length, ['задача', 'задачи', 'задач'])} · ${weekNumber} ${pluralRu(weekNumber, ['событие', 'события', 'событий'])} с временем${isCurrentWeek ? ' · эта неделя' : ''}`}
        actions={
          <>
            <Button
              variant="ghost"
              size="sm"
              leadingIcon="chevron-left"
              aria-label="Предыдущая неделя"
              title="Предыдущая неделя"
              onClick={() => setAnchor((value) => addIsoDays(value, -7))}
            />
            <Button
              variant="ghost"
              size="sm"
              leadingIcon="chevron-right"
              aria-label="Следующая неделя"
              title="Следующая неделя"
              onClick={() => setAnchor((value) => addIsoDays(value, 7))}
            />
            {!isCurrentWeek ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setAnchor(mondayOf(today))}
              >
                Эта неделя
              </Button>
            ) : null}
          </>
        }
      />

      <div className="pc-toolbar">
        <Segmented
          options={[
            { value: 'kanban', label: 'Канбан' },
            { value: 'grid', label: 'Сетка' },
          ]}
          value={view}
          onChange={(value) => setView(value as 'kanban' | 'grid')}
          ariaLabel="Вид недели"
        />
        <div className="pc-toolbar__right">
          <Select
            size="sm"
            value={sphereFilter}
            onChange={(event) => setSphereFilter(event.target.value)}
            options={[
              { value: 'all', label: 'Все сферы' },
              ...spheres.items.map((sphere) => ({
                value: String(sphere.id),
                label: sphere.name,
              })),
            ]}
            aria-label="Фильтр по сфере"
          />
        </div>
      </div>

      {tasks.loading && !tasks.data ? (
        <SkeletonRows rows={5} label="Загрузка недели" />
      ) : tasks.error ? (
        <InlineError text={tasks.error} onRetry={tasks.reload} />
      ) : view === 'kanban' ? (
        <div className="pc-kanban">
          {days.map((day, index) => {
            const list = dayTasks(day);
            const isToday = day === today;
            return (
              <section
                className={isToday ? 'pc-column is-today' : 'pc-column'}
                key={day}
                aria-labelledby={`week-day-${day}`}
              >
                <header className="pc-column__head">
                  <h2 className="pc-column__day" id={`week-day-${day}`}>
                    <span className="pc-column__weekday">{WEEKDAY_SHORT[index]}</span>
                    <span className="pc-column__date">{Number(day.slice(8, 10))}</span>
                    {isToday ? <span className="pc-column__today">сегодня</span> : null}
                  </h2>
                  <span className="pc-column__count">{list.length}</span>
                </header>
                <div className="pc-column__list">
                  {list.length === 0 ? (
                    <p className="pc-column__empty">Пусто</p>
                  ) : (
                    list.map((task) => (
                      <WeekCard
                        key={task.id}
                        task={task}
                        onOpen={setEditorTask}
                        onToggle={(item, done) => {
                          void tasks.toggle(item, done);
                        }}
                        menu={
                          <Menu
                            items={menuFor(task)}
                            label={`Действия: ${task.title}`}
                            triggerClassName="pc-row-action"
                          />
                        }
                      />
                    ))
                  )}
                </div>

                {composerDay === day ? (
                  <div className="pc-column__composer">
                    <TaskComposer
                      compact
                      autoFocus
                      date={day}
                      onAdd={tasks.create}
                      spheres={spheres.items}
                      projects={projectOptions}
                      focusSignal={composerFocus}
                    />
                    <button
                      type="button"
                      className="pc-column__close"
                      aria-label="Закрыть быстрое добавление"
                      title="Закрыть"
                      onClick={() => setComposerDay(null)}
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="pc-column__add"
                    onClick={() => {
                      setComposerDay(day);
                      setComposerFocus((value) => value + 1);
                    }}
                  >
                    <Icon name="plus" size={15} />
                    Задача
                  </button>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        <WeekCalendar
          days={days}
          tasks={items}
          onOpen={setEditorTask}
          onToggle={(task, done) => {
            void tasks.toggle(task, done);
          }}
        />
      )}

      {editorTask !== undefined ? (
        <TaskEditor
          open
          task={editorTask}
          defaults={{ dueDate: editorTask?.dueDate ?? today, resource: 'important' }}
          spheres={spheres.items}
          projects={projectOptions}
          onClose={() => setEditorTask(undefined)}
          onSave={saveTask}
          onDelete={
            editorTask
              ? async () => {
                  return tasks.remove(editorTask);
                }
              : undefined
          }
        />
      ) : null}
    </div>
  );
}
