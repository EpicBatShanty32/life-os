import '../styles/pages-compass.css';
import { useEffect, useMemo, useState } from 'react';
import { useCompass } from '../api/compass';
import { pluralRu } from '../lib/format';
import { useProjects } from '../api/tasks';
import type { Project, ProjectStatus } from '../api/tasks';
import {
  ProjectCreateSheet,
  ProjectSheet,
} from '../components/pages-compass/ProjectSheet';
import { Button, EmptyState, Icon, Input, ProgressBar, Select } from '../components/ui';
import {
  InlineError,
  PROJECT_STATUS_LABEL,
  PageHead,
  SkeletonRows,
  SphereDot,
  isOverdue,
  longToday,
  shortDate,
} from '../components/pages-compass/shared';

const STATUS_OPTIONS = [
  { value: 'all', label: 'Все статусы' },
  ...(['idea', 'active', 'paused', 'done'] as const).map((value) => ({
    value,
    label: PROJECT_STATUS_LABEL[value],
  })),
];

export function Projects() {
  const compass = useCompass();
  const [statusFilter, setStatusFilter] = useState('all');
  const [sphereFilter, setSphereFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const projects = useProjects({
    status: statusFilter === 'all' ? undefined : (statusFilter as ProjectStatus),
    sphereId: sphereFilter === 'all' ? undefined : Number(sphereFilter),
    q: debounced || undefined,
  });

  const spheres = compass.data?.spheres ?? [];
  const goals = compass.data?.goals ?? [];
  const items = projects.data?.items ?? [];
  const goalTitle = useMemo(() => {
    const map = new Map<number, string>();
    for (const goal of goals) map.set(goal.id, goal.title);
    return map;
  }, [goals]);
  const sphereColor = useMemo(() => {
    const map = new Map<number, string>();
    for (const sphere of spheres) map.set(sphere.id, sphere.color);
    return map;
  }, [spheres]);

  async function createProject(input: {
    name: string;
    sphereId: number | null;
    goalId: number | null;
    deadline: string | null;
    status: ProjectStatus;
    outcome: string;
    description: string;
  }): Promise<Project | null> {
    const saved = await projects.create(input);
    if (saved) setSelectedId(saved.id);
    return saved;
  }

  return (
    <div className="pc-page">
      <PageHead
        title="Проекты"
        date={longToday()}
        note={
          projects.data
            ? `${projects.data.counts.total} ${pluralRu(projects.data.counts.total, ['проект', 'проекта', 'проектов'])} · ${projects.data.counts.active} в работе · ${projects.data.counts.paused} на паузе`
            : 'Результаты, этапы и связанные задачи'
        }
        actions={
          <Button
            variant="secondary"
            size="sm"
            leadingIcon="plus"
            onClick={() => setCreateOpen(true)}
          >
            Новый проект
          </Button>
        }
      />

      <div className="pc-filters">
        <div className="pc-search">
          <Icon name="search" size={16} className="pc-search__icon" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Найти проект…"
            aria-label="Поиск по проектам"
            autoComplete="off"
          />
        </div>
        <Select
          size="sm"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
          options={STATUS_OPTIONS}
          aria-label="Фильтр по статусу"
        />
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
        {statusFilter !== 'all' || sphereFilter !== 'all' || search ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setStatusFilter('all');
              setSphereFilter('all');
              setSearch('');
            }}
          >
            Сбросить
          </Button>
        ) : null}
        <span className="pc-filters__count">{items.length}</span>
      </div>

      {projects.loading && !projects.data ? (
        <SkeletonRows rows={4} label="Загрузка проектов" />
      ) : projects.error ? (
        <InlineError text={projects.error} onRetry={projects.reload} />
      ) : items.length === 0 ? (
        <EmptyState
          title={debounced || statusFilter !== 'all' || sphereFilter !== 'all' ? 'Ничего не нашлось' : 'Проектов пока нет'}
          description={
            debounced || statusFilter !== 'all' || sphereFilter !== 'all'
              ? 'Измените фильтры или создайте новый проект.'
              : 'Начните с одного результата и 3–5 этапов — остальное добавится в drawer.'
          }
          action={
            <Button variant="secondary" size="sm" onClick={() => setCreateOpen(true)}>
              Новый проект
            </Button>
          }
        />
      ) : (
        <ul className="pc-projects">
          {items.map((project) => (
            <li key={project.id}>
              <article className="pc-project-card">
                <header className="pc-project-card__head">
                  <h2 className="pc-project-card__title">
                    <button
                      type="button"
                      className="pc-project-card__open"
                      onClick={() => setSelectedId(project.id)}
                    >
                      {project.name}
                    </button>
                  </h2>
                  <span className="pc-project-card__status" data-status={project.status}>
                    {PROJECT_STATUS_LABEL[project.status]}
                  </span>
                </header>
                <p className="pc-project-card__outcome">
                  {project.outcome || 'Результат не сформулирован — добавьте один абзац.'}
                </p>
                <div className="pc-project-card__meta">
                  {project.sphereName ? (
                    <span className="pc-project-card__meta-item">
                      <SphereDot color={sphereColor.get(project.sphereId ?? -1) ?? null} />
                      {project.sphereName}
                    </span>
                  ) : null}
                  {project.goalId && goalTitle.get(project.goalId) ? (
                    <span className="pc-project-card__meta-item">
                      <Icon name="target" size={14} />
                      {goalTitle.get(project.goalId)}
                    </span>
                  ) : null}
                  {project.deadline ? (
                    <span
                      className={
                        isOverdue(project.deadline)
                          ? 'pc-project-card__meta-item is-overdue'
                          : 'pc-project-card__meta-item'
                      }
                    >
                      <Icon name="calendar" size={14} />
                      до {shortDate(project.deadline)}
                    </span>
                  ) : null}
                </div>
                <ProgressBar
                  value={project.stepsDone}
                  max={Math.max(1, project.stepsTotal)}
                  tone="cat-2"
                  showValue
                  ariaLabel={`Этапы проекта ${project.name}`}
                />
                <p className="pc-project-card__foot">
                  {project.stepsDone} из {project.stepsTotal}{' '}
                  {pluralRu(project.stepsTotal, ['этапа', 'этапов', 'этапов'])}
                  {' · '}
                  {project.tasksDone} из {project.tasksTotal}{' '}
                  {pluralRu(project.tasksTotal, ['задачи', 'задач', 'задач'])}
                </p>
              </article>
            </li>
          ))}
        </ul>
      )}

      <ProjectCreateSheet
        open={createOpen}
        spheres={spheres}
        goals={goals}
        onClose={() => setCreateOpen(false)}
        onCreate={createProject}
      />

      <ProjectSheet
        projectId={selectedId}
        spheres={spheres}
        goals={goals}
        onClose={() => setSelectedId(null)}
        onChanged={projects.reload}
      />
    </div>
  );
}
