import '../styles/pages-compass.css';
import { useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import {
  createAntiGoal,
  createGoal,
  createGoalStep,
  deleteAntiGoal,
  deleteGoal,
  deleteGoalStep,
  patchAntiGoal,
  patchGoal,
  patchGoalStep,
} from '../api/compass';
import type {
  AntiGoal,
  AntiPeriod,
  Goal,
  GoalInput,
  GoalStep,
  Horizon,
} from '../api/compass';
import { apiErrorMessage } from '../api/errors';
import { toast } from '../lib/toast';
import { pluralRu } from '../lib/format';
import { GoalSheet } from '../components/pages-compass/GoalSheet';
import { SpheresSheet } from '../components/pages-compass/SpheresSheet';
import { useCompass } from '../api/compass';
import { Button, Checkbox, DateInput, EmptyState, Icon, Input, ProgressBar, Section, Segmented, Select } from '../components/ui';
import {
  HORIZON_LABEL,
  InlineError,
  PageHead,
  SkeletonRows,
  SphereDot,
  longToday,
  shortDate,
} from '../components/pages-compass/shared';

const HORIZON_OPTIONS = (['year', 'quarter', 'month'] as const).map((value) => ({
  value,
  label: HORIZON_LABEL[value],
}));

function daysSince(iso: string): number {
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return 0;
  return Math.max(0, Math.round((Date.now() - time) / 86_400_000));
}

function goalPercent(input: GoalInput, base: Goal | null): number {
  const manual = input.progress ?? 0;
  if (manual > 0) return Math.min(100, manual);
  if (!base) return 0;
  if (base.counts.stepsTotal > 0) {
    return Math.round((base.counts.stepsDone * 100) / base.counts.stepsTotal);
  }
  if (base.counts.tasksTotal > 0) {
    return Math.round((base.counts.tasksDone * 100) / base.counts.tasksTotal);
  }
  return 0;
}

function countWord(count: number, forms: readonly [string, string, string]): string {
  return `${count} ${pluralRu(count, forms)}`;
}

/** Процент по вехам или задачам — то же правило, что на сервере при progress = 0. */
function autoPercent(goal: Goal): number {
  if (goal.progress > 0) return goal.progressPct;
  if (goal.counts.stepsTotal > 0) {
    return Math.round((goal.counts.stepsDone * 100) / goal.counts.stepsTotal);
  }
  if (goal.counts.tasksTotal > 0) {
    return Math.round((goal.counts.tasksDone * 100) / goal.counts.tasksTotal);
  }
  return 0;
}

/** Пересчитывает авто-прогресс после изменения вех, не трогая ручной. */
function withCounts(goal: Goal, counts: Goal['counts']): Goal {
  return { ...goal, counts, progressPct: autoPercent({ ...goal, counts }) };
}

function deadlineLabel(goal: Goal): string {
  if (!goal.deadline) return 'без срока';
  return `до ${shortDate(goal.deadline)}`;
}

function GoalRow({
  goal,
  expanded,
  onToggleExpand,
  onEdit,
  onToggleStep,
  onDeleteStep,
  onAddStep,
}: {
  goal: Goal;
  expanded: boolean;
  onToggleExpand: () => void;
  onEdit: () => void;
  onToggleStep: (step: GoalStep) => void;
  onDeleteStep: (step: GoalStep) => void;
  onAddStep: (title: string, due: string) => Promise<boolean>;
}) {
  const [stepTitle, setStepTitle] = useState('');
  const [stepDue, setStepDue] = useState('');
  const [adding, setAdding] = useState(false);
  const regionId = `goal-steps-${goal.id}`;
  const meta = [
    goal.sphere?.name ?? 'Без сферы',
    deadlineLabel(goal),
    goal.counts.stepsTotal > 0
      ? `${goal.counts.stepsDone}/${goal.counts.stepsTotal} вех`
      : 'без вех',
  ];

  async function addStep(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const title = stepTitle.trim();
    if (!title || adding) return;
    setAdding(true);
    const ok = await onAddStep(title, stepDue);
    setAdding(false);
    if (ok) {
      setStepTitle('');
      setStepDue('');
    }
  }

  return (
    <li className="pc-goal" data-status={goal.status}>
      <div className="pc-goal__row">
        <button
          type="button"
          className="pc-goal__toggle"
          aria-expanded={expanded}
          aria-controls={regionId}
          onClick={onToggleExpand}
        >
          <SphereDot color={goal.sphere?.color} />
          <span className="pc-goal__title">{goal.title}</span>
          <span className="pc-goal__meta">{meta.join(' · ')}</span>
          <Icon name={expanded ? 'chevron-up' : 'chevron-down'} size={16} />
        </button>
        <div className="pc-goal__progress">
          <ProgressBar
            value={goal.progressPct}
            showValue
            tone="cat-1"
            ariaLabel={`Прогресс цели ${goal.title}`}
          />
          {goal.progress > 0 ? <span className="pc-goal__manual">ручной</span> : null}
        </div>
        <button
          type="button"
          className="pc-goal__edit"
          aria-label={`Редактировать цель ${goal.title}`}
          title="Редактировать цель"
          onClick={onEdit}
        >
          <Icon name="edit" size={16} />
        </button>
      </div>

      {expanded ? (
        <div className="pc-goal__body" id={regionId}>
          {goal.counts.tasksTotal > 0 ? (
            <p className="pc-goal__tasks">
              Связанных задач: {goal.counts.tasksDone} из {goal.counts.tasksTotal} закрыто
            </p>
          ) : null}
          {goal.steps.length > 0 ? (
            <ol className="pc-steps">
              {goal.steps.map((step) => (
                <li className="pc-step" key={step.id}>
                  <Checkbox
                    size="sm"
                    checked={step.done}
                    onChange={() => onToggleStep(step)}
                    label={<span className="pc-step__title">{step.title}</span>}
                  />
                  {step.due ? <span className="pc-step__due">{shortDate(step.due)}</span> : null}
                  <button
                    type="button"
                    className="pc-row-action"
                    aria-label={`Удалить веху ${step.title}`}
                    title="Удалить веху"
                    onClick={() => onDeleteStep(step)}
                  >
                    <Icon name="trash" size={15} />
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="pc-muted">Вех пока нет. Добавьте 2–4 шага — по ним посчитается прогресс.</p>
          )}
          <form className="pc-step-add" onSubmit={addStep}>
            <Input
              size="sm"
              value={stepTitle}
              onChange={(event) => setStepTitle(event.target.value)}
              placeholder="Новая веха"
              aria-label={`Новая веха для цели ${goal.title}`}
              maxLength={300}
            />
            <DateInput
              size="sm"
              value={stepDue}
              onChange={(event) => setStepDue(event.target.value)}
              aria-label="Срок вехи"
            />
            <Button type="submit" size="sm" variant="secondary" loading={adding}>
              Добавить
            </Button>
          </form>
        </div>
      ) : null}
    </li>
  );
}

export function Compass() {
  const compass = useCompass();
  const [horizon, setHorizon] = useState<Horizon>('year');
  const [sphereFilter, setSphereFilter] = useState<string>('all');
  const [antiPeriod, setAntiPeriod] = useState<AntiPeriod>('month');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [sheetGoal, setSheetGoal] = useState<Goal | 'new' | null>(null);
  const [spheresOpen, setSpheresOpen] = useState(false);
  const [antiText, setAntiText] = useState('');
  const [antiSaving, setAntiSaving] = useState(false);

  const data = compass.data;
  const goals = data?.goals ?? [];
  const spheres = data?.spheres ?? [];
  const visibleGoals = useMemo(
    () =>
      goals.filter(
        (goal) =>
          goal.horizon === horizon &&
          (sphereFilter === 'all' || String(goal.sphereId) === sphereFilter),
      ),
    [goals, horizon, sphereFilter],
  );
  const averageProgress =
    visibleGoals.length > 0
      ? Math.round(
          visibleGoals.reduce((sum, goal) => sum + goal.progressPct, 0) / visibleGoals.length,
        )
      : 0;
  const staleCount = visibleGoals.filter(
    (goal) => goal.status === 'active' && goal.progressPct === 0 && daysSince(goal.created) > 14,
  ).length;
  const antiGoals = data?.antiGoals[antiPeriod] ?? [];

  function patchGoalInState(saved: Goal) {
    compass.setData((previous) =>
      previous
        ? {
            ...previous,
            goals: previous.goals.some((goal) => goal.id === saved.id)
              ? previous.goals.map((goal) => (goal.id === saved.id ? saved : goal))
              : [...previous.goals, saved],
          }
        : previous,
    );
  }

  async function saveGoal(input: GoalInput): Promise<Goal | null> {
    const editing = sheetGoal !== 'new' ? sheetGoal : null;
    if (editing) {
      const previous = editing;
      patchGoalInState({
        ...editing,
        ...input,
        progressPct: goalPercent(input, editing),
        sphere: input.sphereId
          ? (spheres.find((sphere) => sphere.id === input.sphereId) ?? null)
          : null,
        steps: editing.steps,
        counts: editing.counts,
      } as Goal);
      try {
        const saved = await patchGoal(editing.id, input);
        patchGoalInState(saved);
        return saved;
      } catch (problem: unknown) {
        patchGoalInState(previous);
        toast(apiErrorMessage(problem, 'Не удалось сохранить цель'), 'error');
        return null;
      }
    }

    const now = new Date().toISOString();
    const temp: Goal = {
      id: -Date.now(),
      title: input.title,
      horizon: input.horizon ?? 'year',
      sphereId: input.sphereId ?? null,
      sphere: input.sphereId
        ? (spheres.find((sphere) => sphere.id === input.sphereId) ?? null)
        : null,
      parentId: input.parentId ?? null,
      deadline: input.deadline ?? null,
      status: input.status ?? 'active',
      progress: input.progress ?? 0,
      progressPct: input.progress ?? 0,
      why: input.why ?? '',
      target: null,
      current: 0,
      unit: '',
      steps: [],
      counts: { stepsDone: 0, stepsTotal: 0, tasksDone: 0, tasksTotal: 0 },
      created: now,
    };
    compass.setData((previous) =>
      previous ? { ...previous, goals: [...previous.goals, temp] } : previous,
    );
    try {
      const saved = await createGoal(input);
      compass.setData((previous) =>
        previous
          ? {
              ...previous,
              goals: previous.goals.map((goal) => (goal.id === temp.id ? saved : goal)),
            }
          : previous,
      );
      return saved;
    } catch (problem: unknown) {
      compass.setData((previous) =>
        previous
          ? { ...previous, goals: previous.goals.filter((goal) => goal.id !== temp.id) }
          : previous,
      );
      toast(apiErrorMessage(problem, 'Не удалось создать цель'), 'error');
      return null;
    }
  }

  async function removeGoal(): Promise<boolean> {
    const editing = sheetGoal !== 'new' ? sheetGoal : null;
    if (!editing) return false;
    const snapshot = goals;
    compass.setData((previous) =>
      previous
        ? { ...previous, goals: previous.goals.filter((goal) => goal.id !== editing.id) }
        : previous,
    );
    try {
      await deleteGoal(editing.id);
      toast('Цель удалена', 'default');
      return true;
    } catch (problem: unknown) {
      compass.setData((previous) => (previous ? { ...previous, goals: snapshot } : previous));
      toast(apiErrorMessage(problem, 'Не удалось удалить цель'), 'error');
      return false;
    }
  }

  async function toggleStep(step: GoalStep) {
    const done = !step.done;
    const update = (value: boolean) => {
      compass.setData((previous) =>
        previous
          ? {
              ...previous,
              goals: previous.goals.map((goal) =>
                goal.id !== step.goalId
                  ? goal
                  : {
                      ...withCounts(goal, {
                        ...goal.counts,
                        stepsDone: Math.max(0, goal.counts.stepsDone + (value ? 1 : -1)),
                      }),
                      steps: goal.steps.map((item) =>
                        item.id === step.id ? { ...item, done: value } : item,
                      ),
                    },
              ),
            }
          : previous,
      );
    };
    update(done);
    try {
      const saved = await patchGoalStep(step.id, { done });
      compass.setData((previous) =>
        previous
          ? {
              ...previous,
              goals: previous.goals.map((goal) =>
                goal.id !== saved.goalId
                  ? goal
                  : {
                      ...goal,
                      steps: goal.steps.map((item) => (item.id === saved.id ? saved : item)),
                    },
              ),
            }
          : previous,
      );
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось изменить веху'), 'error');
      compass.reload();
    }
  }

  async function addStep(goalId: number, title: string, due: string): Promise<boolean> {
    const temp: GoalStep = {
      id: -Date.now(),
      goalId,
      title,
      done: false,
      due: due || null,
      sort: Number.MAX_SAFE_INTEGER,
    };
    compass.setData((previous) =>
      previous
        ? {
            ...previous,
            goals: previous.goals.map((goal) =>
              goal.id !== goalId
                ? goal
                : {
                    ...withCounts(goal, {
                      ...goal.counts,
                      stepsTotal: goal.counts.stepsTotal + 1,
                    }),
                    steps: [...goal.steps, temp],
                  },
            ),
          }
        : previous,
    );
    try {
      const saved = await createGoalStep(goalId, { title, due: due || null });
      compass.setData((previous) =>
        previous
          ? {
              ...previous,
              goals: previous.goals.map((goal) =>
                goal.id !== goalId
                  ? goal
                  : {
                      ...goal,
                      steps: goal.steps.map((step) => (step.id === temp.id ? saved : step)),
                    },
              ),
            }
          : previous,
      );
      return true;
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось добавить веху'), 'error');
      compass.reload();
      return false;
    }
  }

  async function removeStep(step: GoalStep) {
    compass.setData((previous) =>
      previous
        ? {
            ...previous,
            goals: previous.goals.map((goal) =>
              goal.id !== step.goalId
                ? goal
                : {
                    ...withCounts(goal, {
                      ...goal.counts,
                      stepsTotal: Math.max(0, goal.counts.stepsTotal - 1),
                      stepsDone: Math.max(0, goal.counts.stepsDone - (step.done ? 1 : 0)),
                    }),
                    steps: goal.steps.filter((item) => item.id !== step.id),
                  },
            ),
          }
        : previous,
    );
    try {
      await deleteGoalStep(step.id);
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось удалить веху'), 'error');
      compass.reload();
    }
  }

  async function addAntiGoal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = antiText.trim();
    if (!text || antiSaving) return;
    setAntiSaving(true);
    const temp: AntiGoal = {
      id: -Date.now(),
      period: antiPeriod,
      text,
      active: true,
      created: new Date().toISOString(),
    };
    compass.setData((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        antiGoals: {
          ...previous.antiGoals,
          [antiPeriod]: [...previous.antiGoals[antiPeriod], temp],
        },
      };
    });
    setAntiText('');
    try {
      const saved = await createAntiGoal({ period: antiPeriod, text });
      compass.setData((previous) => {
        if (!previous) return previous;
        return {
          ...previous,
          antiGoals: {
            ...previous.antiGoals,
            [antiPeriod]: previous.antiGoals[antiPeriod].map((item) =>
              item.id === temp.id ? saved : item,
            ),
          },
        };
      });
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось добавить отказ'), 'error');
      compass.reload();
    } finally {
      setAntiSaving(false);
    }
  }

  async function toggleAnti(goal: AntiGoal) {
    const next = !goal.active;
    const patch = (active: boolean) =>
      compass.setData((previous) =>
        previous
          ? {
              ...previous,
              antiGoals: {
                ...previous.antiGoals,
                [goal.period]: previous.antiGoals[goal.period].map((item) =>
                  item.id === goal.id ? { ...item, active } : item,
                ),
              },
            }
          : previous,
      );
    patch(next);
    try {
      await patchAntiGoal(goal.id, { active: next });
    } catch (problem: unknown) {
      patch(goal.active);
      toast(apiErrorMessage(problem, 'Не удалось изменить отказ'), 'error');
    }
  }

  async function removeAnti(goal: AntiGoal) {
    compass.setData((previous) =>
      previous
        ? {
            ...previous,
            antiGoals: {
              ...previous.antiGoals,
              [goal.period]: previous.antiGoals[goal.period].filter(
                (item) => item.id !== goal.id,
              ),
            },
          }
        : previous,
    );
    try {
      await deleteAntiGoal(goal.id);
    } catch (problem: unknown) {
      toast(apiErrorMessage(problem, 'Не удалось удалить отказ'), 'error');
      compass.reload();
    }
  }

  const sheetGoalValue = sheetGoal === 'new' ? null : sheetGoal;
  const horizonLabel = HORIZON_LABEL[horizon].toLocaleLowerCase('ru-RU');

  return (
    <div className="pc-page">
      <PageHead
        title="Компас"
        date={longToday()}
        note={`${countWord(visibleGoals.length, ['цель', 'цели', 'целей'])} на ${horizonLabel} · ${countWord(spheres.length, ['сфера', 'сферы', 'сфер'])} · прогресс по вехам`}
        actions={
          <>
            <Button
              variant="ghost"
              size="sm"
              leadingIcon="settings"
              onClick={() => setSpheresOpen(true)}
            >
              Сферы
            </Button>
            <Button
              variant="secondary"
              size="sm"
              leadingIcon="plus"
              onClick={() => setSheetGoal('new')}
            >
              Новая цель
            </Button>
          </>
        }
      />

      <dl className="pc-summary" aria-label="Сводка по горизонту">
        <div className="pc-stat">
          <dt>Целей на горизонте</dt>
          <dd>{visibleGoals.length}</dd>
        </div>
        <div className="pc-stat">
          <dt>Средний прогресс</dt>
          <dd>
            {averageProgress}
            <span className="pc-stat__unit">%</span>
          </dd>
        </div>
        <div className="pc-stat">
          <dt>Без движения</dt>
          <dd>
            {staleCount}
            <span className="pc-stat__unit">{pluralRu(staleCount, ['цель', 'цели', 'целей'])}</span>
            <span className="pc-stat__hint">14+ дней</span>
          </dd>
        </div>
      </dl>

      <div className="pc-toolbar">
        <Segmented
          options={HORIZON_OPTIONS}
          value={horizon}
          onChange={(value) => setHorizon(value as Horizon)}
          ariaLabel="Горизонт целей"
        />
        <div className="pc-toolbar__right">
          <Select
            size="sm"
            value={sphereFilter}
            onChange={(event) => setSphereFilter(event.target.value)}
            options={[
              { value: 'all', label: 'Все сферы' },
              ...spheres.map((sphere) => ({ value: String(sphere.id), label: sphere.name })),
            ]}
            aria-label="Фильтр по сфере"
          />
        </div>
      </div>

      <div className="pc-compass__layout">
        <section className="pc-block" aria-labelledby="compass-goals-title">
          <div className="pc-block__head">
            <h2 className="pc-block__title" id="compass-goals-title">
              Цели · {HORIZON_LABEL[horizon]}
            </h2>
            <span className="pc-block__count">{visibleGoals.length}</span>
          </div>

          {compass.loading && !data ? (
            <SkeletonRows rows={4} label="Загрузка целей" />
          ) : compass.error ? (
            <InlineError text={compass.error} onRetry={compass.reload} />
          ) : visibleGoals.length === 0 ? (
            <EmptyState
              title="На этот горизонт целей нет"
              description="Год задаёт направление, квартал — измеримый результат, месяц — 3–5 вех."
              action={
                <Button variant="secondary" size="sm" onClick={() => setSheetGoal('new')}>
                  Добавить цель
                </Button>
              }
            />
          ) : (
            <ul className="pc-goals">
              {visibleGoals.map((goal) => (
                <GoalRow
                  key={goal.id}
                  goal={goal}
                  expanded={expanded.has(goal.id)}
                  onToggleExpand={() =>
                    setExpanded((previous) => {
                      const next = new Set(previous);
                      if (next.has(goal.id)) next.delete(goal.id);
                      else next.add(goal.id);
                      return next;
                    })
                  }
                  onEdit={() => setSheetGoal(goal)}
                  onToggleStep={(step) => {
                    void toggleStep(step);
                  }}
                  onDeleteStep={(step) => {
                    void removeStep(step);
                  }}
                  onAddStep={(title, due) => addStep(goal.id, title, due)}
                />
              ))}
            </ul>
          )}
        </section>

        <aside className="pc-compass__aside">
          <Section
            title="Что не делаю"
            description={`Сознательные отказы на ${antiPeriod === 'month' ? 'месяц' : 'квартал'}.`}
            actions={
              <Segmented
                options={[
                  { value: 'month', label: 'Месяц' },
                  { value: 'quarter', label: 'Квартал' },
                ]}
                value={antiPeriod}
                onChange={(value) => setAntiPeriod(value as AntiPeriod)}
                ariaLabel="Период анти-целей"
              />
            }
          >
            {antiGoals.length === 0 ? (
              <p className="pc-muted">Пока пусто. Первый отказ — тоже рамка.</p>
            ) : (
              <ul className="pc-anti">
                {antiGoals.map((item) => (
                  <li className={item.active ? 'pc-anti__item' : 'pc-anti__item is-off'} key={item.id}>
                    <Checkbox
                      size="sm"
                      checked={item.active}
                      onChange={() => {
                        void toggleAnti(item);
                      }}
                      label={<span className="pc-anti__text">{item.text}</span>}
                    />
                    <button
                      type="button"
                      className="pc-row-action"
                      aria-label={`Удалить отказ ${item.text}`}
                      title="Удалить"
                      onClick={() => {
                        void removeAnti(item);
                      }}
                    >
                      <Icon name="trash" size={15} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <form className="pc-anti__add" onSubmit={addAntiGoal}>
              <Input
                size="sm"
                value={antiText}
                onChange={(event) => setAntiText(event.target.value)}
                placeholder="Не брать больше двух проектов"
                aria-label="Новый отказ"
                maxLength={4000}
              />
              <Button type="submit" size="sm" variant="secondary" loading={antiSaving} disabled={!antiText.trim()}>
                Добавить
              </Button>
            </form>
          </Section>

          <Section title="Как читать прогресс" description="Одна шкала на цель, без декора.">
            <ul className="pc-legend">
              <li>Вехи закрыты — процент считается по ним.</li>
              <li>Вех нет — по связанным задачам.</li>
              <li>Ручной процент важнее автоматического.</li>
            </ul>
          </Section>
        </aside>
      </div>

      <GoalSheet
        open={sheetGoal !== null}
        goal={sheetGoalValue}
        goals={goals}
        spheres={spheres}
        defaultHorizon={horizon}
        onClose={() => setSheetGoal(null)}
        onSave={saveGoal}
        onDelete={sheetGoalValue ? removeGoal : undefined}
      />
      <SpheresSheet
        open={spheresOpen}
        onClose={() => setSpheresOpen(false)}
        onChanged={compass.reload}
      />
    </div>
  );
}
