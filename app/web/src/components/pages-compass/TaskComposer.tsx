import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import type { Sphere } from '../../api/compass';
import type { ListKind, Resource, Task, TaskInput } from '../../api/tasks';
import { Icon } from '../Icon';
import { Button, Input, Select } from '../ui';
import { RESOURCE_LABEL, shortDate } from './shared';

export interface TaskComposerProps {
  date: string;
  onAdd: (input: TaskInput) => Promise<Task | null>;
  spheres?: Sphere[];
  projects?: Array<{ id: number; name: string }>;
  compact?: boolean;
  focusSignal?: number;
  placeholder?: string;
  defaultResource?: Resource;
  autoFocus?: boolean;
}

const RESOURCE_OPTIONS = (['must', 'important', 'low'] as const).map((value) => ({
  value,
  label: RESOURCE_LABEL[value],
}));

const LIST_OPTIONS: Array<{ value: ListKind; label: string }> = [
  { value: 'work', label: 'Работа' },
  { value: 'personal', label: 'Личное' },
];

export function TaskComposer({
  date,
  onAdd,
  spheres = [],
  projects = [],
  compact = false,
  focusSignal = 0,
  placeholder = 'Что сделать? Enter — добавить',
  defaultResource = 'important',
  autoFocus = false,
}: TaskComposerProps) {
  const ids = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState('');
  const [resource, setResource] = useState<Resource>(defaultResource);
  const [listKind, setListKind] = useState<ListKind>('work');
  const [sphereId, setSphereId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [recurrence, setRecurrence] = useState('');
  const [details, setDetails] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (focusSignal > 0) inputRef.current?.focus();
  }, [focusSignal]);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  const resetTimes = () => {
    setStart('');
    setEnd('');
  };

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = title.trim();
    if (!text || saving) return;
    setError(null);
    setSaving(true);
    const startAt = start ? `${date}T${start}` : null;
    const endAt = end ? `${date}T${end}` : null;
    const input: TaskInput = {
      title: text,
      dueDate: date,
      resource,
      listKind,
      sphereId: sphereId ? Number(sphereId) : null,
      projectId: projectId ? Number(projectId) : null,
      startAt,
      endAt,
      recurrence,
    };
    const saved = await onAdd(input);
    setSaving(false);
    if (!saved) {
      setError('Не удалось добавить задачу — текст сохранён');
      inputRef.current?.focus();
      return;
    }
    setTitle('');
    if (startAt || endAt) resetTimes();
    inputRef.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLFormElement>) {
    if (event.key === 'Escape' && details) {
      event.stopPropagation();
      setDetails(false);
    }
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
    <form
      className={compact ? 'pc-composer pc-composer--compact' : 'pc-composer'}
      onSubmit={submit}
      onKeyDown={onKeyDown}
      aria-label="Быстрое добавление задачи"
    >
      <div className="pc-composer__main">
        <Icon name="plus" size={16} className="pc-composer__plus" />
        <input
          ref={inputRef}
          className="ui-input pc-composer__input"
          value={title}
          onChange={(event) => {
            setTitle(event.target.value);
            setError(null);
          }}
          placeholder={placeholder}
          aria-label="Название задачи"
          autoComplete="off"
          maxLength={300}
        />
        <Select
          className="pc-composer__resource"
          value={resource}
          onChange={(event) => setResource(event.target.value as Resource)}
          options={RESOURCE_OPTIONS}
          aria-label="Ресурс"
          title="Ресурс: от «обязательно» до «рутины»"
        />
        <Button type="submit" variant="secondary" size="sm" disabled={!title.trim() || saving}>
          Добавить
        </Button>
        <button
          type="button"
          className="pc-composer__details"
          aria-expanded={details}
          aria-controls={`${ids}-details`}
          onClick={() => setDetails((open) => !open)}
          title={details ? 'Скрыть детали' : 'Дата, время, сфера, проект'}
        >
          <Icon name={details ? 'chevron-up' : 'chevron-down'} size={16} />
          <span className="visually-hidden">Детали задачи</span>
        </button>
      </div>

      <p className="pc-composer__hint">
        {shortDate(date)} · {RESOURCE_LABEL[resource]}
        {recurrence ? ' · повтор' : ''}
        {error ? <span className="pc-composer__error"> — {error}</span> : null}
      </p>

      {details ? (
        <div className="pc-composer__details-panel" id={`${ids}-details`}>
          <label className="pc-mini-field">
            <span className="pc-mini-field__label">Начало</span>
            <Input
              type="time"
              size="sm"
              value={start}
              onChange={(event) => setStart(event.target.value)}
              aria-label="Время начала"
            />
          </label>
          <label className="pc-mini-field">
            <span className="pc-mini-field__label">Конец</span>
            <Input
              type="time"
              size="sm"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
              aria-label="Время окончания"
            />
          </label>
          <label className="pc-mini-field">
            <span className="pc-mini-field__label">Сфера</span>
            <Select
              size="sm"
              value={sphereId}
              onChange={(event) => setSphereId(event.target.value)}
              options={sphereOptions}
              aria-label="Сфера"
            />
          </label>
          <label className="pc-mini-field">
            <span className="pc-mini-field__label">Проект</span>
            <Select
              size="sm"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              options={projectOptions}
              aria-label="Проект"
            />
          </label>
          <label className="pc-mini-field">
            <span className="pc-mini-field__label">Список</span>
            <Select
              size="sm"
              value={listKind}
              onChange={(event) => setListKind(event.target.value as ListKind)}
              options={LIST_OPTIONS}
              aria-label="Список"
            />
          </label>
          <label className="pc-mini-field">
            <span className="pc-mini-field__label">Повтор</span>
            <Select
              size="sm"
              value={recurrence}
              onChange={(event) => setRecurrence(event.target.value)}
              options={[
                { value: '', label: 'Нет' },
                { value: 'daily', label: 'Ежедневно' },
                { value: 'weekly:1,2,3,4,5', label: 'По будням' },
                { value: `weekly:${new Date(`${date}T12:00:00`).getDay() || 7}`, label: 'Еженедельно' },
                { value: `monthly:${Number(date.slice(8, 10))}`, label: 'Ежемесячно' },
              ]}
              aria-label="Повтор задачи"
            />
          </label>
          <p className="pc-composer__note">
            Время превращает задачу в жёсткое событие — оно появится в таймлайне и сетке недели.
          </p>
        </div>
      ) : null}
    </form>
  );
}
