import { useState } from 'react';
import type { AgendaKind, DataSource, OverviewPayload } from '../api/types';
import { Icon } from '../components/Icon';
import type { IconName } from '../components/Icon';
import { KpiCard } from '../components/KpiCard';
import { Sparkline } from '../components/Sparkline';
import {
  capitalize,
  formatDateShort,
  formatDateLong,
  formatNumber,
  greetingForHour,
  percent,
  pluralRu,
} from '../lib/format';
import { navigate } from '../lib/router';

interface OverviewProps {
  data: OverviewPayload | null;
  source: DataSource;
  refreshing: boolean;
  onRefresh: () => void;
  onQuickEntry: () => void;
  onAddWater: (ml: number) => void;
}

const AGENDA_ICONS: Record<AgendaKind, IconName> = {
  habit: 'habits',
  goal: 'target',
  workout: 'dumbbell',
  journal: 'journal',
};

function WaterRing({ value, total }: { value: number; total: number }) {
  const filled = percent(value, total);
  const radius = 42;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - filled / 100);

  return (
    <div className="ring" aria-hidden="true">
      <svg viewBox="0 0 104 104">
        <circle className="ring__track" cx="52" cy="52" r={radius} />
        <circle
          className="ring__value"
          cx="52"
          cy="52"
          r={radius}
          strokeDasharray={circumference.toFixed(2)}
          strokeDashoffset={offset.toFixed(2)}
          transform="rotate(-90 52 52)"
        />
      </svg>
      <span className="ring__center">
        <strong>{filled}%</strong>
        <small>нормы</small>
      </span>
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <div className="page" aria-busy="true">
      <header className="page-head">
        <div className="page-head__text">
          <span className="skeleton skeleton--line skeleton--short" />
          <span className="skeleton skeleton--title" />
          <span className="skeleton skeleton--line" />
        </div>
      </header>
      <div className="bento">
        {Array.from({ length: 6 }, (_, index) => (
          <div key={index} className="card skeleton skeleton-card skeleton-card--kpi" />
        ))}
        <div className="card skeleton skeleton-card skeleton-card--water" />
        <div className="card skeleton skeleton-card skeleton-card--weight" />
        <div className="card skeleton skeleton-card skeleton-card--course" />
        <div className="card skeleton skeleton-card skeleton-card--agenda" />
        <div className="card skeleton skeleton-card skeleton-card--activity" />
      </div>
      <span className="visually-hidden">Загружаем данные обзора</span>
    </div>
  );
}

export function Overview({
  data,
  source,
  refreshing,
  onRefresh,
  onQuickEntry,
  onAddWater,
}: OverviewProps) {
  const [agendaState, setAgendaState] = useState<Record<string, boolean>>({});

  if (!data) return <OverviewSkeleton />;

  const { kpis } = data;
  const waterPercent = percent(kpis.water.ml, kpis.water.targetMl);
  const checkinsPercent = percent(kpis.checkins.completed, kpis.checkins.total);
  const workoutsPercent = percent(kpis.workouts.week, kpis.workouts.target);
  const waterLeft = Math.max(0, kpis.water.targetMl - kpis.water.ml);
  const sleepDelta = kpis.sleep.lastHours - kpis.sleep.avg7Hours;
  const moodDelta = kpis.mood.score - kpis.mood.avg7;
  const agendaDone = data.agenda.filter(
    (item) => agendaState[item.id] ?? item.done,
  ).length;
  const activityTotal = data.activity.reduce((sum, day) => sum + day.minutes, 0);
  const activityAverage = data.activity.length
    ? Math.round(activityTotal / data.activity.length)
    : 0;
  const activityMax = Math.max(60, ...data.activity.map((day) => day.minutes));
  const coursePercent = data.course
    ? percent(data.course.doneLessons, data.course.totalLessons)
    : 0;
  const lastWeightDate = data.weight.series.at(-1)?.date;

  const sleepTrend =
    Math.abs(sleepDelta) >= 0.05
      ? {
          value: `${sleepDelta > 0 ? '+' : '−'}${formatNumber(Math.abs(sleepDelta), 1)} ч`,
          direction: sleepDelta > 0 ? ('up' as const) : ('down' as const),
          tone: sleepDelta > 0 ? ('good' as const) : ('bad' as const),
        }
      : undefined;

  const moodTrend =
    Math.abs(moodDelta) >= 0.05
      ? {
          value: `${moodDelta > 0 ? '+' : '−'}${formatNumber(Math.abs(moodDelta), 1)}`,
          direction: moodDelta > 0 ? ('up' as const) : ('down' as const),
          tone: moodDelta > 0 ? ('good' as const) : ('bad' as const),
        }
      : undefined;

  const toggleAgenda = (id: string, current: boolean) => {
    setAgendaState((previous) => ({ ...previous, [id]: !current }));
  };

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-head__text">
          <p className="page-head__kicker">
            Сегодня, {capitalize(formatDateLong(new Date()))}
          </p>
          <h1 className="page-head__title page-head__title--hero">
            {greetingForHour(new Date().getHours())}
          </h1>
          <p className="page-head__sub">
            Серия — {kpis.streak.days} {pluralRu(kpis.streak.days, ['день', 'дня', 'дней'])},
            вода — {waterPercent}%, сон — {formatNumber(kpis.sleep.lastHours, 1)} ч
          </p>
          {source === 'mock' ? (
            <p className="page-head__flag">
              <span className="status-pill__dot" aria-hidden="true" />
              Демо-режим: API Ф4 ещё не подключён, действия сохраняются локально.
            </p>
          ) : null}
        </div>

        <div className="page-head__actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onRefresh}
            disabled={refreshing}
            aria-label="Обновить данные обзора"
          >
            <Icon name="sync" size={16} className={refreshing ? 'is-spinning' : undefined} />
            <span>{refreshing ? 'Обновляем…' : 'Обновить'}</span>
          </button>
          <button type="button" className="btn btn--primary" onClick={onQuickEntry}>
            <Icon name="plus" size={17} />
            <span>Быстрая запись</span>
          </button>
        </div>
      </header>

      <section className="bento" aria-label="Пульс дня">
        <h2 className="visually-hidden">Ключевые показатели</h2>

        <KpiCard
          icon="flame"
          accent="peach"
          label="Стрик"
          value={formatNumber(kpis.streak.days)}
          unit={pluralRu(kpis.streak.days, ['день', 'дня', 'дней'])}
          hint={`Лучший результат — ${formatNumber(kpis.streak.bestDays)} ${pluralRu(
            kpis.streak.bestDays,
            ['день', 'дня', 'дней'],
          )}`}
        />

        <KpiCard
          icon="check-circle"
          accent="violet"
          label="Чек-ины"
          value={`${formatNumber(kpis.checkins.completed)}/${formatNumber(kpis.checkins.total)}`}
          hint={`Осталось ${formatNumber(Math.max(0, kpis.checkins.total - kpis.checkins.completed))} ${pluralRu(
            Math.max(0, kpis.checkins.total - kpis.checkins.completed),
            ['отметка', 'отметки', 'отметок'],
          )}`}
          progress={checkinsPercent}
        />

        <KpiCard
          icon="dumbbell"
          accent="green"
          label="Тренировки"
          value={`${formatNumber(kpis.workouts.week)}/${formatNumber(kpis.workouts.target)}`}
          hint={`Цель недели — ${formatNumber(kpis.workouts.target)} ${pluralRu(
            kpis.workouts.target,
            ['тренировка', 'тренировки', 'тренировок'],
          )}`}
          progress={workoutsPercent}
        />

        <KpiCard
          icon="moon"
          accent="blue"
          label="Сон"
          value={formatNumber(kpis.sleep.lastHours, 1)}
          unit="ч"
          hint={`Среднее за 7 дней — ${formatNumber(kpis.sleep.avg7Hours, 1)} ч`}
          trend={sleepTrend}
        />

        <KpiCard
          icon="droplet"
          accent="blue"
          label="Вода"
          value={formatNumber(kpis.water.ml)}
          unit="мл"
          hint={
            waterLeft > 0
              ? `До нормы — ${formatNumber(waterLeft)} мл`
              : 'Норма воды выполнена'
          }
          progress={waterPercent}
        />

        <KpiCard
          icon="mood"
          accent="violet"
          label="Настроение"
          value={`${formatNumber(kpis.mood.score)}/${formatNumber(kpis.mood.max)}`}
          hint={`${capitalize(kpis.mood.label)} · среднее ${formatNumber(kpis.mood.avg7, 1)}`}
          trend={moodTrend}
        />

        <article className="card widget widget--water" aria-labelledby="widget-water-title">
          <header className="widget__head">
            <div className="widget__title-wrap">
              <span className="widget__icon" data-accent="blue" aria-hidden="true">
                <Icon name="droplet" size={17} />
              </span>
              <h2 className="widget__title" id="widget-water-title">
                Вода
              </h2>
            </div>
            <span className="chip chip--soft">{waterPercent}%</span>
          </header>

          <div className="water">
            <WaterRing value={kpis.water.ml} total={kpis.water.targetMl} />
            <div className="water__info">
              <p className="water__value">
                <strong>{formatNumber(kpis.water.ml)}</strong>
                <span>мл</span>
              </p>
              <p className="water__target">из {formatNumber(kpis.water.targetMl)} мл</p>
              <p className="water__left">
                {waterLeft > 0
                  ? `Осталось ${formatNumber(waterLeft)} мл`
                  : 'Норма выполнена — отлично'}
              </p>
            </div>
          </div>

          <div className="water__actions">
            <button
              type="button"
              className="btn btn--soft"
              onClick={() => onAddWater(250)}
            >
              <Icon name="plus" size={15} />
              <span>250 мл</span>
            </button>
            <button
              type="button"
              className="btn btn--soft"
              onClick={() => onAddWater(500)}
            >
              <Icon name="plus" size={15} />
              <span>500 мл</span>
            </button>
          </div>
        </article>

        <article className="card widget widget--weight" aria-labelledby="widget-weight-title">
          <header className="widget__head">
            <div className="widget__title-wrap">
              <span className="widget__icon" data-accent="green" aria-hidden="true">
                <Icon name="scale" size={17} />
              </span>
              <h2 className="widget__title" id="widget-weight-title">
                Вес
              </h2>
            </div>
            <span className="chip chip--soft">
              цель {formatNumber(data.weight.targetKg, 1)} кг
            </span>
          </header>

          <div className="weight__main">
            <p className="weight__value">
              <strong>{formatNumber(data.weight.currentKg, 1)}</strong>
              <span>кг</span>
            </p>
            <span
              className={`trend ${data.weight.delta30Kg <= 0 ? 'trend--good' : 'trend--warn'}`}
            >
              <Icon
                name={data.weight.delta30Kg <= 0 ? 'trend-down' : 'trend-up'}
                size={14}
              />
              {formatNumber(Math.abs(data.weight.delta30Kg), 1)} кг за 30 дней
            </span>
          </div>

          <Sparkline
            points={data.weight.series}
            label={`Вес за ${data.weight.series.length} дней: от ${formatNumber(
              data.weight.series[0]?.value ?? data.weight.currentKg,
              1,
            )} до ${formatNumber(data.weight.currentKg, 1)} кг`}
            accent="green"
            unit="кг"
          />

          <p className="widget-note">
            {data.weight.series.length > 0
              ? `${data.weight.series.length} дней${
                  lastWeightDate
                    ? ` · последнее измерение ${formatDateShort(lastWeightDate)}`
                    : ''
                }`
              : 'Нет измерений веса — добавьте первое значение'}
          </p>
        </article>

        <article className="card widget widget--course" aria-labelledby="widget-course-title">
          <span className="course__kicker">
            <Icon name="book" size={14} />
            Обучение
          </span>
          <h2 className="widget__title widget__title--lg" id="widget-course-title">
            Продолжить обучение
          </h2>

          {data.course ? (
            <>
              <p className="course__title">{data.course.title}</p>
              <p className="course__lesson">{data.course.lessonTitle}</p>
              <div className="course__progress">
                <div
                  className="meter"
                  role="progressbar"
                  aria-valuenow={coursePercent}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`Курс «${data.course.title}»: ${coursePercent}%`}
                >
                  <span className="meter__fill" style={{ width: `${coursePercent}%` }} />
                </div>
                <span className="course__count">
                  {data.course.doneLessons} из {data.course.totalLessons} уроков
                </span>
              </div>
              <button
                type="button"
                className="btn btn--dark course__btn"
                onClick={() => navigate('/courses')}
              >
                <Icon name="play" size={15} />
                <span>Продолжить</span>
              </button>
            </>
          ) : (
            <p className="empty-note">Курсы ещё не подключены. Появятся после фазы API.</p>
          )}
        </article>

        <article className="card widget widget--agenda" aria-labelledby="widget-agenda-title">
          <header className="widget__head">
            <div className="widget__title-wrap">
              <span className="widget__icon" data-accent="peach" aria-hidden="true">
                <Icon name="today" size={17} />
              </span>
              <h2 className="widget__title" id="widget-agenda-title">
                План на сегодня
              </h2>
            </div>
            <span className="chip chip--soft">
              {agendaDone} из {data.agenda.length}
            </span>
          </header>

          {data.agenda.length === 0 ? (
            <p className="empty-note">На сегодня задач нет — хороший день для отдыха.</p>
          ) : (
            <ul className="agenda">
              {data.agenda.map((item) => {
                const done = agendaState[item.id] ?? item.done;
                return (
                  <li key={item.id} className={`agenda__item ${done ? 'is-done' : ''}`}>
                    <button
                      type="button"
                      className="agenda__check"
                      onClick={() => toggleAgenda(item.id, done)}
                      aria-pressed={done}
                      aria-label={`${done ? 'Снять отметку' : 'Отметить'} «${item.title}»`}
                    >
                      {done ? <Icon name="check" size={13} /> : null}
                    </button>
                    <span className="agenda__kind" aria-hidden="true">
                      <Icon name={AGENDA_ICONS[item.kind]} size={15} />
                    </span>
                    <span className="agenda__body">
                      <span className="agenda__title">{item.title}</span>
                      <span className="agenda__meta">{item.meta}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}

          <p className="widget-note">Отметки хранятся в браузере до подключения API.</p>
        </article>

        <article className="card widget widget--activity" aria-labelledby="widget-activity-title">
          <header className="widget__head">
            <div className="widget__title-wrap">
              <span className="widget__icon" data-accent="violet" aria-hidden="true">
                <Icon name="chart" size={17} />
              </span>
              <h2 className="widget__title" id="widget-activity-title">
                Активность
              </h2>
            </div>
            <span className="chip chip--soft">{activityTotal} мин</span>
          </header>

          {data.activity.length > 0 ? (
            <>
              <div
                className="bars"
                role="img"
                aria-label={`Активность за 7 дней: ${data.activity
                  .map((day) => `${day.label} — ${day.minutes} минут`)
                  .join(', ')}`}
              >
                {data.activity.map((day) => {
                  const height = Math.max(8, Math.round((day.minutes / activityMax) * 100));
                  const isToday = day.date === data.date;
                  return (
                    <div className="bars__col" key={day.date} aria-hidden="true">
                      <div className="bars__track">
                        <span
                          className={`bars__bar ${isToday ? 'is-today' : ''}`}
                          style={{ height: `${height}%` }}
                        />
                      </div>
                      <span className={`bars__label ${isToday ? 'is-today' : ''}`}>
                        {day.label}
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className="widget-note">Среднее — {activityAverage} мин в день</p>
            </>
          ) : (
            <p className="empty-note">Нет данных об активности за последние дни.</p>
          )}
        </article>

        <article className="card widget widget--insight" aria-labelledby="widget-insight-title">
          <span className="insight__icon" aria-hidden="true">
            <Icon name="sparkles" size={20} />
          </span>
          {data.insight ? (
            <>
              <h2 className="widget__title" id="widget-insight-title">
                {data.insight.title}
              </h2>
              <p className="insight__text">{data.insight.text}</p>
              {data.insight.footnote ? (
                <p className="widget-note">{data.insight.footnote}</p>
              ) : null}
            </>
          ) : (
            <>
              <h2 className="widget__title" id="widget-insight-title">
                Инсайты
              </h2>
              <p className="insight__text">
                Пока мало данных для корреляций — нужно минимум 14 дней наблюдений.
              </p>
            </>
          )}
          <button type="button" className="link-btn" onClick={() => navigate('/analytics')}>
            <span>Открыть аналитику</span>
            <Icon name="arrow-right" size={15} />
          </button>
        </article>
      </section>
    </div>
  );
}
