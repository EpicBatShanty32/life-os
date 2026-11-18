import { useEffect, useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { Goal, Sphere } from '../../api/compass';
import {
  deleteProject,
  useProjectDetail,
} from '../../api/tasks';
import type { Project, ProjectNote, ProjectStatus, Task } from '../../api/tasks';
import { apiErrorMessage, apiStatus } from '../../api/errors';
import { toast } from '../../lib/toast';
import {
  Button,
  Checkbox,
  DateInput,
  Field,
  Icon,
  Input,
  ProgressBar,
  Select,
  Sheet,
  Skeleton,
  Textarea,
} from '../ui';
import {
  HORIZON_LABEL,
  PROJECT_STATUS_LABEL,
  InlineError,
  SphereDot,
  isOverdue,
  shortDate,
  todayIso,
} from './shared';

const STATUS_OPTIONS = (['idea', 'active', 'paused', 'done'] as const).map((value) => ({
  value,
  label: PROJECT_STATUS_LABEL[value],
}));

export interface ProjectSheetProps {
  projectId: number | null;
  spheres: Sphere[];
  goals: Goal[];
  onClose: () => void;
  onChanged: () => void;
}

function goalLabel(goal: Goal): string {
  return `${HORIZON_LABEL[goal.horizon]} · ${goal.title}`;
}

function safeUrl(value: string): string | null {
  return /^https?:\/\//i.test(value.trim()) ? value.trim() : null;
}

function NoteRow({
  note,
  onDelete,
}: {
  note: ProjectNote;
  onDelete: () => void;
}) {
  const url = note.kind === 'link' ? safeUrl(note.url) : null;
  return (
    <li className="pc-note">
      {note.kind === 'link' ? (
        url ? (
          <a
            className="pc-note__link"
            href={url}
            target="_blank"
            rel="noreferrer noopener"
          >
            <Icon name="arrow-up-right" size={15} />
            <span>{note.title || note.url}</span>
          </a>
        ) : (
          <span className="pc-note__link">
            <Icon name="folder" size={15} />
            <span>{note.title || note.url || 'Ссылка без адреса'}</span>
          </span>
        )
      ) : (
        <div className="pc-note__content">
          {note.title ? <p className="pc-note__title">{note.title}</p> : null}
          <p className="pc-note__body">{note.body}</p>
        </div>
      )}
      <button
        type="button"
        className="pc-row-action"
        aria-label={`Удалить ${note.kind === 'link' ? 'ссылку' : 'заметку'}`}
        title="Удалить"
        onClick={onDelete}
      >
        <Icon name="trash" size={15} />
      </button>
    </li>
  );
}

function ProjectEditForm({
  project,
  spheres,
  goals,
  saving,
  onSave,
  onCancel,
}: {
  project: Project;
  spheres: Sphere[];
  goals: Goal[];
  saving: boolean;
  onSave: (patch: {
    name: string;
    status: ProjectStatus;
    sphereId: number | null;
    goalId: number | null;
    deadline: string | null;
    outcome: string;
    description: string;
  }) => void;
  onCancel: () => void;
}) {
  const ids = useId();
  const [name, setName] = useState(project.name);
  const [status, setStatus] = useState<ProjectStatus>(project.status);
  const [sphereId, setSphereId] = useState(project.sphereId ? String(project.sphereId) : '');
  const [goalId, setGoalId] = useState(project.goalId ? String(project.goalId) : '');
  const [deadline, setDeadline] = useState(project.deadline ?? '');
  const [outcome, setOutcome] = useState(project.outcome);
  const [description, setDescription] = useState(project.description);
  const [error, setError] = useState<string | null>(null);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim()) {
      setError('Название проекта не может быть пустым');
      return;
    }
    setError(null);
    onSave({
      name: name.trim(),
      status,
      sphereId: sphereId ? Number(sphereId) : null,
      goalId: goalId ? Number(goalId) : null,
      deadline: deadline || null,
      outcome,
      description,
    });
  }

  return (
    <form className="pc-form" onSubmit={submit}>
      <Field label="Название" htmlFor={`${ids}-name`} required error={error ?? undefined}>
        <Input
          id={`${ids}-name`}
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={200}
        />
      </Field>
      <div className="pc-form__grid">
        <Field label="Статус" htmlFor={`${ids}-status`}>
          <Select
            id={`${ids}-status`}
            value={status}
            onChange={(event) => setStatus(event.target.value as ProjectStatus)}
            options={STATUS_OPTIONS}
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
      <div className="pc-form__grid">
        <Field label="Сфера" htmlFor={`${ids}-sphere`}>
          <Select
            id={`${ids}-sphere`}
            value={sphereId}
            onChange={(event) => setSphereId(event.target.value)}
            options={[
              { value: '', label: 'Без сферы' },
              ...spheres.map((sphere) => ({ value: String(sphere.id), label: sphere.name })),
            ]}
          />
        </Field>
        <Field label="Цель" htmlFor={`${ids}-goal`}>
          <Select
            id={`${ids}-goal`}
            value={goalId}
            onChange={(event) => setGoalId(event.target.value)}
            options={[
              { value: '', label: 'Без цели' },
              ...goals.map((goal) => ({ value: String(goal.id), label: goalLabel(goal) })),
            ]}
          />
        </Field>
      </div>
      <Field label="Результат" htmlFor={`${ids}-outcome`} hint="Один абзац: что будет, когда проект завершён">
        <Textarea
          id={`${ids}-outcome`}
          rows={3}
          value={outcome}
          onChange={(event) => setOutcome(event.target.value)}
          maxLength={2000}
        />
      </Field>
      <Field label="Заметки о проекте" htmlFor={`${ids}-description`}>
        <Textarea
          id={`${ids}-description`}
          rows={3}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          maxLength={4000}
        />
      </Field>
      <div className="pc-form__actions">
        <Button type="submit" size="sm" variant="primary" loading={saving}>
          Сохранить
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Отмена
        </Button>
      </div>
    </form>
  );
}

function ProjectDetail({
  projectId,
  spheres,
  goals,
  onClose,
  onChanged,
}: {
  projectId: number;
  spheres: Sphere[];
  goals: Goal[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const detail = useProjectDetail(projectId);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [stepTitle, setStepTitle] = useState('');
  const [noteText, setNoteText] = useState('');
  const [linkTitle, setLinkTitle] = useState('');
  const [linkUrl, setLinkUrl] = useState('');
  const [taskTitle, setTaskTitle] = useState('');
  const [adding, setAdding] = useState(false);

  const project = detail.detail?.project;

  useEffect(() => {
    setEditing(false);
    setConfirmDelete(false);
    setStepTitle('');
    setNoteText('');
    setLinkTitle('');
    setLinkUrl('');
    setTaskTitle('');
  }, [projectId]);

  async function saveProject(patch: Parameters<typeof detail.updateProject>[0]) {
    setSaving(true);
    const saved = await detail.updateProject(patch);
    setSaving(false);
    if (saved) {
      setEditing(false);
      onChanged();
    }
  }

  async function removeProject() {
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    try {
      await deleteProject(projectId);
      toast('Проект удалён', 'default');
      onChanged();
      onClose();
    } catch (problem: unknown) {
      if (apiStatus(problem) === 409) {
        toast('В проекте есть задачи — сначала закройте или перенесите их', 'error');
      } else {
        toast(apiErrorMessage(problem, 'Не удалось удалить проект'), 'error');
      }
      setConfirmDelete(false);
    }
  }

  async function addStep(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const title = stepTitle.trim();
    if (!title || adding) return;
    setAdding(true);
    const ok = await detail.addStep(title);
    setAdding(false);
    if (ok) {
      setStepTitle('');
      onChanged();
    }
  }

  async function addNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const body = noteText.trim();
    if (!body || adding) return;
    setAdding(true);
    const ok = await detail.addNote({ kind: 'note', body });
    setAdding(false);
    if (ok) {
      setNoteText('');
      onChanged();
    }
  }

  async function addLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = linkUrl.trim();
    if (!url || adding) return;
    setAdding(true);
    const ok = await detail.addNote({
      kind: 'link',
      title: linkTitle.trim(),
      url,
    });
    setAdding(false);
    if (ok) {
      setLinkTitle('');
      setLinkUrl('');
      onChanged();
    }
  }

  async function addTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const title = taskTitle.trim();
    if (!title || adding) return;
    setAdding(true);
    const saved = await detail.addTask({ title, dueDate: todayIso() });
    setAdding(false);
    if (saved) {
      setTaskTitle('');
      onChanged();
    }
  }

  const steps = detail.detail?.steps ?? [];
  const stepsDone = steps.filter((step) => step.done).length;
  const notes = detail.detail?.notes.filter((note) => note.kind === 'note') ?? [];
  const links = detail.detail?.notes.filter((note) => note.kind === 'link') ?? [];
  const tasks = detail.detail?.tasks.items ?? [];
  const goalTitle = goals.find((goal) => goal.id === project?.goalId)?.title;

  return (
    <Sheet
      open
      onClose={onClose}
      size="wide"
      title={project?.name ?? 'Проект'}
      description={
        project
          ? `${PROJECT_STATUS_LABEL[project.status]}${project.sphereName ? ` · ${project.sphereName}` : ''}${project.deadline ? ` · до ${shortDate(project.deadline)}` : ''}`
          : 'Загрузка проекта'
      }
      footer={
        <>
          {project ? (
            <Button
              variant={confirmDelete ? 'danger' : 'ghost'}
              size="sm"
              onClick={() => {
                void removeProject();
              }}
              onBlur={() => setConfirmDelete(false)}
            >
              {confirmDelete ? 'Точно удалить' : 'Удалить проект'}
            </Button>
          ) : null}
          <span className="pc-sheet__spacer" />
          <Button variant="ghost" size="sm" onClick={onClose}>
            Закрыть
          </Button>
          {project && !editing ? (
            <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
              Редактировать
            </Button>
          ) : null}
        </>
      }
    >
      {detail.loading && !detail.detail ? (
        <div className="pc-form">
          <Skeleton variant="title" label="Загрузка проекта" />
          <Skeleton variant="text" lines={3} label="Загрузка проекта" />
        </div>
      ) : detail.error ? (
        <InlineError text={detail.error} onRetry={detail.reload} />
      ) : project ? (
        <div className="pc-project">
          {editing ? (
            <ProjectEditForm
              project={project}
              spheres={spheres}
              goals={goals}
              saving={saving}
              onSave={(patch) => {
                void saveProject(patch);
              }}
              onCancel={() => setEditing(false)}
            />
          ) : (
            <>
              <div className="pc-project__meta">
                {project.sphereName ? (
                  <span className="pc-project__meta-item">
                    <SphereDot color={null} />
                    {project.sphereName}
                  </span>
                ) : null}
                {goalTitle ? (
                  <span className="pc-project__meta-item">
                    <Icon name="target" size={15} />
                    {goalTitle}
                  </span>
                ) : null}
                {project.deadline ? (
                  <span
                    className={
                      isOverdue(project.deadline)
                        ? 'pc-project__meta-item is-overdue'
                        : 'pc-project__meta-item'
                    }
                  >
                    <Icon name="calendar" size={15} />
                    до {shortDate(project.deadline)}
                  </span>
                ) : null}
              </div>
              {project.outcome ? (
                <section className="pc-project__section" aria-label="Результат проекта">
                  <h3 className="pc-project__subtitle">Результат</h3>
                  <p className="pc-project__text">{project.outcome}</p>
                </section>
              ) : (
                <p className="pc-muted">Результат не сформулирован — добавьте один абзац.</p>
              )}
              {project.description ? (
                <section className="pc-project__section" aria-label="Описание проекта">
                  <h3 className="pc-project__subtitle">Заметки</h3>
                  <p className="pc-project__text">{project.description}</p>
                </section>
              ) : null}
            </>
          )}

          <section className="pc-project__section" aria-label="Этапы проекта">
            <div className="pc-project__section-head">
              <h3 className="pc-project__subtitle">
                Этапы · {stepsDone}/{steps.length}
              </h3>
            </div>
            <ProgressBar
              value={stepsDone}
              max={Math.max(1, steps.length)}
              tone="cat-2"
              showValue
              ariaLabel="Прогресс этапов проекта"
            />
            {steps.length === 0 ? (
              <p className="pc-muted">Этапов пока нет. Разбейте результат на 3–5 шагов.</p>
            ) : (
              <ol className="pc-steps">
                {steps.map((step) => (
                  <li className="pc-step" key={step.id}>
                    <Checkbox
                      size="sm"
                      checked={step.done}
                      onChange={() => {
                        void detail.toggleStep(step).then((ok) => {
                          if (ok) onChanged();
                        });
                      }}
                      label={<span className="pc-step__title">{step.title}</span>}
                    />
                    <button
                      type="button"
                      className="pc-row-action"
                      aria-label={`Удалить этап ${step.title}`}
                      title="Удалить этап"
                      onClick={() => {
                        void detail.removeStep(step).then(() => onChanged());
                      }}
                    >
                      <Icon name="trash" size={15} />
                    </button>
                  </li>
                ))}
              </ol>
            )}
            <form className="pc-inline-add" onSubmit={addStep}>
              <Input
                size="sm"
                value={stepTitle}
                onChange={(event) => setStepTitle(event.target.value)}
                placeholder="Новый этап"
                aria-label="Новый этап проекта"
                maxLength={300}
              />
              <Button type="submit" size="sm" variant="secondary" loading={adding} disabled={!stepTitle.trim()}>
                Добавить
              </Button>
            </form>
          </section>

          <section className="pc-project__section" aria-label="Заметки проекта">
            <h3 className="pc-project__subtitle">Заметки</h3>
            {notes.length === 0 ? (
              <p className="pc-muted">Пока нет заметок.</p>
            ) : (
              <ul className="pc-notes">
                {notes.map((note) => (
                  <NoteRow
                    key={note.id}
                    note={note}
                    onDelete={() => {
                      void detail.removeNote(note).then(() => onChanged());
                    }}
                  />
                ))}
              </ul>
            )}
            <form className="pc-note-add" onSubmit={addNote}>
              <Textarea
                rows={2}
                value={noteText}
                onChange={(event) => setNoteText(event.target.value)}
                placeholder="Мысль, решение, следующий шаг"
                aria-label="Текст новой заметки"
                maxLength={8000}
              />
              <Button type="submit" size="sm" variant="secondary" loading={adding} disabled={!noteText.trim()}>
                Добавить заметку
              </Button>
            </form>
          </section>

          <section className="pc-project__section" aria-label="Ссылки проекта">
            <h3 className="pc-project__subtitle">Ссылки</h3>
            {links.length === 0 ? (
              <p className="pc-muted">Ссылок пока нет.</p>
            ) : (
              <ul className="pc-notes">
                {links.map((note) => (
                  <NoteRow
                    key={note.id}
                    note={note}
                    onDelete={() => {
                      void detail.removeNote(note).then(() => onChanged());
                    }}
                  />
                ))}
              </ul>
            )}
            <form className="pc-link-add" onSubmit={addLink}>
              <Input
                size="sm"
                value={linkTitle}
                onChange={(event) => setLinkTitle(event.target.value)}
                placeholder="Подпись"
                aria-label="Подпись ссылки"
                maxLength={300}
              />
              <Input
                size="sm"
                type="url"
                value={linkUrl}
                onChange={(event) => setLinkUrl(event.target.value)}
                placeholder="https://…"
                aria-label="URL ссылки"
                maxLength={2000}
              />
              <Button type="submit" size="sm" variant="secondary" loading={adding} disabled={!linkUrl.trim()}>
                Добавить
              </Button>
            </form>
          </section>

          <section className="pc-project__section" aria-label="Задачи проекта">
            <div className="pc-project__section-head">
              <h3 className="pc-project__subtitle">
                Задачи · {detail.detail?.tasks.counts.done ?? 0}/{tasks.length}
              </h3>
            </div>
            {tasks.length === 0 ? (
              <p className="pc-muted">Связанных задач нет.</p>
            ) : (
              <ul className="pc-tasks">
                {tasks.map((task: Task) => (
                  <li className="pc-task" key={task.id}>
                    <Checkbox
                      size="sm"
                      checked={task.status === 'done'}
                      onChange={() => {
                        void detail.toggleTask(task, task.status !== 'done').then(() => onChanged());
                      }}
                      label={<span className="visually-hidden">Отметить «{task.title}» выполненной</span>}
                    />
                    <span className="pc-task__main pc-task__main--static">
                      <span className={task.status === 'done' ? 'pc-task__title is-done' : 'pc-task__title'}>
                        {task.title}
                      </span>
                      <span className="pc-task__meta">
                        {task.startAt
                          ? `${shortDate(task.startAt)} · ${task.startAt.slice(11, 16)}`
                          : task.dueDate
                            ? `до ${shortDate(task.dueDate)}`
                            : 'без срока'}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <form className="pc-inline-add" onSubmit={addTask}>
              <Input
                size="sm"
                value={taskTitle}
                onChange={(event) => setTaskTitle(event.target.value)}
                placeholder="Задача в проект, на сегодня"
                aria-label="Новая задача проекта"
                maxLength={300}
              />
              <Button type="submit" size="sm" variant="secondary" loading={adding} disabled={!taskTitle.trim()}>
                Добавить
              </Button>
            </form>
          </section>
        </div>
      ) : null}
    </Sheet>
  );
}

export function ProjectSheet({ projectId, spheres, goals, onClose, onChanged }: ProjectSheetProps) {
  if (projectId === null) return null;
  return (
    <ProjectDetail
      projectId={projectId}
      spheres={spheres}
      goals={goals}
      onClose={onClose}
      onChanged={onChanged}
    />
  );
}

export interface ProjectCreateSheetProps {
  open: boolean;
  spheres: Sphere[];
  goals: Goal[];
  onClose: () => void;
  onCreate: (input: {
    name: string;
    sphereId: number | null;
    goalId: number | null;
    deadline: string | null;
    status: ProjectStatus;
    outcome: string;
    description: string;
  }) => Promise<Project | null>;
}

export function ProjectCreateSheet({
  open,
  spheres,
  goals,
  onClose,
  onCreate,
}: ProjectCreateSheetProps) {
  const ids = useId();
  const [name, setName] = useState('');
  const [status, setStatus] = useState<ProjectStatus>('idea');
  const [sphereId, setSphereId] = useState('');
  const [goalId, setGoalId] = useState('');
  const [deadline, setDeadline] = useState('');
  const [outcome, setOutcome] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName('');
    setStatus('idea');
    setSphereId('');
    setGoalId('');
    setDeadline('');
    setOutcome('');
    setDescription('');
    setError(null);
    setSaving(false);
  }, [open]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = name.trim();
    if (!value) {
      setError('Название проекта не может быть пустым');
      return;
    }
    setSaving(true);
    setError(null);
    const saved = await onCreate({
      name: value,
      sphereId: sphereId ? Number(sphereId) : null,
      goalId: goalId ? Number(goalId) : null,
      deadline: deadline || null,
      status,
      outcome,
      description,
    });
    setSaving(false);
    if (!saved) {
      setError('Не удалось создать проект');
      return;
    }
    onClose();
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Новый проект"
      description="Результат, срок и этапы — остальное добавите внутри."
      footer={
        <>
          <span className="pc-sheet__spacer" />
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button variant="primary" size="sm" type="submit" form={`${ids}-project-new`} loading={saving}>
            Создать
          </Button>
        </>
      }
    >
      <form id={`${ids}-project-new`} className="pc-form" onSubmit={submit}>
        <Field label="Название" htmlFor={`${ids}-name`} required error={error ?? undefined}>
          <Input
            id={`${ids}-name`}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setError(null);
            }}
            maxLength={200}
            autoComplete="off"
          />
        </Field>
        <div className="pc-form__grid">
          <Field label="Статус" htmlFor={`${ids}-status`}>
            <Select
              id={`${ids}-status`}
              value={status}
              onChange={(event) => setStatus(event.target.value as ProjectStatus)}
              options={STATUS_OPTIONS}
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
        <div className="pc-form__grid">
          <Field label="Сфера" htmlFor={`${ids}-sphere`}>
            <Select
              id={`${ids}-sphere`}
              value={sphereId}
              onChange={(event) => setSphereId(event.target.value)}
              options={[
                { value: '', label: 'Без сферы' },
                ...spheres.map((sphere) => ({ value: String(sphere.id), label: sphere.name })),
              ]}
            />
          </Field>
          <Field label="Цель" htmlFor={`${ids}-goal`}>
            <Select
              id={`${ids}-goal`}
              value={goalId}
              onChange={(event) => setGoalId(event.target.value)}
              options={[
                { value: '', label: 'Без цели' },
                ...goals.map((goal) => ({ value: String(goal.id), label: goalLabel(goal) })),
              ]}
            />
          </Field>
        </div>
        <Field label="Результат" htmlFor={`${ids}-outcome`} hint="Что будет, когда проект завершён">
          <Textarea
            id={`${ids}-outcome`}
            rows={3}
            value={outcome}
            onChange={(event) => setOutcome(event.target.value)}
            maxLength={2000}
          />
        </Field>
        <Field label="Заметки" htmlFor={`${ids}-description`}>
          <Textarea
            id={`${ids}-description`}
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={4000}
          />
        </Field>
      </form>
    </Sheet>
  );
}
