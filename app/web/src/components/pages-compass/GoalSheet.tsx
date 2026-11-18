import { useEffect, useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { Goal, GoalInput, Horizon, Sphere } from '../../api/compass';
import { Button, DateInput, Field, Input, Select, Sheet, Textarea } from '../ui';

export interface GoalSheetProps {
  open: boolean;
  goal: Goal | null;
  goals: Goal[];
  spheres: Sphere[];
  defaultHorizon: Horizon;
  onClose: () => void;
  onSave: (input: GoalInput) => Promise<Goal | null>;
  onDelete?: () => Promise<boolean>;
}

const HORIZONS: Array<{ value: Horizon; label: string }> = [
  { value: 'year', label: 'Год' },
  { value: 'quarter', label: 'Квартал' },
  { value: 'month', label: 'Месяц' },
];

const STATUSES = [
  { value: 'active', label: 'В работе' },
  { value: 'done', label: 'Достигнута' },
  { value: 'dropped', label: 'Отложена' },
];

function descendantIds(goals: Goal[], rootId: number | null): Set<number> {
  const result = new Set<number>();
  if (rootId === null) return result;
  const queue = [rootId];
  while (queue.length > 0) {
    const current = queue.shift();
    if (current === undefined) continue;
    for (const goal of goals) {
      if (goal.parentId === current && !result.has(goal.id)) {
        result.add(goal.id);
        queue.push(goal.id);
      }
    }
  }
  return result;
}

export function GoalSheet({
  open,
  goal,
  goals,
  spheres,
  defaultHorizon,
  onClose,
  onSave,
  onDelete,
}: GoalSheetProps) {
  const ids = useId();
  const [title, setTitle] = useState('');
  const [horizon, setHorizon] = useState<Horizon>(defaultHorizon);
  const [sphereId, setSphereId] = useState('');
  const [parentId, setParentId] = useState('');
  const [deadline, setDeadline] = useState('');
  const [why, setWhy] = useState('');
  const [progress, setProgress] = useState('0');
  const [status, setStatus] = useState('active');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!open) return;
    setTitle(goal?.title ?? '');
    setHorizon(goal?.horizon ?? defaultHorizon);
    setSphereId(goal?.sphereId ? String(goal.sphereId) : '');
    setParentId(goal?.parentId ? String(goal.parentId) : '');
    setDeadline(goal?.deadline ?? '');
    setWhy(goal?.why ?? '');
    setProgress(String(goal?.progress ?? 0));
    setStatus(goal?.status ?? 'active');
    setError(null);
    setSaving(false);
    setConfirmDelete(false);
    // Форма заполняется при открытии, а не на каждом рендере списка.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, goal?.id]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = title.trim();
    if (!text) {
      setError('Название цели не может быть пустым');
      return;
    }
    const numericProgress = Math.max(0, Math.min(100, Number(progress) || 0));
    const input: GoalInput = {
      title: text,
      horizon,
      sphereId: sphereId ? Number(sphereId) : null,
      parentId: parentId ? Number(parentId) : null,
      deadline: deadline || null,
      why,
      progress: numericProgress,
      status: status as GoalInput['status'],
    };
    setSaving(true);
    setError(null);
    const saved = await onSave(input);
    setSaving(false);
    if (!saved) {
      setError('Не удалось сохранить цель');
      return;
    }
    onClose();
  }

  const excluded = descendantIds(goals, goal?.id ?? null);
  if (goal) excluded.add(goal.id);
  const parentOptions = [
    { value: '', label: 'Без родительской цели' },
    ...goals
      .filter((item) => !excluded.has(item.id))
      .map((item) => ({
        value: String(item.id),
        label: `${item.horizon === 'year' ? 'Год' : item.horizon === 'quarter' ? 'Квартал' : 'Месяц'} · ${item.title}`,
      })),
  ];
  const sphereOptions = [
    { value: '', label: 'Без сферы' },
    ...spheres.map((sphere) => ({ value: String(sphere.id), label: sphere.name })),
  ];

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={goal ? 'Цель' : 'Новая цель'}
      description="Год → квартал → месяц. Вехи и связанные задачи дают прогресс."
      footer={
        <>
          {goal && onDelete ? (
            <Button
              variant="danger"
              size="sm"
              onClick={async () => {
                if (!confirmDelete) {
                  setConfirmDelete(true);
                  return;
                }
                const ok = await onDelete();
                if (ok) onClose();
              }}
              onBlur={() => setConfirmDelete(false)}
            >
              {confirmDelete ? 'Точно удалить' : 'Удалить'}
            </Button>
          ) : null}
          <span className="pc-sheet__spacer" />
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button
            variant="primary"
            size="sm"
            type="submit"
            form={`${ids}-goal-form`}
            loading={saving}
          >
            Сохранить
          </Button>
        </>
      }
    >
      <form id={`${ids}-goal-form`} className="pc-form" onSubmit={submit}>
        <Field label="Цель" htmlFor={`${ids}-title`} required error={error ?? undefined}>
          <Input
            id={`${ids}-title`}
            value={title}
            onChange={(event) => {
              setTitle(event.target.value);
              setError(null);
            }}
            maxLength={300}
            autoComplete="off"
          />
        </Field>

        <div className="pc-form__grid">
          <Field label="Горизонт" htmlFor={`${ids}-horizon`}>
            <Select
              id={`${ids}-horizon`}
              value={horizon}
              onChange={(event) => setHorizon(event.target.value as Horizon)}
              options={HORIZONS}
            />
          </Field>
          <Field label="Сфера" htmlFor={`${ids}-sphere`}>
            <Select
              id={`${ids}-sphere`}
              value={sphereId}
              onChange={(event) => setSphereId(event.target.value)}
              options={sphereOptions}
            />
          </Field>
        </div>

        <div className="pc-form__grid">
          <Field label="Родительская цель" htmlFor={`${ids}-parent`} hint="Месячная — под квартальной">
            <Select
              id={`${ids}-parent`}
              value={parentId}
              onChange={(event) => setParentId(event.target.value)}
              options={parentOptions}
            />
          </Field>
          <Field label="Дедлайн" htmlFor={`${ids}-deadline`}>
            <DateInput
              id={`${ids}-deadline`}
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
            />
          </Field>
        </div>

        <Field
          label="Почему это важно"
          htmlFor={`${ids}-why`}
          hint="Одна-две фразы: зачем цель, а не что сделать"
        >
          <Textarea
            id={`${ids}-why`}
            rows={3}
            value={why}
            onChange={(event) => setWhy(event.target.value)}
            maxLength={4000}
          />
        </Field>

        <div className="pc-form__grid">
          <Field
            label="Ручной прогресс, %"
            htmlFor={`${ids}-progress`}
            hint="0 — считать по вехам или задачам"
          >
            <Input
              id={`${ids}-progress`}
              type="number"
              min={0}
              max={100}
              value={progress}
              onChange={(event) => setProgress(event.target.value)}
            />
          </Field>
          {goal ? (
            <Field label="Статус" htmlFor={`${ids}-status`}>
              <Select
                id={`${ids}-status`}
                value={status}
                onChange={(event) => setStatus(event.target.value)}
                options={STATUSES}
              />
            </Field>
          ) : null}
        </div>
      </form>
    </Sheet>
  );
}
