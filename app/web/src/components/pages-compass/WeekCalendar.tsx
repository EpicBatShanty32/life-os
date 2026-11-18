import type { ReactNode } from 'react';
import type { Task } from '../../api/tasks';
import { Checkbox, Icon } from '../ui';
import { ResourceDot, WEEKDAY_SHORT, isOverdue, minutesOf, shortDate, timeOf } from './shared';

const START_HOUR = 8;
const END_HOUR = 22;
const SPAN_MINUTES = (END_HOUR - START_HOUR) * 60;
const HOURS = Array.from({ length: END_HOUR - START_HOUR }, (_, index) => START_HOUR + index);

function dayOf(task: Task): string | null {
  return task.dueDate ?? task.startAt?.slice(0, 10) ?? null;
}

interface PositionedEvent {
  task: Task;
  start: number;
  end: number;
  lane: number;
  lanes: number;
}

function positionEvents(tasks: Task[]): PositionedEvent[] {
  const raw = tasks
    .filter((task) => task.startAt)
    .map((task) => {
      const start = minutesOf(task.startAt) ?? START_HOUR * 60;
      const end = minutesOf(task.endAt) ?? start + 60;
      const clampedStart = Math.max(START_HOUR * 60, Math.min(start, END_HOUR * 60 - 30));
      const clampedEnd = Math.max(clampedStart + 30, Math.min(end, END_HOUR * 60));
      return { task, start: clampedStart, end: clampedEnd };
    })
    .sort((left, right) => left.start - right.start || left.end - right.end);

  const laneEnds: number[] = [];
  const positioned: PositionedEvent[] = [];
  for (const event of raw) {
    let lane = laneEnds.findIndex((end) => end <= event.start);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(event.end);
    } else {
      laneEnds[lane] = event.end;
    }
    positioned.push({ ...event, lane, lanes: 1 });
  }
  const lanes = Math.max(1, laneEnds.length);
  return positioned.map((event) => ({ ...event, lanes }));
}

export interface WeekCalendarProps {
  days: string[];
  tasks: Task[];
  onOpen: (task: Task) => void;
  onToggle: (task: Task, done: boolean) => void;
}

export function WeekCalendar({ days, tasks, onOpen, onToggle }: WeekCalendarProps) {
  const today = new Date();
  const todayIso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(
    today.getDate(),
  ).padStart(2, '0')}`;

  return (
    <div className="pc-cal__scroll">
      <div className="pc-cal">
        <div className="pc-cal__head">
          <span className="pc-cal__gutter" aria-hidden="true" />
          {days.map((day) => {
            const date = Number(day.slice(8, 10));
            const index = days.indexOf(day);
            return (
              <div
                className={day === todayIso ? 'pc-cal__dayhead is-today' : 'pc-cal__dayhead'}
                key={day}
              >
                <span>{WEEKDAY_SHORT[index]}</span>
                <strong>{date}</strong>
              </div>
            );
          })}
        </div>

        <div className="pc-cal__allday">
          <span className="pc-cal__allday-label">Весь день</span>
          {days.map((day) => {
            const items = tasks.filter((task) => !task.startAt && dayOf(task) === day);
            return (
              <div className="pc-cal__allday-cell" key={day}>
                {items.slice(0, 2).map((task) => (
                  <div className="pc-allday" key={task.id}>
                    <button
                      type="button"
                      className="pc-allday__main"
                      onClick={() => onOpen(task)}
                      title={task.title}
                    >
                      <ResourceDot resource={task.resource} />
                      <span>{task.title}</span>
                    </button>
                    <button
                      type="button"
                      className="pc-allday__check"
                      aria-label={`Отметить «${task.title}» выполненной`}
                      aria-pressed={task.status === 'done'}
                      onClick={() => onToggle(task, task.status !== 'done')}
                    >
                      <Icon name={task.status === 'done' ? 'check-circle' : 'check'} size={13} />
                    </button>
                  </div>
                ))}
                {items.length > 2 ? (
                  <span className="pc-allday__more" title={items.slice(2).map((task) => task.title).join(', ')}>
                    +{items.length - 2}
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>

        <div className="pc-cal__grid">
          <div className="pc-cal__hours" aria-hidden="true">
            {HOURS.map((hour) => (
              <span
                key={hour}
                style={{ top: `${((hour * 60 - START_HOUR * 60) / SPAN_MINUTES) * 100}%` }}
              >
                {String(hour).padStart(2, '0')}:00
              </span>
            ))}
          </div>

          {days.map((day) => {
            const dayTasks = tasks.filter((task) => dayOf(task) === day);
            const events = positionEvents(dayTasks);
            return (
              <div
                className={day === todayIso ? 'pc-cal__col is-today' : 'pc-cal__col'}
                key={day}
                role="group"
                aria-label={shortDate(day)}
              >
                {events.map((event) => {
                  const top = ((event.start - START_HOUR * 60) / SPAN_MINUTES) * 100;
                  const height = ((event.end - event.start) / SPAN_MINUTES) * 100;
                  const width = 100 / event.lanes;
                  return (
                    <div
                      className="pc-cal__event"
                      key={event.task.id}
                      data-resource={event.task.resource}
                      style={{
                        top: `${top}%`,
                        height: `${Math.max(height, 6)}%`,
                        left: `calc(${event.lane * width}% + 2px)`,
                        width: `calc(${width}% - 4px)`,
                      }}
                    >
                      <button
                        type="button"
                        className="pc-cal__event-title"
                        onClick={() => onOpen(event.task)}
                        title={`${timeOf(event.task.startAt)} ${event.task.title}`}
                      >
                        <span className="pc-cal__event-time">{timeOf(event.task.startAt)}</span>
                        <span className="pc-cal__event-text">{event.task.title}</span>
                      </button>
                      <button
                        type="button"
                        className="pc-cal__event-check"
                        aria-label={`Отметить «${event.task.title}» выполненной`}
                        aria-pressed={event.task.status === 'done'}
                        onClick={() => onToggle(event.task, event.task.status !== 'done')}
                      >
                        <Icon
                          name={event.task.status === 'done' ? 'check-circle' : 'check'}
                          size={12}
                        />
                      </button>
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function WeekCard({
  task,
  onOpen,
  onToggle,
  menu,
}: {
  task: Task;
  onOpen: (task: Task) => void;
  onToggle: (task: Task, done: boolean) => void;
  menu?: ReactNode;
}) {
  return (
    <article className={task.status === 'done' ? 'pc-card is-done' : 'pc-card'}>
      <div className="pc-card__check">
        <Checkbox
          size="sm"
          checked={task.status === 'done'}
          onChange={() => onToggle(task, task.status !== 'done')}
          label={<span className="visually-hidden">Отметить «{task.title}» выполненной</span>}
        />
      </div>
      <button type="button" className="pc-card__main" onClick={() => onOpen(task)}>
        <span className="pc-card__title">{task.title}</span>
        <span className="pc-card__meta">
          {task.startAt ? <span className="pc-card__time">{timeOf(task.startAt)}</span> : null}
          <ResourceDot resource={task.resource} />
          <span className="pc-card__meta-text">
            {[
              task.sphereName ?? 'Без сферы',
              task.projectName,
              task.dueDate && isOverdue(task.dueDate) ? 'просрочено' : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
        </span>
      </button>
      {menu ? <div className="pc-card__menu">{menu}</div> : null}
    </article>
  );
}
