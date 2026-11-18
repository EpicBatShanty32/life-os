import { useState } from 'react';
import type { ReactNode } from 'react';
import { createGoal, markHabit, patchGoal, useSection } from '../api/sections';
import type { SectionGoal, SectionHabit, SectionSeries } from '../api/sections';
import { Icon } from './Icon';
import { habitIcon } from '../lib/habitIcon';
import type { IconName } from './Icon';
import { KpiCard } from './KpiCard';
import type { Accent } from '../lib/ui';

const ACCENTS: Accent[] = ['violet', 'green', 'peach', 'blue'];

const PERIODS = [30, 90, 120];

function dayLabel(isoDate: string): string {
  const [, month, day] = isoDate.split('-');
  return `${day}.${month}`;
}

function shortDay(isoDate: string): string {
  return isoDate.slice(8, 10);
}

function Bars({ series, max }: { series: SectionSeries; max: number }) {
  const points = series.points.slice(-30);
  const ceiling = max > 0 ? max : 1;
  return (
    <figure className="bars" aria-label={`${series.label}: динамика`}>
      {points.map((point) => {
        const height = Math.max(3, Math.round((point.value / ceiling) * 100));
        return (
          <div className="bars__col" key={point.date} title={`${dayLabel(point.date)} — ${point.value} ${series.unit}`}>
            <div className="bars__track">
              <span className="bars__bar" style={{ height: `${height}%` }} />
            </div>
            <span className="bars__label">{shortDay(point.date)}</span>
          </div>
        );
      })}
    </figure>
  );
}

function SeriesCard({ series }: { series: SectionSeries }) {
  const max = Math.max(...series.points.map((point) => point.value), 0);
  return (
    <section className="card card--chart" aria-labelledby={`series-${series.key}`}>
      <header className="card__head">
        <div>
          <h2 className="card__title" id={`series-${series.key}`}>
            {series.label}
          </h2>
          <p className="card__sub">
            Последнее: {series.last} {series.unit} · среднее за неделю: {series.avg} {series.unit}
          </p>
        </div>
        <span className="status-pill">
          <span className="status-pill__dot" aria-hidden="true" />
          {series.points.length} дн.
        </span>
      </header>
      <Bars series={series} max={max} />
    </section>
  );
}

function HabitRows({
  habits,
  onToggle,
}: {
  habits: SectionHabit[];
  onToggle: (habit: SectionHabit) => void;
}) {
  return (
    <div className="habit-list">
      {habits.map((habit) => {
        const todayDone = habit.marks[habit.marks.length - 1] === true;
        return (
          <div className="habit" key={habit.id}>
            <span className="habit__emoji" aria-hidden="true">
              <Icon name={habitIcon(habit.emoji, habit.name)} size={17} />
            </span>
            <span className="habit__name">
              {habit.name}
              <small>
                {habit.doneWeek} из {habit.target} за неделю
                {habit.streak > 1 ? ` · серия ${habit.streak} дн.` : ''}
              </small>
            </span>
            <span className="habit__week" aria-hidden="true">
              {habit.marks.map((done, index) => (
                <span
                  className={done ? 'habit__dot habit__dot--on' : 'habit__dot'}
                  key={`${habit.id}-${habit.week[index]}`}
                  title={habit.week[index]}
                />
              ))}
            </span>
            <button
              className={todayDone ? 'btn btn--ghost habit__check' : 'btn habit__check'}
              type="button"
              onClick={() => onToggle(habit)}
              aria-pressed={todayDone}
            >
              <Icon name={todayDone ? 'check-circle' : 'check'} size={16} />
              <span>{todayDone ? 'Сделано' : 'Отметить'}</span>
            </button>
          </div>
        );
      })}
    </div>
  );
}

function GoalRows({
  goals,
  onProgress,
  onAdd,
}: {
  goals: SectionGoal[];
  onProgress: (goal: SectionGoal, delta: number) => void;
  onAdd: (title: string, target: number | null, unit: string) => void;
}) {
  const [title, setTitle] = useState('');
  const [target, setTarget] = useState('');
  const [unit, setUnit] = useState('');

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (title.trim() === '') return;
    const parsed = Number.parseFloat(target.replace(',', '.'));
    onAdd(title.trim(), Number.isFinite(parsed) ? parsed : null, unit.trim());
    setTitle('');
    setTarget('');
    setUnit('');
  }

  return (
    <div className="goal-list">
      {goals.map((goal) => (
        <article className="goal" key={goal.id}>
          <header className="goal__head">
            <h3 className="goal__title">{goal.title}</h3>
            <span className="goal__value">
              {goal.current}
              {goal.target ? ` / ${goal.target}` : ''} {goal.unit}
            </span>
          </header>
          <div className="goal__meter" aria-hidden="true">
            <span className="goal__bar" style={{ width: `${goal.percent}%` }} />
          </div>
          <footer className="goal__foot">
            <span className="goal__percent">{goal.percent}%</span>
            {goal.deadline ? (
              <span className="goal__deadline">
                <Icon name="clock" size={14} /> до {dayLabel(goal.deadline)}
              </span>
            ) : null}
            <span className="goal__actions">
              <button className="btn btn--ghost" type="button" onClick={() => onProgress(goal, 1)}>
                +1
              </button>
              <button className="btn btn--ghost" type="button" onClick={() => onProgress(goal, -1)}>
                −1
              </button>
            </span>
          </footer>
        </article>
      ))}

      <form className="goal-form" onSubmit={submit}>
        <input
          className="input"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Новая цель"
          aria-label="Название цели"
        />
        <input
          className="input input--short"
          value={target}
          onChange={(event) => setTarget(event.target.value)}
          placeholder="Цель"
          inputMode="decimal"
          aria-label="Целевое значение"
        />
        <input
          className="input input--short"
          value={unit}
          onChange={(event) => setUnit(event.target.value)}
          placeholder="ед."
          aria-label="Единица"
        />
        <button className="btn" type="submit">
          <Icon name="plus" size={16} />
          <span>Добавить</span>
        </button>
      </form>
    </div>
  );
}

export interface SectionPageProps {
  name: string;
  icon: IconName;
  days?: number;
  note?: string;
  periods?: boolean;
  searchable?: boolean;
  children?: ReactNode;
}

export function SectionPage({
  name,
  icon,
  days = 30,
  note,
  periods = true,
  searchable = false,
  children,
}: SectionPageProps) {
  const [period, setPeriod] = useState(days);
  const [search, setSearch] = useState('');
  const { data, loading, error, reload } = useSection(name, period, search);

  async function toggleHabit(habit: SectionHabit) {
    const todayDone = habit.marks[habit.marks.length - 1] === true;
    await markHabit(habit.id, todayDone ? 0 : 1);
    reload();
  }

  async function moveGoal(goal: SectionGoal, delta: number) {
    const next = Math.max(0, Math.round((goal.current + delta) * 100) / 100);
    await patchGoal(goal.id, { current: next });
    reload();
  }

  async function addGoal(title: string, target: number | null, unit: string) {
    await createGoal({ title, target: target ?? undefined, unit: unit || undefined });
    reload();
  }

  const title = data?.title ?? 'Раздел';
  const subtitle = note ?? data?.note ?? '';

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1 className="page-head__title">{title}</h1>
          <p className="page-head__sub">{subtitle}</p>
        </div>
        <div className="page-head__actions">
          {searchable ? (
            <input
              className="input search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Поиск по записям"
              aria-label="Поиск по записям"
              type="search"
            />
          ) : null}
          {periods ? (
            <div className="period" role="group" aria-label="Период">
              {PERIODS.map((value) => (
                <button
                  className={value === period ? 'period__btn period__btn--on' : 'period__btn'}
                  key={value}
                  type="button"
                  onClick={() => setPeriod(value)}
                  aria-pressed={value === period}
                >
                  {value} дн.
                </button>
              ))}
            </div>
          ) : null}
          <span className="status-pill">
            <span className="status-pill__dot" aria-hidden="true" />
            {loading ? 'Обновляю' : error ? 'Нет связи' : 'Данные'}
          </span>
        </div>
      </header>

      {children}

      {error ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>
            API не ответил ({error}). Показываю то, что уже загружено; обновите страницу позже.
          </span>
          <button className="btn btn--ghost" type="button" onClick={reload}>
            Повторить
          </button>
        </div>
      ) : null}

      {data && data.kpis.length > 0 ? (
        <section className="bento" aria-label="Показатели">
          {data.kpis.map((kpi, index) => (
            <KpiCard
              key={`${kpi.label}-${index}`}
              icon={icon}
              accent={ACCENTS[index % ACCENTS.length]}
              label={kpi.label}
              value={String(kpi.value)}
              unit={kpi.unit}
              hint={kpi.hint}
            />
          ))}
        </section>
      ) : null}

      {data?.habits && data.habits.length > 0 ? (
        <section className="card" aria-label="Привычки">
          <header className="card__head">
            <div>
              <h2 className="card__title">Неделя привычек</h2>
              <p className="card__sub">Отметьте сегодняшний день — серия растёт.</p>
            </div>
          </header>
          <HabitRows habits={data.habits} onToggle={toggleHabit} />
        </section>
      ) : null}

      {data?.goals && data.goals.length > 0 ? (
        <section className="card" aria-label="Цели">
          <header className="card__head">
            <div>
              <h2 className="card__title">Цели</h2>
              <p className="card__sub">Прогресс считается от целевого значения.</p>
            </div>
          </header>
          <GoalRows goals={data.goals} onProgress={moveGoal} onAdd={addGoal} />
        </section>
      ) : null}

      {data?.series.map((series) => (
        <SeriesCard key={series.key} series={series} />
      ))}

      {data && data.items.length > 0 ? (
        <section className="card" aria-labelledby="items-title">
          <header className="card__head">
            <div>
              <h2 className="card__title" id="items-title">
                Последние записи
              </h2>
              <p className="card__sub">{data.count ?? data.items.length} шт. в выбранном периоде</p>
            </div>
          </header>
          <ol className="item-list">
            {data.items.map((item) => (
              <li className="item" key={item.id}>
                <span className="item__date">
                  <strong>{dayLabel(item.date)}</strong>
                  <small>{item.time}</small>
                </span>
                <span className="item__kind">{item.kindLabel}</span>
                <span className="item__text">{item.text}</span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {data && data.empty ? (
        <div className="card empty-note" role="status">
          <Icon name={icon} size={20} />
          <span>
            Здесь пока пусто. Записи приходят из Telegram-бота или из быстрого ввода на «Обзоре» —
            начните с одной записи.
          </span>
        </div>
      ) : null}

      {!data && loading ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>Загружаю раздел…</span>
        </div>
      ) : null}

    </div>
  );
}
