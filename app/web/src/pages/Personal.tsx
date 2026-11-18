/**
 * «Личное» — ресурс дня (DESIGN-v4 §4, handoff §10.4).
 *
 * Компактный чек-ин энергии, привычки сеткой недели/четырёх недель и личные
 * списки по тегам. Никаких героев и медальонов: полоса фактов, строки, точки
 * 6 px и волосяные линии. Все действия оптимистичные: сначала UI, потом
 * запрос, при ошибке — откат и тост.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  dataColor,
  fetchHabitMonth,
  saveCheckin,
  setHabitMark,
  setTaskDue,
  usePersonalSummary,
  usePersonalTasks,
} from '../api/personal';
import type { HabitSummary, PersonalTask } from '../api/personal';
import { Button, EmptyState, Input, Menu, Segmented, Section, Skeleton } from '../components/ui';
import type { MenuAction } from '../components/ui';
import {
  addDays,
  formatDateLong,
  formatDateShort,
  formatNumber,
  parseIsoDate,
  pluralRu,
  toLocalIso,
  weekdayShort,
} from '../lib/format';
import { toast } from '../lib/toast';
import '../styles/pages-insight.css';

const ENERGY_LEVELS = [1, 2, 3, 4, 5] as const;
const ENERGY_WORDS: Record<number, string> = {
  1: 'на нуле',
  2: 'мало сил',
  3: 'норма',
  4: 'хорошо',
  5: 'полный',
};
const MOODS = ['В потоке', 'Спокойно', 'Усталость', 'Перегруз'] as const;
const UNTAGGED = 'Без тега';
const HABIT_WEEKS = 4;
const HABIT_DAYS = HABIT_WEEKS * 7;

type HabitRange = 'week' | 'month';

function wordEnergy(level: number): string {
  return ENERGY_WORDS[level] ?? '';
}

/** Отметка в оптимистичной копии привычки: пересчитываем неделю сразу. */
function withMark(habit: HabitSummary, day: string, marked: boolean): HabitSummary {
  const marks = { ...habit.marks };
  if (marked) marks[day] = 1;
  else delete marks[day];
  return {
    ...habit,
    marks,
    doneWeek: habit.week.filter((item) => marks[item] === 1).length,
  };
}

function habitPercent(habits: readonly HabitSummary[]): number {
  const total = habits.reduce((sum, habit) => sum + (habit.targetPerWeek || 7), 0);
  if (!total) return 0;
  const done = habits.reduce(
    (sum, habit) => sum + Math.min(habit.doneWeek, habit.targetPerWeek || 7),
    0,
  );
  return Math.round((done / total) * 100);
}

function dueLabel(task: PersonalTask, today: string): { text: string; overdue: boolean } {
  if (!task.dueDate) return { text: 'без даты', overdue: false };
  const overdue = task.dueDate < today;
  if (task.dueDate === today) return { text: 'сегодня', overdue: false };
  return { text: `${formatDateShort(task.dueDate)}`, overdue };
}

export function Personal() {
  const today = toLocalIso();
  const summary = usePersonalSummary(today);
  const lists = usePersonalTasks();

  const checkin = summary.data?.checkin ?? null;
  const [energy, setEnergy] = useState<number | null>(null);
  const [mood, setMood] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [range, setRange] = useState<HabitRange>('week');
  const [monthMarks, setMonthMarks] = useState<Map<number, Set<string>> | null>(null);
  const [monthLoading, setMonthLoading] = useState(false);
  const [tag, setTag] = useState('all');
  const seeded = useRef('');

  // Чек-ин всегда отражает сервер: после ответа и после откатов.
  useEffect(() => {
    const key = checkin
      ? `${checkin.date}|${checkin.energy}|${checkin.mood}|${checkin.note}`
      : 'none';
    if (seeded.current === key) return;
    seeded.current = key;
    setEnergy(checkin?.energy ?? null);
    setMood(checkin?.mood ?? '');
    setNote(checkin?.note ?? '');
  }, [checkin]);

  // Четыре недели отметок нужны только при переключении на месяц.
  useEffect(() => {
    if (range !== 'month' || monthMarks) return;
    let alive = true;
    setMonthLoading(true);
    fetchHabitMonth(today, HABIT_WEEKS)
      .then((marks) => {
        if (alive) setMonthMarks(marks);
      })
      .catch(() => {
        if (alive) toast('Не удалось загрузить отметки за месяц', 'error');
      })
      .finally(() => {
        if (alive) setMonthLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [range, monthMarks, today]);

  const commitCheckin = useCallback(
    async (next: { energy: number; mood: string; note: string }) => {
      if (saving) return;
      setSaving(true);
      seeded.current = `${today}|${next.energy}|${next.mood}|${next.note}`;
      summary.apply((previous) => ({
        ...previous,
        checkin: { date: today, energy: next.energy, mood: next.mood, note: next.note },
      }));
      try {
        await saveCheckin({
          date: today,
          energy: next.energy,
          mood: next.mood || undefined,
          note: next.note || undefined,
        });
        toast('Чек-ин сохранён', 'success');
      } catch {
        toast('Чек-ин не сохранился — данные вернул', 'error');
        await summary.reload();
      } finally {
        setSaving(false);
      }
    },
    [saving, summary, today],
  );

  const pickEnergy = (level: number) => {
    setEnergy(level);
    void commitCheckin({ energy: level, mood, note });
  };

  const pickMood = (next: string) => {
    const value = mood === next ? '' : next;
    setMood(value);
    if (energy === null) {
      toast('Сначала выберите энергию — тег сохранится вместе с ней', 'default');
      return;
    }
    void commitCheckin({ energy, mood: value, note });
  };

  const saveNote = () => {
    if (energy === null) {
      toast('Сначала выберите энергию — заметка сохранится вместе с чек-ином', 'default');
      return;
    }
    void commitCheckin({ energy, mood, note });
  };

  const toggleHabit = async (habit: HabitSummary, day: string, marked: boolean) => {
    if (day > today) return;
    if (range === 'week') {
      summary.apply((previous) => ({
        ...previous,
        habits: previous.habits.map((item) =>
          item.id === habit.id ? withMark(item, day, marked) : item,
        ),
      }));
    } else {
      setMonthMarks((previous) => {
        const next = new Map(previous ?? []);
        const days = new Set(next.get(habit.id) ?? habit.week.filter((d) => habit.marks[d] === 1));
        if (marked) days.add(day);
        else days.delete(day);
        next.set(habit.id, days);
        return next;
      });
    }
    // Отметки месяца кэшированы: правка в неделе делает их устаревшими.
    // В месячном режиме карту уже обновили оптимистично — её не сбрасываем.
    if (range === 'week') setMonthMarks((previous) => (previous ? null : previous));
    try {
      await setHabitMark(habit.id, day, marked ? 1 : 0);
    } catch {
      toast('Отметка не сохранилась — вернул как было', 'error');
      if (range === 'week') {
        summary.apply((previous) => ({
          ...previous,
          habits: previous.habits.map((item) =>
            item.id === habit.id ? withMark(item, day, !marked) : item,
          ),
        }));
      } else {
        setMonthMarks(null);
      }
    }
  };

  const moveTask = async (task: PersonalTask, dueDate: string | null, label: string) => {
    lists.setTasks((previous) =>
      previous.map((item) => (item.id === task.id ? { ...item, dueDate } : item)),
    );
    try {
      await setTaskDue(task.id, dueDate);
      toast(`${task.title}: ${label}`, 'success');
    } catch {
      toast('Не удалось перенести задачу', 'error');
      await lists.reload();
    }
  };

  const habits = summary.data?.habits ?? [];
  const energyDays = summary.data?.energy7 ?? [];
  const tasks = summary.data?.tasks ?? null;
  const reading = summary.data?.reading ?? null;

  const habitPct = habitPercent(habits);
  const energyFilled = energyDays.filter((day) => day.value !== null).length;

  const monthDays = useMemo(() => {
    const days: string[] = [];
    const anchor = parseIsoDate(today);
    for (let index = HABIT_DAYS - 1; index >= 0; index -= 1) {
      const date = addDays(anchor, -index);
      days.push(toLocalIso(date));
    }
    return days;
  }, [today]);

  const groups = useMemo(() => {
    const byTag = new Map<string, PersonalTask[]>();
    for (const task of lists.tasks) {
      const key = task.tags[0] ?? UNTAGGED;
      const bucket = byTag.get(key) ?? [];
      bucket.push(task);
      byTag.set(key, bucket);
    }
    for (const bucket of byTag.values()) {
      bucket.sort((left, right) => {
        if (left.dueDate === right.dueDate) return left.title.localeCompare(right.title, 'ru');
        if (!left.dueDate) return 1;
        if (!right.dueDate) return -1;
        return left.dueDate.localeCompare(right.dueDate);
      });
    }
    return [...byTag.entries()].sort((left, right) => {
      if (left[0] === UNTAGGED) return 1;
      if (right[0] === UNTAGGED) return -1;
      return right[1].length - left[1].length;
    });
  }, [lists.tasks]);

  const visibleGroups = tag === 'all' ? groups : groups.filter(([name]) => name === tag);

  const taskMenu = (task: PersonalTask): MenuAction[] => [
    {
      id: 'today',
      label: 'Сегодня',
      icon: 'today',
      disabled: task.dueDate === today,
      onSelect: () => void moveTask(task, today, 'сегодня'),
    },
    {
      id: 'tomorrow',
      label: 'Завтра',
      icon: 'calendar',
      onSelect: () => void moveTask(task, toLocalIso(addDays(parseIsoDate(today), 1)), 'завтра'),
    },
    {
      id: 'week',
      label: 'Через неделю',
      icon: 'arrow-right',
      onSelect: () =>
        void moveTask(
          task,
          toLocalIso(addDays(parseIsoDate(today), 7)),
          'на следующую неделю',
        ),
    },
    {
      id: 'none',
      label: 'Снять дату',
      icon: 'close',
      separatorBefore: true,
      disabled: task.dueDate === null,
      onSelect: () => void moveTask(task, null, 'дата снята'),
    },
  ];

  if (summary.error) {
    return (
      <div className="pi">
        <PersonalHead subtitle="Ресурс дня" />
        <p className="pi-quiet" role="status">
          Не удалось загрузить сводку личного: {summary.error}
          <button type="button" className="pi-link" onClick={() => void summary.reload()}>
            Повторить
          </button>
        </p>
      </div>
    );
  }

  return (
    <div className="pi">
      <PersonalHead subtitle="Ресурс дня" />

      <div className="pi-strip" role="group" aria-label="Факты дня">
        <div className="pi-strip__cell">
          <span className="pi-strip__label">Задачи</span>
          <p className="pi-strip__value">
            {tasks ? `${formatNumber(tasks.done)}/${formatNumber(tasks.total)}` : '—'}
          </p>
          <span className="pi-strip__hint">закрыто из плана на день</span>
        </div>
        <div className="pi-strip__cell">
          <span className="pi-strip__label">Привычки</span>
          <p className="pi-strip__value">{habits.length ? `${habitPct}%` : '—'}</p>
          <span className="pi-strip__hint">
            {habits.length
              ? `выполнение ${pluralRu(habits.length, ['привычки', 'привычек', 'привычек'])}`
              : 'привычек пока нет'}
          </span>
        </div>
        <div className="pi-strip__cell">
          <span className="pi-strip__label">Чтение</span>
          <p className="pi-strip__value">{reading ? formatNumber(reading.pagesWeek) : '—'}</p>
          <span className="pi-strip__hint">страниц за неделю</span>
        </div>
        <div className="pi-strip__cell">
          <span className="pi-strip__label">Чек-ины</span>
          <p className="pi-strip__value">{`${energyFilled}/7`}</p>
          <span className="pi-strip__hint">дней с энергией</span>
        </div>
      </div>

      <Section
        title="Чек-ин"
        description={
          checkin
            ? 'Чек-ин записан — повторный ответ перезапишет его'
            : 'Раз в день: энергия, состояние и одна строка заметки'
        }
      >
        <div className="pi-checkin">
          <div className="pi-checkin__main">
            <div className="pi-checkin__row">
              <span className="pi-label">Энергия</span>
              <div className="pi-energy" role="radiogroup" aria-label="Энергия от 1 до 5">
                {ENERGY_LEVELS.map((level) => (
                  <button
                    key={level}
                    type="button"
                    role="radio"
                    aria-checked={energy === level}
                    aria-label={`${level} — ${wordEnergy(level)}`}
                    title={wordEnergy(level)}
                    className="pi-energy__level"
                    data-active={energy === level}
                    onClick={() => pickEnergy(level)}
                  >
                    {level}
                  </button>
                ))}
              </div>
              <span className="pi-checkin__word">
                {energy === null ? 'не выбрано' : wordEnergy(energy)}
              </span>
            </div>

            <div className="pi-checkin__row">
              <span className="pi-label">Состояние</span>
              <div className="pi-moods" role="group" aria-label="Состояние дня">
                {MOODS.map((item) => (
                  <button
                    key={item}
                    type="button"
                    className="pi-mood"
                    aria-pressed={mood === item}
                    data-active={mood === item}
                    onClick={() => pickMood(item)}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>

            <div className="pi-checkin__row pi-checkin__row--note">
              <label className="pi-label" htmlFor="pi-checkin-note">
                Заметка
              </label>
              <Input
                id="pi-checkin-note"
                className="pi-checkin__note"
                placeholder="Одна строка о дне"
                value={note}
                maxLength={280}
                onChange={(event) => setNote(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    saveNote();
                  }
                }}
              />
              <Button
                size="sm"
                variant="ghost"
                onClick={saveNote}
                disabled={saving || energy === null}
              >
                Сохранить
              </Button>
            </div>
          </div>

          <div className="pi-week">
            <span className="pi-week__title">Энергия за 7 дней</span>
            <div className="pi-week__chart" role="img" aria-label="Энергия по дням недели">
              {energyDays.map((day) => (
                <div className="pi-week__col" key={day.date}>
                  <span className="pi-week__track">
                    <span
                      className="pi-week__bar"
                      data-empty={day.value === null}
                      style={
                        day.value === null ? undefined : { height: `${(day.value / 5) * 100}%` }
                      }
                    />
                  </span>
                  <span className="pi-week__day">{weekdayShort(day.date)}</span>
                </div>
              ))}
            </div>
            <span className="pi-week__hint">
              {energyFilled
                ? `средняя ${formatNumber(
                    energyDays.reduce((sum, day) => sum + (day.value ?? 0), 0) / energyFilled,
                    1,
                  )} из 5`
                : 'нет чек-инов за неделю'}
            </span>
          </div>
        </div>
      </Section>

      <Section
        title="Привычки"
        description={
          range === 'week'
            ? 'Отметки за неделю, серия и норма. Нажмите на день, чтобы отметить.'
            : `Отметки за ${HABIT_DAYS} дней: сетка 7 × ${HABIT_WEEKS}. Нажмите на день, чтобы отметить.`
        }
        actions={
          <Segmented
            ariaLabel="Период привычек"
            value={range}
            onChange={setRange}
            options={[
              { value: 'week', label: 'Неделя' },
              { value: 'month', label: `${HABIT_WEEKS} недели` },
            ]}
          />
        }
      >
        {summary.loading ? (
          <Skeleton variant="text" lines={3} label="Загружаю привычки" />
        ) : habits.length === 0 ? (
          <EmptyState
            title="Привычек пока нет"
            description="Опорные 3–5 привычек задаются в Настройках — здесь появятся отметки и серии."
            action={
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  window.location.hash = '#/settings';
                }}
              >
                Открыть настройки
              </Button>
            }
          />
        ) : (
          <div className="pi-habits" data-range={range}>
            {range === 'week' ? (
              <div className="pi-habits__head" aria-hidden="true">
                <span />
                <span />
                <span />
                <div className="pi-habits__days">
                  {(habits[0]?.week ?? []).map((day) => (
                    <span key={day}>{weekdayShort(day)}</span>
                  ))}
                </div>
              </div>
            ) : null}

            {monthLoading ? <Skeleton variant="row" label="Загружаю отметки за месяц" /> : null}

            {habits.map((habit) => {
              const days = range === 'week' ? habit.week : monthDays;
              const markedDays =
                range === 'week'
                  ? new Set(habit.week.filter((day) => habit.marks[day] === 1))
                  : (monthMarks?.get(habit.id) ?? new Set(habit.week.filter((day) => habit.marks[day] === 1)));
              const monthDone = [...markedDays].filter((day) => day >= (monthDays[0] ?? day)).length;
              return (
                <div className="pi-habit" key={habit.id}>
                  <span
                    className="pi-habit__dot"
                    style={{ background: dataColor(habit.color) }}
                    aria-hidden="true"
                  />
                  <span className="pi-habit__name" title={habit.name}>
                    {habit.name}
                  </span>
                  <span className="pi-habit__meta">
                    <span className="pi-num">
                      {range === 'week'
                        ? `${habit.doneWeek}/${habit.targetPerWeek || 7}`
                        : `${monthDone}/${HABIT_DAYS}`}
                    </span>
                    {habit.streak > 0 ? (
                      <span className="pi-habit__streak" title="Дней подряд">
                        серия {habit.streak}
                      </span>
                    ) : null}
                  </span>
                  <div
                    className="pi-habit__grid"
                    data-range={range}
                    role="group"
                    aria-label={`Отметки: ${habit.name}`}
                  >
                    {days.map((day) => {
                      const marked = markedDays.has(day);
                      const future = day > today;
                      return (
                        <button
                          key={day}
                          type="button"
                          className="pi-habit__cell"
                          data-marked={marked}
                          data-future={future}
                          disabled={future}
                          aria-pressed={marked}
                          aria-label={`${formatDateShort(day)} ${habit.name}: ${
                            marked ? 'отмечено' : future ? 'ещё не наступило' : 'не отмечено'
                          }`}
                          title={`${formatDateShort(day)}: ${marked ? 'отмечено' : 'не отмечено'}`}
                          onClick={() => void toggleHabit(habit, day, !marked)}
                        >
                          <span
                            className="pi-habit__mark"
                            style={marked ? { background: dataColor(habit.color) } : undefined}
                            aria-hidden="true"
                          />
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Section>

      <Section
        title="Личные списки"
        description="Покупки, дом и уют, семья, здоровье и досуг — отдельно от рабочих дедлайнов"
        actions={
          <span className="pi-section-note">
            {lists.tasks.length
              ? `${formatNumber(lists.tasks.length)} ${pluralRu(lists.tasks.length, ['дело', 'дела', 'дел'])}`
              : 'список пуст'}
          </span>
        }
      >
        {lists.loading ? (
          <Skeleton variant="text" lines={3} label="Загружаю личные дела" />
        ) : lists.error ? (
          <p className="pi-quiet" role="status">
            Не удалось загрузить личные дела: {lists.error}
            <button type="button" className="pi-link" onClick={() => void lists.reload()}>
              Повторить
            </button>
          </p>
        ) : lists.tasks.length === 0 ? (
          <EmptyState
            title="Личных дел нет"
            description="Всё, что не про работу, попадает сюда из инбокса или с экрана «Сегодня»."
            action={
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  window.location.hash = '#/inbox';
                }}
              >
                Открыть инбокс
              </Button>
            }
          />
        ) : (
          <>
            <div className="pi-tags" role="group" aria-label="Тег личных дел">
              <button
                type="button"
                className="pi-tag"
                aria-pressed={tag === 'all'}
                data-active={tag === 'all'}
                onClick={() => setTag('all')}
              >
                Все
                <span className="pi-num">{lists.tasks.length}</span>
              </button>
              {groups.map(([name, items]) => (
                <button
                  key={name}
                  type="button"
                  className="pi-tag"
                  aria-pressed={tag === name}
                  data-active={tag === name}
                  onClick={() => setTag(name)}
                >
                  {name}
                  <span className="pi-num">{items.length}</span>
                </button>
              ))}
            </div>

            {visibleGroups.map(([name, items]) => (
              <div className="pi-group" key={name}>
                <h3 className="pi-group__title">
                  {name}
                  <span className="pi-num">{items.length}</span>
                </h3>
                <ul className="pi-tasks" role="list">
                  {items.map((task) => {
                    const due = dueLabel(task, today);
                    return (
                      <li className="pi-task" key={task.id}>
                        <span className="pi-task__title" title={task.title}>
                          {task.title}
                        </span>
                        <span className="pi-task__due" data-overdue={due.overdue}>
                          {due.text}
                        </span>
                        <span className="pi-task__actions">
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => void moveTask(task, today, 'сегодня')}
                          >
                            Сегодня
                          </Button>
                          <Menu label={`Действия: ${task.title}`} items={taskMenu(task)} />
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </>
        )}
      </Section>
    </div>
  );
}

function PersonalHead({ subtitle }: { subtitle: string }) {
  return (
    <header className="pi-head">
      <div className="pi-head__text">
        <p className="pi-head__date">{formatDateLong()}</p>
        <h1 className="pi-head__title">Личное</h1>
        <p className="pi-head__sub">{subtitle}</p>
      </div>
    </header>
  );
}
