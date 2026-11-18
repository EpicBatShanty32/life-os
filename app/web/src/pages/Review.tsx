/**
 * «Итоги» — пошаговый мастер недели и месяца (DESIGN-v4 §4, handoff §10.4).
 *
 * Пять шагов: факты от системы → три победы → незавершённое с решениями →
 * фокус и забота → сохранение. Факты всегда выше текстовых полей, числа
 * считает сервер, тексты — человек. История итогов — списком ниже.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  cancelTask,
  fetchReview,
  fetchReviews,
  formatPeriodRange,
  periodStart,
  saveReview,
  setTaskDue,
  shiftPeriod,
} from '../api/personal';
import type { ReviewOpenTask, ReviewPeriod, ReviewRecord, ReviewView } from '../api/personal';
import { Icon } from '../components/Icon';
import { Button, Card, EmptyState, Section, Segmented, Skeleton, Textarea } from '../components/ui';
import {
  addDays,
  formatDateShort,
  formatDateWithYear,
  formatNumber,
  parseIsoDate,
  pluralRu,
  toLocalIso,
} from '../lib/format';
import { apiErrorMessage } from '../api/errors';
import { toast } from '../lib/toast';
import '../styles/pages-insight.css';

const STEPS = [
  { id: 1, title: 'Факты', hint: 'что было' },
  { id: 2, title: 'Победы', hint: 'три главные' },
  { id: 3, title: 'Хвосты', hint: 'что не успелось' },
  { id: 4, title: 'Фокус', hint: 'на следующий период' },
  { id: 5, title: 'Сохранить', hint: 'итог и история' },
] as const;

type StepId = (typeof STEPS)[number]['id'];

interface FormState {
  wins: string[];
  misses: string;
  focus: string;
  care: string;
}

const EMPTY_FORM: FormState = { wins: ['', '', ''], misses: '', focus: '', care: '' };

function periodWord(period: ReviewPeriod): string {
  return period === 'week' ? 'Неделя' : 'Месяц';
}

function decisionsText(moved: number, cancelled: number): string {
  const parts: string[] = [];
  if (moved > 0) {
    parts.push(
      `перенёс ${moved} ${pluralRu(moved, ['задачу', 'задачи', 'задач'])} на следующий период`,
    );
  }
  if (cancelled > 0) {
    parts.push(`отменил ${cancelled} без вины`);
  }
  return parts.length ? `${parts.join(', ')}.` : '';
}

export function Review() {
  const today = toLocalIso();
  const [period, setPeriod] = useState<ReviewPeriod>('week');
  const [start, setStart] = useState<string>(() => periodStart(today, 'week'));
  const [step, setStep] = useState<StepId>(1);
  const [view, setView] = useState<ReviewView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<ReviewRecord[]>([]);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [decisions, setDecisions] = useState({ moved: 0, cancelled: 0 });
  const [openTasks, setOpenTasks] = useState<ReviewOpenTask[]>([]);
  const [busyTask, setBusyTask] = useState<number | null>(null);

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await fetchReviews(12));
    } catch {
      setHistory([]);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setSavedAt(null);
    setDecisions({ moved: 0, cancelled: 0 });
    fetchReview(period, start)
      .then((next) => {
        if (!alive) return;
        setView(next);
        setOpenTasks(next.openTasks);
        setForm(
          next.existing
            ? {
                wins: [
                  ...next.existing.wins.split('\n'),
                  '',
                  '',
                ].slice(0, 3),
                misses: next.existing.misses,
                focus: next.existing.focus,
                care: next.existing.care,
              }
            : EMPTY_FORM,
        );
      })
      .catch((problem: unknown) => {
        if (!alive) return;
        setError(apiErrorMessage(problem, 'Сервер не ответил'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [period, start]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const currentStart = useMemo(() => periodStart(today, period), [period, today]);
  const facts = view?.facts ?? null;
  const isCurrent = start === currentStart;

  const changePeriod = (next: ReviewPeriod) => {
    setPeriod(next);
    setStart(periodStart(today, next));
    setStep(1);
  };

  const movePeriod = (delta: number) => {
    setStart((previous) => shiftPeriod(previous, period, delta));
    setStep(1);
  };

  // Перенос — на первый день следующего периода, а не «конец + 7 дней».
  const nextPeriodStart = useCallback(
    (fromEnd: string): string => {
      if (period === 'month') return shiftPeriod(periodStart(fromEnd, 'month'), 'month', 1);
      return periodStart(toLocalIso(addDays(parseIsoDate(fromEnd), 1)), 'week');
    },
    [period],
  );

  const rescheduleTask = async (task: ReviewOpenTask) => {
    if (!view) return;
    const target = nextPeriodStart(view.endDate);
    setBusyTask(task.id);
    setOpenTasks((previous) => previous.filter((item) => item.id !== task.id));
    setDecisions((previous) => ({ ...previous, moved: previous.moved + 1 }));
    try {
      await setTaskDue(task.id, target);
      toast(`«${task.title}» — на ${formatDateShort(target)}`, 'success');
      setForm((previous) => ({
        ...previous,
        misses:
          previous.misses.trim() ||
          decisionsText(decisions.moved + 1, decisions.cancelled),
      }));
    } catch {
      toast('Не удалось перенести задачу', 'error');
      setOpenTasks((previous) => [task, ...previous]);
      setDecisions((previous) => ({ ...previous, moved: Math.max(0, previous.moved - 1) }));
    } finally {
      setBusyTask(null);
    }
  };

  const cancelOpenTask = async (task: ReviewOpenTask) => {
    setBusyTask(task.id);
    setOpenTasks((previous) => previous.filter((item) => item.id !== task.id));
    setDecisions((previous) => ({ ...previous, cancelled: previous.cancelled + 1 }));
    try {
      await cancelTask(task.id);
      toast(`«${task.title}» снята без вины`, 'success');
      setForm((previous) => ({
        ...previous,
        misses:
          previous.misses.trim() ||
          decisionsText(decisions.moved, decisions.cancelled + 1),
      }));
    } catch {
      toast('Не удалось отменить задачу', 'error');
      setOpenTasks((previous) => [task, ...previous]);
      setDecisions((previous) => ({
        ...previous,
        cancelled: Math.max(0, previous.cancelled - 1),
      }));
    } finally {
      setBusyTask(null);
    }
  };

  const save = async () => {
    if (!view || saving) return;
    setSaving(true);
    try {
      const record = await saveReview({
        period,
        startDate: view.startDate,
        wins: form.wins.map((line) => line.trim()).filter(Boolean).join('\n'),
        misses: form.misses.trim(),
        focus: form.focus.trim(),
        care: form.care.trim(),
      });
      toast('Итог сохранён', 'success');
      setView((previous) => (previous ? { ...previous, existing: record } : previous));
      setSavedAt(record.created || new Date().toISOString());
      await loadHistory();
    } catch {
      toast('Итог не сохранился — попробуйте ещё раз', 'error');
    } finally {
      setSaving(false);
    }
  };

  const setWin = (index: number, value: string) => {
    setForm((previous) => {
      const wins = [...previous.wins];
      wins[index] = value;
      return { ...previous, wins };
    });
  };

  if (error) {
    return (
      <div className="pi">
        <header className="pi-head">
          <div className="pi-head__text">
            <p className="pi-head__date">{formatDateWithYear(parseIsoDate(today))}</p>
            <h1 className="pi-head__title">Итоги</h1>
            <p className="pi-head__sub">Мастер недели и месяца</p>
          </div>
        </header>
        <p className="pi-quiet" role="status">
          Не удалось загрузить итоги: {error}
        </p>
      </div>
    );
  }

  return (
    <div className="pi">
      <header className="pi-head">
        <div className="pi-head__text">
          <p className="pi-head__date">{formatDateWithYear(parseIsoDate(today))}</p>
          <h1 className="pi-head__title">Итоги</h1>
          <p className="pi-head__sub">
            Пять шагов: факты, победы, незавершённое, фокус, сохранение
          </p>
        </div>
      </header>

      <div className="pi-controls">
        <Segmented
          ariaLabel="Период итогов"
          value={period}
          onChange={(next) => changePeriod(next)}
          options={[
            { value: 'week', label: 'Неделя' },
            { value: 'month', label: 'Месяц' },
          ]}
        />
        <div className="pi-periodnav">
          <button
            type="button"
            className="pi-iconbtn"
            aria-label="Предыдущий период"
            onClick={() => movePeriod(-1)}
          >
            <Icon name="chevron-left" size={16} />
          </button>
          <span className="pi-periodnav__label">
            {view ? formatPeriodRange(view.startDate, view.endDate) : '—'}
            {isCurrent ? <span className="pi-periodnav__tag">текущий</span> : null}
          </span>
          <button
            type="button"
            className="pi-iconbtn"
            aria-label="Следующий период"
            disabled={start >= currentStart}
            onClick={() => movePeriod(1)}
          >
            <Icon name="chevron-right" size={16} />
          </button>
        </div>
      </div>

      <ol className="pi-steps" aria-label="Шаги мастера">
        {STEPS.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              className="pi-step"
              data-active={step === item.id}
              data-done={step > item.id}
              aria-current={step === item.id ? 'step' : undefined}
              onClick={() => setStep(item.id)}
            >
              <span className="pi-step__num">{item.id}</span>
              <span className="pi-step__text">
                <span className="pi-step__title">{item.title}</span>
                <span className="pi-step__hint">{item.hint}</span>
              </span>
            </button>
          </li>
        ))}
      </ol>

      <Card>
        {loading || !view ? (
          <Skeleton variant="text" lines={3} label="Загружаю итоги периода" />
        ) : (
          <>
            {view.existing ? (
              <p className="pi-note">
                Итог за период уже сохранён — сохранение перезапишет текст.
                {view.existing.facts.demo
                  ? ' Запись пришла из демо-сида: после сохранения факты пересчитаются по базе.'
                  : ''}
              </p>
            ) : null}

            {step === 1 ? (
              <div className="pi-stepbody">
                <div className="pi-strip pi-strip--flush" role="group" aria-label="Факты периода">
                  <div className="pi-strip__cell">
                    <span className="pi-strip__label">Задачи</span>
                    <p className="pi-strip__value">
                      {facts ? `${facts.tasksDone}/${facts.tasksTotal}` : '—'}
                    </p>
                    <span className="pi-strip__hint">закрыто из запланированных</span>
                  </div>
                  <div className="pi-strip__cell">
                    <span className="pi-strip__label">Страницы</span>
                    <p className="pi-strip__value">
                      {facts ? formatNumber(facts.pagesRead) : '—'}
                    </p>
                    <span className="pi-strip__hint">прочитано за период</span>
                  </div>
                  <div className="pi-strip__cell">
                    <span className="pi-strip__label">Энергия</span>
                    <p className="pi-strip__value">
                      {facts?.energyAvg === null || !facts
                        ? '—'
                        : formatNumber(facts.energyAvg, 1)}
                    </p>
                    <span className="pi-strip__hint">
                      {facts?.energyAvg === null ? 'чек-инов не было' : 'средняя из 5'}
                    </span>
                  </div>
                  <div className="pi-strip__cell">
                    <span className="pi-strip__label">Привычки</span>
                    <p className="pi-strip__value">{facts ? `${facts.habitsPct}%` : '—'}</p>
                    <span className="pi-strip__hint">отметок от нормы</span>
                  </div>
                </div>
                <p className="pi-quiet">
                  Факты посчитаны сервером за {facts ? facts.days : 0}{' '}
                  {pluralRu(facts?.days ?? 0, ['день', 'дня', 'дней'])} периода. Их нельзя
                  поправить руками — только тексты ниже.
                </p>
                <p className="pi-quiet">
                  Незавершённых задач к концу периода:{' '}
                  <strong className="pi-num">{openTasks.length}</strong>. Разберём на третьем шаге.
                </p>
              </div>
            ) : null}

            {step === 2 ? (
              <div className="pi-stepbody">
                <p className="pi-quiet">Три главные победы периода — своими словами, коротко.</p>
                {[0, 1, 2].map((index) => (
                  <label className="pi-field" key={index}>
                    <span className="pi-field__label">Победа {index + 1}</span>
                    <input
                      className="ui-input"
                      value={form.wins[index] ?? ''}
                      maxLength={200}
                      placeholder={
                        index === 0
                          ? 'Что получилось лучше всего'
                          : index === 1
                            ? 'Что сдвинулось с места'
                            : 'Чего не ожидал(а) от себя'
                      }
                      onChange={(event) => setWin(index, event.target.value)}
                    />
                  </label>
                ))}
              </div>
            ) : null}

            {step === 3 ? (
              <div className="pi-stepbody">
                {openTasks.length === 0 ? (
                  <EmptyState
                    title="Незавершённых задач нет"
                    description="Всё запланированное на период закрыто — переносить нечего."
                  />
                ) : (
                  <>
                    <p className="pi-quiet">
                      Решение применяется сразу: перенос ставит новую дату, отмена снимает задачу
                      без вины.
                    </p>
                    <ul className="pi-tasks pi-tasks--wizard" role="list">
                      {openTasks.map((task) => (
                        <li className="pi-task" key={task.id}>
                          <span className="pi-task__title" title={task.title}>
                            {task.title}
                          </span>
                          <span className="pi-task__due">{formatDateShort(task.dueDate)}</span>
                          <span className="pi-task__actions">
                            <Button
                              size="sm"
                              variant="secondary"
                              loading={busyTask === task.id}
                              onClick={() => void rescheduleTask(task)}
                            >
                              {period === 'week' ? 'На следующую неделю' : 'На следующий месяц'}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={busyTask === task.id}
                              onClick={() => void cancelOpenTask(task)}
                            >
                              Отменить без вины
                            </Button>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
                <label className="pi-field">
                  <span className="pi-field__label">Что не успелось</span>
                  <Textarea
                    rows={3}
                    value={form.misses}
                    maxLength={800}
                    placeholder="Спокойно, без самобичевания: что переносится и почему"
                    onChange={(event) =>
                      setForm((previous) => ({ ...previous, misses: event.target.value }))
                    }
                  />
                </label>
              </div>
            ) : null}

            {step === 4 ? (
              <div className="pi-stepbody">
                <label className="pi-field">
                  <span className="pi-field__label">Фокус на следующий период</span>
                  <Textarea
                    rows={3}
                    value={form.focus}
                    maxLength={800}
                    placeholder="Одно главное направление и 1–2 конкретных шага"
                    onChange={(event) =>
                      setForm((previous) => ({ ...previous, focus: event.target.value }))
                    }
                  />
                </label>
                <label className="pi-field">
                  <span className="pi-field__label">Забота о себе</span>
                  <Textarea
                    rows={2}
                    value={form.care}
                    maxLength={800}
                    placeholder="Что вернёт ресурс: сон, прогулка, тишина"
                    onChange={(event) =>
                      setForm((previous) => ({ ...previous, care: event.target.value }))
                    }
                  />
                </label>
              </div>
            ) : null}

            {step === 5 ? (
              <div className="pi-stepbody">
                <dl className="pi-review">
                  <div className="pi-review__row">
                    <dt>Период</dt>
                    <dd>
                      {periodWord(period)}, {formatPeriodRange(view.startDate, view.endDate)}
                    </dd>
                  </div>
                  <div className="pi-review__row">
                    <dt>Победы</dt>
                    <dd>
                      {form.wins.filter((line) => line.trim()).length ? (
                        <ol className="pi-review__list">
                          {form.wins
                            .filter((line) => line.trim())
                            .map((line, index) => (
                              <li key={index}>{line}</li>
                            ))}
                        </ol>
                      ) : (
                        '—'
                      )}
                    </dd>
                  </div>
                  <div className="pi-review__row">
                    <dt>Что не успелось</dt>
                    <dd>{form.misses.trim() || '—'}</dd>
                  </div>
                  <div className="pi-review__row">
                    <dt>Фокус</dt>
                    <dd>{form.focus.trim() || '—'}</dd>
                  </div>
                  <div className="pi-review__row">
                    <dt>Забота</dt>
                    <dd>{form.care.trim() || '—'}</dd>
                  </div>
                  <div className="pi-review__row">
                    <dt>Факты</dt>
                    <dd>
                      {facts ? (
                        <div className="pi-strip pi-strip--flush" role="group" aria-label="Факты итога">
                          <div className="pi-strip__cell">
                            <span className="pi-strip__label">Задачи</span>
                            <p className="pi-strip__value">
                              {facts.tasksDone}/{facts.tasksTotal}
                            </p>
                          </div>
                          <div className="pi-strip__cell">
                            <span className="pi-strip__label">Страницы</span>
                            <p className="pi-strip__value">{formatNumber(facts.pagesRead)}</p>
                          </div>
                          <div className="pi-strip__cell">
                            <span className="pi-strip__label">Энергия</span>
                            <p className="pi-strip__value">
                              {facts.energyAvg === null ? '—' : formatNumber(facts.energyAvg, 1)}
                            </p>
                          </div>
                          <div className="pi-strip__cell">
                            <span className="pi-strip__label">Привычки</span>
                            <p className="pi-strip__value">{facts.habitsPct}%</p>
                          </div>
                        </div>
                      ) : (
                        '—'
                      )}
                    </dd>
                  </div>
                </dl>
                {savedAt ? (
                  <p className="pi-note pi-note--ok" role="status">
                    Итог сохранён. Он появился в истории ниже — его можно открыть и дописать.
                  </p>
                ) : null}
              </div>
            ) : null}

            <div className="pi-wizard__foot">
              <Button
                size="md"
                variant="ghost"
                leadingIcon="arrow-left"
                disabled={step === 1}
                onClick={() => setStep((previous) => (previous > 1 ? ((previous - 1) as StepId) : previous))}
              >
                Назад
              </Button>
              {step < 5 ? (
                <Button
                  size="md"
                  variant="primary"
                  trailingIcon="arrow-right"
                  onClick={() => setStep((previous) => ((previous + 1) as StepId))}
                >
                  Дальше
                </Button>
              ) : (
                <Button
                  size="md"
                  variant="primary"
                  loading={saving}
                  onClick={() => void save()}
                >
                  Сохранить итог
                </Button>
              )}
            </div>
          </>
        )}
      </Card>

      <Section
        title="История итогов"
        description="Последние сохранённые периоды. Нажмите на строку, чтобы открыть её в мастере."
      >
        {history.length === 0 ? (
          <EmptyState
            title="Итогов пока нет"
            description="Сохраните первый итог — он появится здесь и будет доступен для правок."
          />
        ) : (
          <ul className="pi-history" role="list">
            {history.map((record) => (
              <li key={`${record.period}-${record.startDate}`}>
                <button
                  type="button"
                  className="pi-history__row"
                  onClick={() => {
                    setPeriod(record.period);
                    setStart(record.startDate);
                    setStep(5);
                  }}
                >
                  <span className="pi-history__period">
                    {periodWord(record.period)}
                    <span className="pi-history__range">
                      {formatPeriodRange(record.startDate, record.endDate)}
                    </span>
                  </span>
                  <span className="pi-history__text">
                    {record.wins.split('\n').filter(Boolean).join('; ') || 'Без текста побед'}
                  </span>
                  <span className="pi-history__facts pi-num">
                    {record.facts.tasksDone}/{record.facts.tasksTotal} задач, привычки{' '}
                    {record.facts.habitsPct}%
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
