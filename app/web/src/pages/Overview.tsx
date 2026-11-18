import { useState } from 'react';
import type { AgendaItem, DataSource, OverviewPayload } from '../api/types';
import { Sparkline } from '../components/Sparkline';
import { Button, Card, Checkbox, EmptyState, Kpi, ProgressBar, Skeleton } from '../components/ui';
import {
  capitalize,
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
  onAddWater: (ml: number) => void;
}

/** Цвет точки сферы по типу пункта плана — только точка 6 px, без иконок. */
const AGENDA_TONE: Record<AgendaItem['kind'], string> = {
  habit: 'cat-2',
  goal: 'cat-1',
  workout: 'cat-3',
  journal: 'cat-4',
};

/** В метке пункта плана время идёт первым: «18:30 · спина и плечи». */
function splitMeta(meta: string): { time: string | null; text: string } {
  const match = /^\s*(\d{1,2}:\d{2})\s*(?:·\s*)?(.*)$/.exec(meta);
  if (!match) return { time: null, text: meta.trim() };
  return { time: match[1] ?? null, text: (match[2] ?? '').trim() };
}

function OverviewSkeleton() {
  return (
    <div className="page ov" aria-busy="true">
      <header className="ov__head">
        <Skeleton variant="text" lines={1} label="Загружаем дату" />
        <Skeleton variant="title" label="Загружаем заголовок" />
        <Skeleton variant="text" lines={1} label="Загружаем сводку" />
      </header>
      <div className="ov-pulse" aria-hidden="true">
        {Array.from({ length: 6 }, (_, index) => (
          <div className="ov-pulse__cell" key={index}>
            <Skeleton variant="row" label="" />
          </div>
        ))}
      </div>
      <div className="ov-bento" aria-hidden="true">
        <Skeleton variant="block" label="" />
        <Skeleton variant="block" label="" />
      </div>
      <span className="visually-hidden">Загружаем данные обзора</span>
    </div>
  );
}

export function Overview({ data, source, refreshing, onAddWater }: OverviewProps) {
  const [agendaState, setAgendaState] = useState<Record<string, boolean>>({});

  if (!data) return <OverviewSkeleton />;

  const { kpis } = data;
  const waterPercent = percent(kpis.water.ml, kpis.water.targetMl);
  const waterLeft = Math.max(0, kpis.water.targetMl - kpis.water.ml);

  const isDone = (item: AgendaItem): boolean => agendaState[item.id] ?? item.done;
  const agendaDone = data.agenda.filter(isDone).length;

  const sleepDelta = kpis.sleep.lastHours - kpis.sleep.avg7Hours;
  const moodDelta = kpis.mood.score - kpis.mood.avg7;

  const factLine = [
    `${formatNumber(kpis.streak.days)} ${pluralRu(kpis.streak.days, ['день', 'дня', 'дней'])} серии`,
    `вода ${waterPercent}%`,
    `сон ${formatNumber(kpis.sleep.lastHours, 1)} ч`,
    source === 'mock' ? 'демо-данные' : null,
    refreshing ? 'обновляем…' : null,
  ]
    .filter(Boolean)
    .join(' · ');

  const activityTotal = data.activity.reduce((sum, day) => sum + day.minutes, 0);
  const activityMax = Math.max(1, ...data.activity.map((day) => day.minutes));

  return (
    <div className="page ov">
      <header className="ov__head">
        <p className="ov__date">{capitalize(formatDateLong(new Date()))}</p>
        <h1 className="ov__title">{greetingForHour(new Date().getHours())}</h1>
        <p className="ov__fact">{factLine}</p>
      </header>

      <section className="ov-pulse" aria-label="Пульс дня">
        <div className="ov-pulse__cell">
          <Kpi
            label="Серия"
            value={formatNumber(kpis.streak.days)}
            unit="дн."
            hint={`лучшая ${formatNumber(kpis.streak.bestDays)}`}
          />
        </div>
        <div className="ov-pulse__cell">
          <Kpi
            label="Чек-ины"
            value={`${formatNumber(kpis.checkins.completed)}/${formatNumber(kpis.checkins.total)}`}
            hint={
              kpis.checkins.total > kpis.checkins.completed
                ? `осталось ${formatNumber(kpis.checkins.total - kpis.checkins.completed)}`
                : 'всё отмечено'
            }
          />
        </div>
        <div className="ov-pulse__cell">
          <Kpi
            label="Тренировки"
            value={`${formatNumber(kpis.workouts.week)}/${formatNumber(kpis.workouts.target)}`}
            hint="цель недели"
          />
        </div>
        <div className="ov-pulse__cell">
          <Kpi
            label="Сон"
            value={formatNumber(kpis.sleep.lastHours, 1)}
            unit="ч"
            delta={
              Math.abs(sleepDelta) >= 0.05
                ? {
                    value: `${sleepDelta > 0 ? '+' : '−'}${formatNumber(Math.abs(sleepDelta), 1)} к средней`,
                    direction: sleepDelta > 0 ? 'up' : 'down',
                    tone: sleepDelta > 0 ? 'good' : 'bad',
                    label: `Среднее за 7 дней — ${formatNumber(kpis.sleep.avg7Hours, 1)} ч`,
                  }
                : undefined
            }
          />
        </div>
        <div className="ov-pulse__cell ov-pulse__cell--water">
          <Kpi
            label="Вода"
            value={formatNumber(kpis.water.ml)}
            unit="мл"
            hint={
              waterLeft > 0
                ? `из ${formatNumber(kpis.water.targetMl)}`
                : 'норма выполнена'
            }
          />
          <div className="ov-pulse__actions">
            {[250, 500].map((ml) => (
              <button
                key={ml}
                type="button"
                className="ov-step"
                onClick={() => onAddWater(ml)}
                aria-label={`Добавить ${ml} мл воды`}
              >
                +{ml}
              </button>
            ))}
          </div>
        </div>
        <div className="ov-pulse__cell">
          <Kpi
            label="Настроение"
            value={`${formatNumber(kpis.mood.score)}/${formatNumber(kpis.mood.max)}`}
            delta={
              Math.abs(moodDelta) >= 0.05
                ? {
                    value: `${moodDelta > 0 ? '+' : '−'}${formatNumber(Math.abs(moodDelta), 1)} к средней`,
                    direction: moodDelta > 0 ? 'up' : 'down',
                    tone: moodDelta > 0 ? 'good' : 'bad',
                    label: `Среднее за 7 дней — ${formatNumber(kpis.mood.avg7, 1)}`,
                  }
                : undefined
            }
            hint={capitalize(kpis.mood.label)}
          />
        </div>
      </section>

      <div className="ov-bento">
        <div className="ov-col">
          <Card className="ov-plan">
            <Card.Header
              title="План на сегодня"
              subtitle={`${agendaDone} из ${data.agenda.length} отмечено`}
              actions={
                <Button size="sm" variant="ghost" onClick={() => navigate('/today')}>
                  Все дела
                </Button>
              }
            />

            {data.agenda.length === 0 ? (
              <EmptyState
                title="На сегодня пусто"
                description="Задачи, привычки и события появятся здесь, когда их добавят."
                action={
                  <Button size="sm" variant="secondary" onClick={() => navigate('/today')}>
                    Открыть дела
                  </Button>
                }
              />
            ) : (
              <ul className="ov-plan__list">
                {data.agenda.map((item) => {
                  const done = isDone(item);
                  const meta = splitMeta(item.meta);
                  return (
                    <li
                      key={item.id}
                      className="ov-plan__row"
                      data-done={done ? 'true' : undefined}
                    >
                      <span
                        className="ov-plan__dot"
                        data-tone={AGENDA_TONE[item.kind]}
                        aria-hidden="true"
                      />
                      <Checkbox
                        size="sm"
                        className="ov-plan__check"
                        checked={done}
                        label={item.title}
                        description={meta.text || undefined}
                        onChange={() =>
                          setAgendaState((previous) => ({ ...previous, [item.id]: !done }))
                        }
                      />
                      {meta.time ? (
                        <time className="ov-plan__time">{meta.time}</time>
                      ) : (
                        <span className="ov-plan__time" aria-hidden="true" />
                      )}
                    </li>
                  );
                })}
              </ul>
            )}

            <p className="ov-note-line">Отметки сохраняются в этом браузере.</p>
          </Card>

          <Card className="ov-week">
            <Card.Header
              title="Неделя"
              subtitle={`${formatNumber(activityTotal)} минут активности`}
              actions={
                <Button size="sm" variant="ghost" onClick={() => navigate('/analytics')}>
                  Динамика
                </Button>
              }
            />

            {data.activity.length === 0 ? (
              <p className="ov-note-line">За последние дни активности нет.</p>
            ) : (
              <div
                className="ov-week__bars"
                role="img"
                aria-label={`Активность за 7 дней: ${data.activity
                  .map((day) => `${day.label} — ${day.minutes} минут`)
                  .join(', ')}`}
              >
                {data.activity.map((day) => {
                  const today = day.date === data.date;
                  const height = Math.max(4, Math.round((day.minutes / activityMax) * 100));
                  return (
                    <div
                      className="ov-week__col"
                      key={day.date}
                      title={`${day.label}: ${formatNumber(day.minutes)} мин`}
                      aria-hidden="true"
                    >
                      <span className="ov-week__track">
                        <span
                          className="ov-week__bar"
                          data-today={today ? 'true' : undefined}
                          style={{ height: `${height}%` }}
                        />
                      </span>
                      <span className="ov-week__value">{day.minutes}</span>
                      <span className="ov-week__label" data-today={today ? 'true' : undefined}>
                        {day.label}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </div>

        <div className="ov-col ov-col--side">
          <Card className="ov-body">
            <Card.Header
              title="Тело"
              actions={
                <Button size="sm" variant="ghost" onClick={() => navigate('/body')}>
                  Подробнее
                </Button>
              }
            />

            {data.weight.currentKg > 0 ? (
              <>
                <p className="ov-body__value">
                  <span className="ov-body__number">
                    {formatNumber(data.weight.currentKg, 1)}
                  </span>
                  <span className="ov-body__unit">кг</span>
                  {data.weight.delta30Kg !== 0 ? (
                    <span
                      className="ov-body__delta"
                      data-tone={data.weight.delta30Kg <= 0 ? 'good' : 'warn'}
                    >
                      {data.weight.delta30Kg > 0 ? '+' : '−'}
                      {formatNumber(Math.abs(data.weight.delta30Kg), 1)} за 30 дней
                    </span>
                  ) : null}
                </p>

                {data.weight.series.length >= 2 ? (
                  <Sparkline
                    points={data.weight.series}
                    label={`Вес за ${data.weight.series.length} дней: от ${formatNumber(
                      data.weight.series[0]?.value ?? data.weight.currentKg,
                      1,
                    )} до ${formatNumber(data.weight.currentKg, 1)} кг`}
                    accent="green"
                    height={62}
                    unit="кг"
                  />
                ) : (
                  <p className="ov-note-line">Мало измерений для графика.</p>
                )}

                <p className="ov-body__foot">
                  цель {formatNumber(data.weight.targetKg, 1)} кг
                </p>
              </>
            ) : (
              <EmptyState
                title="Вес не записан"
                description="Первое измерение откроет график и динамику за месяц."
                action={
                  <Button size="sm" variant="secondary" onClick={() => navigate('/body')}>
                    Записать вес
                  </Button>
                }
              />
            )}
          </Card>

          <Card className="ov-study">
            <Card.Header title="Учение" />

            {data.course ? (
              <>
                <p className="ov-study__course">{data.course.title}</p>
                <p className="ov-study__lesson">{data.course.lessonTitle}</p>
                <ProgressBar
                  value={data.course.doneLessons}
                  max={data.course.totalLessons}
                  tone="cat-1"
                  showValue
                  label={`${formatNumber(data.course.doneLessons)} из ${formatNumber(
                    data.course.totalLessons,
                  )} уроков`}
                />
                <div className="ov-study__actions">
                  <Button
                    size="sm"
                    variant="secondary"
                    leadingIcon="play"
                    onClick={() => navigate('/courses')}
                  >
                    Продолжить
                  </Button>
                </div>
              </>
            ) : (
              <EmptyState
                title="Курс не выбран"
                description="Выберите курс, чтобы видеть следующий урок на обзоре."
                action={
                  <Button size="sm" variant="secondary" onClick={() => navigate('/courses')}>
                    Открыть курсы
                  </Button>
                }
              />
            )}
          </Card>
        </div>

        <Card className="ov-note">
          {data.insight ? (
            <>
              <Card.Header
                title={data.insight.title}
                subtitle={data.insight.footnote || undefined}
                actions={
                  <Button size="sm" variant="ghost" onClick={() => navigate('/analytics')}>
                    Разбор
                  </Button>
                }
              />
              <p className="ov-note__text">{data.insight.text}</p>
            </>
          ) : (
            <>
              <Card.Header title="Наблюдение дня" />
              <p className="ov-note__text">
                Пока мало данных: связи появятся, когда наберётся не меньше двух
                недель с записями по обоим показателям.
              </p>
            </>
          )}
        </Card>
      </div>

      <nav className="ov-links" aria-label="Другие разделы">
        <a href="#/today">Все дела</a>
        <span aria-hidden="true">·</span>
        <a href="#/inbox">Инбокс</a>
        <span aria-hidden="true">·</span>
        <a href="#/compass">Компас</a>
        <span aria-hidden="true">·</span>
        <a href="#/analytics">Аналитика</a>
      </nav>
    </div>
  );
}
