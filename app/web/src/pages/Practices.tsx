import '../styles/pages-compass.css';
import '../styles/practices.css';
import { useMemo, useRef, useState } from 'react';
import { submitPracticeSession, usePointsHistory, usePractices } from '../api/practices';
import type {
  PointsDay,
  Practice,
  PracticeKind,
  PracticeSessionInput,
} from '../api/practices';
import { holdLabel } from '../components/practices/timeline';
import { PracticePlayer } from '../components/practices/PracticePlayer';
import {
  InlineError,
  PageHead,
  SkeletonRows,
  longToday,
} from '../components/pages-compass/shared';
import { Button, EmptyState, Segmented } from '../components/ui';
import { formatNumber, pluralRu } from '../lib/format';
import { toast } from '../lib/toast';

/* ─────────────────────────────────────────────────────────────
   Экран «Практики»: каталог и вход в плеер.

   Данные — живой API /api/practices: шесть практик из базы, личные
   цифры (сколько раз, серия, рекорд) и состояние очков считает
   сервер. Экран их только показывает и не спорит с /api/points.
   ───────────────────────────────────────────────────────────── */

type Filter = 'all' | PracticeKind;

const KIND_ORDER: readonly PracticeKind[] = ['breathing', 'stretch', 'other'];

const KIND_LABEL: Record<PracticeKind, string> = {
  breathing: 'Дыхание',
  stretch: 'Растяжка',
  other: 'Другое',
};

function roundWord(count: number): string {
  return `${count} ${pluralRu(count, ['раунд', 'раунда', 'раундов'])}`;
}

function dayWord(count: number): string {
  return `${count} ${pluralRu(count, ['день', 'дня', 'дней'])}`;
}

interface PracticeRowProps {
  practice: Practice;
  onStart: (practice: Practice) => void;
}

function PracticeRow({ practice, onStart }: PracticeRowProps) {
  const rounds = practice.rounds.length;
  const record =
    practice.recordHoldSec && practice.recordHoldSec > 0
      ? `рекорд ${practice.recordLabel ?? holdLabel(practice.recordHoldSec)}`
      : 'рекорда пока нет';
  const streak = practice.streakDays > 0 ? `серия ${dayWord(practice.streakDays)}` : 'серии нет';
  const times = practice.doneCount > 0 ? `· ${practice.doneCount} раз` : '';

  return (
    <li className="pp-row" data-today={practice.todayDone || undefined}>
      <div className="pp-row__main">
        <div className="pp-row__heading">
          <span className="pp-row__dot" data-kind={practice.kind} aria-hidden="true" />
          <h3 className="pp-row__title">{practice.title}</h3>
          {practice.todayDone ? <span className="pp-row__flag">сегодня</span> : null}
        </div>
        <p className="pp-row__meta">
          {KIND_LABEL[practice.kind]} · {practice.minutes} мин · {roundWord(rounds)}
        </p>
        <p className="pp-row__stats">
          {record} · {streak} {times}
        </p>
      </div>
      <Button
        variant="secondary"
        size="sm"
        onClick={() => onStart(practice)}
        aria-label={`Начать: ${practice.title}`}
      >
        Начать
      </Button>
    </li>
  );
}

interface HistoryProps {
  days: readonly PointsDay[];
  sum: number;
  bestDay: number;
  cap: number;
}

/** Полоски за 30 дней: масштаб — кап дня, иначе пики съедают всю картинку. */
function PointsHistoryChart({ days, sum, bestDay, cap }: HistoryProps) {
  if (days.length === 0) return null;
  const scale = Math.max(cap, bestDay, 1);
  const first = days[0].date;
  const last = days[days.length - 1].date;
  return (
    <section className="pp-history" aria-labelledby="pp-history-title">
      <div className="pp-history__head">
        <h2 className="pp-history__title" id="pp-history-title">
          30 дней
        </h2>
        <span className="pp-history__sum">+{formatNumber(sum)}</span>
      </div>
      <div
        className="pp-history__bars"
        role="img"
        aria-label={`Очки по дням с ${first} по ${last}: всего ${sum}, лучший день ${bestDay}`}
      >
        {days.map((day) => (
          <span
            className="pp-history__bar"
            key={day.date}
            data-zero={day.points === 0 || undefined}
            data-today={day.date === days[days.length - 1].date || undefined}
            style={{ height: `${Math.max(2, Math.round((day.points / scale) * 100))}%` }}
          />
        ))}
      </div>
      <p className="pp-history__legend">
        лучший день {formatNumber(bestDay)} из {cap}
      </p>
    </section>
  );
}

export function Practices() {
  const practices = usePractices();
  const history = usePointsHistory(30);
  const [filter, setFilter] = useState<Filter>('all');
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const items = practices.data?.items ?? [];
  const points = practices.data?.points ?? null;
  const active = useMemo(
    () => items.find((item) => item.slug === activeSlug) ?? null,
    [items, activeSlug],
  );

  const filters = useMemo<ReadonlyArray<{ value: Filter; label: string }>>(
    () => [
      { value: 'all', label: 'Все' },
      ...KIND_ORDER.filter((kind) => items.some((item) => item.kind === kind)).map((kind) => ({
        value: kind as Filter,
        label: KIND_LABEL[kind],
      })),
    ],
    [items],
  );

  const groups = useMemo(
    () =>
      KIND_ORDER.map((kind) => ({
        kind,
        items: items.filter((item) => item.kind === kind),
      })).filter((group) => group.items.length > 0),
    [items],
  );

  const filtered = filter === 'all' ? items : items.filter((item) => item.kind === filter);

  function openPractice(practice: Practice) {
    openerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setActiveSlug(practice.slug);
  }

  function closePlayer() {
    setActiveSlug(null);
    openerRef.current?.focus();
  }

  async function submit(input: PracticeSessionInput) {
    if (!active) throw new Error('Практика не выбрана');
    const result = await submitPracticeSession(active, input);
    practices.refresh();
    history.refresh();
    if (result.pointsEarned > 0) {
      toast(`Практика записана: +${result.pointsEarned} очков`, 'success');
    } else if (result.duplicated) {
      toast('Практика уже учтена сегодня — очки не начислены');
    }
    return result;
  }

  return (
    <div className="pc-page pp-page">
      <PageHead
        date={longToday()}
        title="Практики"
        note="Дыхание, растяжка и холод: короткие практики тела, а не ввод данных."
      />

      <div className="pp-layout">
        <div className="pp-main">
          <div className="pp-filter">
            <Segmented
              options={filters}
              value={filter}
              onChange={setFilter}
              ariaLabel="Тип практики"
            />
          </div>

          {practices.loading && items.length === 0 ? (
            <SkeletonRows rows={4} label="Загружаем практики" />
          ) : practices.error ? (
            <InlineError text="Не удалось загрузить практики" onRetry={practices.refresh} />
          ) : filtered.length === 0 ? (
            <EmptyState
              title="Здесь пока пусто"
              description="В этом типе практик ещё нет — загляните в «Все»."
            />
          ) : filter === 'all' ? (
            groups.map((group) => (
              <section
                className="pp-group"
                key={group.kind}
                aria-labelledby={`pp-group-${group.kind}`}
              >
                <div className="pp-group__head">
                  <h2 className="pp-group__title" id={`pp-group-${group.kind}`}>
                    {KIND_LABEL[group.kind]}
                  </h2>
                  <span className="pp-group__count">{group.items.length}</span>
                </div>
                <ul className="pp-list">
                  {group.items.map((practice) => (
                    <PracticeRow key={practice.slug} practice={practice} onStart={openPractice} />
                  ))}
                </ul>
              </section>
            ))
          ) : (
            <ul className="pp-list">
              {filtered.map((practice) => (
                <PracticeRow key={practice.slug} practice={practice} onStart={openPractice} />
              ))}
            </ul>
          )}
        </div>

        <aside className="pp-aside" aria-label="Очки и серия">
          <div className="pp-strip">
            <div className="pp-strip__cell">
              <span className="pp-strip__label">Очки</span>
              <span className="pp-strip__value">{points ? points.balance : '—'}</span>
              <span className="pp-strip__hint">
                {points
                  ? points.level.next
                    ? `${points.level.title} · до «${points.level.next.title}» ${formatNumber(points.level.toNext)}`
                    : points.level.title
                  : 'считаем'}
              </span>
              {points && points.level.next ? (
                <span className="pp-level" aria-hidden="true">
                  <span
                    className="pp-level__fill"
                    style={{ width: `${Math.round(points.level.progress * 100)}%` }}
                  />
                </span>
              ) : null}
            </div>
            <div className="pp-strip__cell">
              <span className="pp-strip__label">Сегодня</span>
              <span className="pp-strip__value">
                {points ? points.cap.today : '—'}
                <span className="pp-strip__unit"> / {points ? points.cap.day : 60}</span>
              </span>
              <span className="pp-strip__hint">кап в день</span>
            </div>
            <div className="pp-strip__cell">
              <span className="pp-strip__label">Серия</span>
              <span className="pp-strip__value">{points ? points.streak.days : '—'}</span>
              <span className="pp-strip__hint">
                {points
                  ? `лучшая ${dayWord(points.streak.best)}`
                  : 'дней подряд'}
              </span>
            </div>
          </div>

          {history.data ? (
            <PointsHistoryChart
              days={history.data.days}
              sum={history.data.sum}
              bestDay={history.data.bestDay}
              cap={history.data.cap.day}
            />
          ) : null}

          <p className="pp-foot-note">
            За практику — 10 очков, за новый рекорд задержки — плюс 5, за день серии — плюс 2.
            Очки складываются не больше {points ? points.cap.day : 60} в день; одна практика в день
            засчитывается один раз.
          </p>
        </aside>
      </div>

      {active ? (
        <PracticePlayer
          key={active.slug}
          practice={active}
          onSubmit={submit}
          onClose={closePlayer}
        />
      ) : null}
    </div>
  );
}
