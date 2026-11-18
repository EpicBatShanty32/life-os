import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { Sphere } from '../../api/compass';
import type { ListKind, Resource, Task, TaskInput } from '../../api/tasks';
import { Button, DateInput, Field, Input, Select, Sheet, Textarea } from '../ui';
import { shortDate, todayIso } from './shared';

export interface TaskEditorProps {
  open: boolean;
  task: Task | null;
  defaults?: Partial<TaskInput>;
  spheres: Sphere[];
  projects: Array<{ id: number; name: string }>;
  onClose: () => void;
  onSave: (input: TaskInput) => Promise<Task | null>;
  onDelete?: () => Promise<boolean>;
}

const RESOURCE_OPTIONS = [
  { value: 'must', label: 'Обязательно (must)' },
  { value: 'important', label: 'Важно, если будет время' },
  { value: 'low', label: 'На низком ресурсе' },
];

const LIST_OPTIONS: Array<{ value: ListKind; label: string }> = [
  { value: 'work', label: 'Работа' },
  { value: 'personal', label: 'Личное' },
];

export function TaskEditor({
  open,
  task,
  defaults,
  spheres,
  projects,
  onClose,
  onSave,
  onDelete,
}: TaskEditorProps) {
  const ids = useId();
  const formId = `${ids}-form`;
  const titleRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [resource, setResource] = useState<Resource>('important');
  const [listKind, setListKind] = useState<ListKind>('work');
  const [sphereId, setSphereId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [recurrence, setRecurrence] = useState('');
  const [tags, setTags] = useState('');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!open) return;
    const source = task ?? null;
    const fallbackDate = defaults?.dueDate ?? todayIso();
    setTitle(source?.title ?? defaults?.title ?? '');
    setDueDate(source?.dueDate?.slice(0, 10) ?? fallbackDate);
    setStart(
      source?.startAt ? source.startAt.slice(11, 16) : defaults?.startAt ? defaults.startAt.slice(11, 16) : '',
    );
    setEnd(source?.endAt ? source.endAt.slice(11, 16) : defaults?.endAt ? defaults.endAt.slice(11, 16) : '');
    setResource(source?.resource ?? (defaults?.resource as Resource | undefined) ?? 'important');
    setListKind(source?.listKind ?? (defaults?.listKind as ListKind | undefined) ?? 'work');
    setSphereId(source?.sphereId ? String(source.sphereId) : '');
    setProjectId(source?.projectId ? String(source.projectId) : '');
    setRecurrence(source?.recurrence ?? '');
    setTags(source?.tags.join(', ') ?? '');
    setNotes(source?.notes ?? '');
    setError(null);
    setSaving(false);
    setConfirmDelete(false);
    const frame = window.requestAnimationFrame(() => titleRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
    // defaults — намеренно только при открытии, иначе форма сбрасывалась бы на каждом рендере.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, task?.id]);

  const recurrenceOptions = useMemo(() => {
    const day = new Date(`${dueDate || todayIso()}T12:00:00`);
    const isoWeekday = day.getDay() || 7;
    const monthDay = day.getDate();
    const options = [
      { value: '', label: 'Не повторять' },
      { value: 'daily', label: 'Ежедневно' },
      { value: 'weekly:1,2,3,4,5', label: 'По будням' },
      { value: `weekly:${isoWeekday}`, label: 'Раз в неделю' },
      { value: `monthly:${monthDay}`, label: 'Раз в месяц' },
    ];
    if (recurrence && !options.some((option) => option.value === recurrence)) {
      options.push({ value: recurrence, label: `Своё правило (${recurrence})` });
    }
    return options;
  }, [dueDate, recurrence]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = title.trim();
    if (!text) {
      setError('Название не может быть пустым');
      titleRef.current?.focus();
      return;
    }
    const day = dueDate || todayIso();
    const input: TaskInput = {
      title: text,
      dueDate: day,
      startAt: start ? `${day}T${start}` : null,
      endAt: end ? `${day}T${end}` : null,
      resource,
      listKind,
      sphereId: sphereId ? Number(sphereId) : null,
      projectId: projectId ? Number(projectId) : null,
      recurrence,
      tags: tags
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean),
      notes,
    };
    setSaving(true);
    setError(null);
    const saved = await onSave(input);
    setSaving(false);
    if (!saved) {
      setError('Не удалось сохранить — проверьте поля');
      return;
    }
    onClose();
  }

  const sphereOptions = [
    { value: '', label: 'Без сферы' },
    ...spheres.map((sphere) => ({ value: String(sphere.id), label: sphere.name })),
  ];
  const projectOptions = [
    { value: '', label: 'Без проекта' },
    ...projects.map((project) => ({ value: String(project.id), label: project.name })),
  ];

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={task ? 'Задача' : defaults?.startAt ? 'Новое событие' : 'Новая задача'}
      description={
        task
          ? `Создана ${shortDate(task.created)}${task.doneAt ? ' · закрыта' : ''}`
          : defaults?.startAt
            ? 'Время уже подставлено — поправьте, если нужно'
            : 'Enter — сохранить, Esc — закрыть'
      }
      footer={
        <>
          {task && onDelete ? (
            <Button
              variant="danger"
              size="sm"
              className="pc-sheet__danger"
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
          <Button variant="primary" size="sm" type="submit" form={formId} loading={saving}>
            Сохранить
          </Button>
        </>
      }
    >
      <form id={formId} className="pc-form" onSubmit={submit}>
        <Field label="Название" htmlFor={`${ids}-title`} required error={error ?? undefined}>
          <input
            ref={titleRef}
            id={`${ids}-title`}
            className="ui-input"
            value={title}
            onChange={(event) => {
              setTitle(event.target.value);
              setError(null);
            }}
            maxLength={300}
            autoComplete="off"
          />
        </Field>

        <div className="pc-form__grid pc-form__grid--3">
          <Field label="Дата" htmlFor={`${ids}-date`}>
            <DateInput
              id={`${ids}-date`}
              value={dueDate}
              onChange={(event) => setDueDate(event.target.value)}
            />
          </Field>
          <Field label="Начало" htmlFor={`${ids}-start`}>
            <Input
              id={`${ids}-start`}
              type="time"
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </Field>
          <Field label="Конец" htmlFor={`${ids}-end`}>
            <Input
              id={`${ids}-end`}
              type="time"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </Field>
        </div>

        <div className="pc-form__grid">
          <Field label="Ресурс" htmlFor={`${ids}-resource`}>
            <Select
              id={`${ids}-resource`}
              value={resource}
              onChange={(event) => setResource(event.target.value as Resource)}
              options={RESOURCE_OPTIONS}
            />
          </Field>
          <Field label="Список" htmlFor={`${ids}-list`}>
            <Select
              id={`${ids}-list`}
              value={listKind}
              onChange={(event) => setListKind(event.target.value as ListKind)}
              options={LIST_OPTIONS}
            />
          </Field>
        </div>

        <div className="pc-form__grid">
          <Field label="Сфера" htmlFor={`${ids}-sphere`}>
            <Select
              id={`${ids}-sphere`}
              value={sphereId}
              onChange={(event) => setSphereId(event.target.value)}
              options={sphereOptions}
            />
          </Field>
          <Field label="Проект" htmlFor={`${ids}-project`}>
            <Select
              id={`${ids}-project`}
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              options={projectOptions}
            />
          </Field>
        </div>

        <div className="pc-form__grid">
          <Field label="Повтор" htmlFor={`${ids}-recurrence`}>
            <Select
              id={`${ids}-recurrence`}
              value={recurrence}
              onChange={(event) => setRecurrence(event.target.value)}
              options={recurrenceOptions}
            />
          </Field>
          <Field label="Теги" htmlFor={`${ids}-tags`} hint="Через запятую">
            <Input
              id={`${ids}-tags`}
              value={tags}
              onChange={(event) => setTags(event.target.value)}
              placeholder="дом, покупки"
              autoComplete="off"
            />
          </Field>
        </div>

        <Field label="Заметка" htmlFor={`${ids}-notes`}>
          <Textarea
            id={`${ids}-notes`}
            rows={3}
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="Контекст, ссылки, следующий шаг"
          />
        </Field>

        {start ? (
          <p className="pc-form__note">
            Время {start}
            {end ? `–${end}` : ''} делает задачу жёстким событием: она появится в
            таймлайне дня и в сетке недели.
          </p>
        ) : null}
      </form>
    </Sheet>
  );
}
