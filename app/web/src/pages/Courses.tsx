import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import { navigate } from '../lib/router';

interface Course {
  id: number;
  title: string;
  category: string;
  cover: string;
  lessons_total: number;
  lessons_done: number;
  progress: number;
}

interface Lesson {
  id: number;
  course_id: number;
  idx: number;
  title: string;
  media_path: string;
  duration_s: number;
  status: string;
}

interface Segment {
  id: number;
  lesson_id: number;
  start_s: number;
  end_s: number;
  text: string;
}

interface ProgressInfo {
  lesson_id: number;
  position_s: number;
  watched_s: number;
  percent: number | null;
}

interface LessonDetail {
  lesson: Lesson;
  progress: ProgressInfo | null;
  transcript: string | null;
  segments: Segment[];
  prev_lesson_id: number | null;
  next_lesson_id: number | null;
}

const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '');

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, '0')}`;
}

function isVideo(path: string): boolean {
  return /\.(mp4|webm|mov|m4v)$/i.test(path);
}

export function Courses() {
  const [courses, setCourses] = useState<Course[]>([]);
  const [course, setCourse] = useState<{ course: Course; lessons: Lesson[] } | null>(null);
  const [detail, setDetail] = useState<LessonDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** Фильтр по субтитрам текущего урока: сбрасывается при открытии другого урока. */
  const [segmentQuery, setSegmentQuery] = useState('');
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const saveTimer = useRef<number | null>(null);

  const loadCourses = useCallback(() => {
    setLoading(true);
    getJson<Course[]>('/courses')
      .then((list) => {
        setCourses(list);
        setError(null);
      })
      .catch((problem: unknown) => setError(problem instanceof Error ? problem.message : 'нет ответа'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(loadCourses, [loadCourses]);

  const openCourse = useCallback((courseId: number) => {
    // API отдаёт курс вместе с уроками (CourseDetail), а не вложенный объект.
    getJson<Course & { lessons: Lesson[] }>(`/courses/${courseId}`)
      .then((payload) => setCourse({ course: payload, lessons: payload.lessons ?? [] }))
      .catch(() => setError('Курс не загрузился'));
  }, []);

  const openLesson = useCallback((lessonId: number) => {
    setSegmentQuery('');
    getJson<LessonDetail>(`/lessons/${lessonId}`)
      .then((payload) => {
        setDetail(payload);
        window.setTimeout(() => {
          const player = mediaRef.current;
          if (player && payload.progress && payload.progress.position_s > 0) {
            player.currentTime = payload.progress.position_s;
          }
        }, 120);
      })
      .catch(() => setError('Урок не загрузился'));
  }, []);

  function saveProgress(position: number, watched?: number) {
    if (!detail) return;
    const body = JSON.stringify({ position_s: Math.round(position), watched_s: watched === undefined ? undefined : Math.round(watched) });
    void fetch(`${API_BASE}/lessons/${detail.lesson.id}/progress`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body,
    }).catch(() => undefined);
  }

  function onTimeUpdate(event: React.SyntheticEvent<HTMLMediaElement>) {
    const player = event.currentTarget;
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => saveProgress(player.currentTime, player.currentTime), 4000);
  }

  function jump(seconds: number) {
    const player = mediaRef.current;
    if (!player) return;
    player.currentTime = seconds;
    void player.play().catch(() => undefined);
  }

  if (loading) {
    return (
      <div className="page">
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>Загружаю курсы…</span>
        </div>
      </div>
    );
  }

  if (detail) {
    const mediaPath = `${API_BASE.replace(/\/api$/, '')}/media/${detail.lesson.id}`;
    // Поиск по субтитрам: текст сегмента в нижнем регистре + таймкод вида 1:05.
    const normalizedQuery = segmentQuery.trim().toLowerCase();
    const visibleSegments =
      normalizedQuery === ''
        ? detail.segments
        : detail.segments.filter(
            (segment) =>
              segment.text.toLowerCase().includes(normalizedQuery) ||
              clock(segment.start_s).includes(normalizedQuery),
          );
    return (
      <div className="page">
        <header className="page-head">
          <div>
            <p className="page-head__kicker">Life OS · v3</p>
            <h1 className="page-head__title">{detail.lesson.title}</h1>
            <p className="page-head__sub">
              Урок {detail.lesson.idx}
              {detail.progress && detail.progress.percent !== null
                ? ` · просмотрено ${Math.round((detail.progress.percent ?? 0) * 100)}%`
                : ''}
            </p>
          </div>
          <div className="page-head__actions">
            <button
              className="btn btn--ghost"
              type="button"
              onClick={() => {
                setSegmentQuery('');
                setDetail(null);
              }}
            >
              <Icon name="chevron-left" size={16} />
              <span>К списку уроков</span>
            </button>
          </div>
        </header>

        <section className="card" aria-label="Плеер">
          {detail.lesson.media_path ? (
            isVideo(detail.lesson.media_path) ? (
              <video
                className="player"
                ref={(node) => {
                  mediaRef.current = node;
                }}
                src={mediaPath}
                controls
                preload="metadata"
                onTimeUpdate={onTimeUpdate}
                onPause={(event) => saveProgress(event.currentTarget.currentTime, event.currentTarget.currentTime)}
              />
            ) : (
              <audio
                className="player"
                ref={(node) => {
                  mediaRef.current = node;
                }}
                src={mediaPath}
                controls
                preload="metadata"
                onTimeUpdate={onTimeUpdate}
                onPause={(event) => saveProgress(event.currentTarget.currentTime, event.currentTarget.currentTime)}
              />
            )
          ) : (
            <p className="card__sub">У урока нет медиафайла — доступен конспект и таймкоды.</p>
          )}

          {detail.segments.length > 0 ? (
            <div className="segment-search">
              <div className="page-head">
                <div>
                  <h2 className="card__title">
                    <label htmlFor="lesson-segment-search">Поиск по субтитрам</label>
                  </h2>
                  <p className="card__sub" id="lesson-segment-count" role="status">
                    Найдено {visibleSegments.length} из {detail.segments.length}
                  </p>
                </div>
                <div className="page-head__actions">
                  <input
                    id="lesson-segment-search"
                    className="input search"
                    type="text"
                    value={segmentQuery}
                    onChange={(event) => setSegmentQuery(event.target.value)}
                    placeholder="Например: дыхание"
                    aria-describedby="lesson-segment-count"
                    autoComplete="off"
                  />
                  {segmentQuery !== '' ? (
                    <button
                      className="btn btn--ghost"
                      type="button"
                      onClick={() => setSegmentQuery('')}
                    >
                      <Icon name="close" size={16} />
                      <span>Сбросить</span>
                    </button>
                  ) : null}
                </div>
              </div>

              {visibleSegments.length > 0 ? (
                <ol className="chips" aria-label="Таймкоды">
                  {visibleSegments.map((segment) => (
                    <li key={segment.id}>
                      <button className="chip" type="button" onClick={() => jump(segment.start_s)}>
                        <span className="chip__time">{clock(segment.start_s)}</span>
                        <span className="chip__text">{segment.text}</span>
                      </button>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="empty-note">
                  Ничего не найдено по запросу «{segmentQuery.trim()}». Попробуйте другое слово или
                  таймкод вида 1:05.
                </p>
              )}
            </div>
          ) : null}

          <div className="player-nav">
            <button
              className="btn btn--ghost"
              type="button"
              disabled={detail.prev_lesson_id === null}
              onClick={() => detail.prev_lesson_id !== null && openLesson(detail.prev_lesson_id)}
            >
              <Icon name="chevron-left" size={16} />
              <span>Предыдущий</span>
            </button>
            <button
              className="btn"
              type="button"
              disabled={detail.next_lesson_id === null}
              onClick={() => detail.next_lesson_id !== null && openLesson(detail.next_lesson_id)}
            >
              <span>Следующий</span>
              <Icon name="chevron-right" size={16} />
            </button>
          </div>
        </section>

        {detail.transcript ? (
          <section className="card" aria-label="Конспект">
            <header className="card__head">
              <h2 className="card__title">Конспект</h2>
            </header>
            <p className="transcript">{detail.transcript}</p>
          </section>
        ) : null}
      </div>
    );
  }

  if (course) {
    return (
      <div className="page">
        <header className="page-head">
          <div>
            <p className="page-head__kicker">Life OS · v3</p>
            <h1 className="page-head__title">{course.course.title}</h1>
            <p className="page-head__sub">
              {course.course.lessons_done} из {course.course.lessons_total} уроков пройдено
            </p>
          </div>
          <div className="page-head__actions">
            <button className="btn btn--ghost" type="button" onClick={() => setCourse(null)}>
              <Icon name="chevron-left" size={16} />
              <span>Все курсы</span>
            </button>
          </div>
        </header>

        <section className="card" aria-label="Уроки">
          <ol className="lesson-list">
            {course.lessons.map((lesson) => (
              <li key={lesson.id}>
                <button className="lesson" type="button" onClick={() => openLesson(lesson.id)}>
                  <span className="lesson__idx">{lesson.idx}</span>
                  <span className="lesson__title">{lesson.title}</span>
                  <span className="lesson__meta">
                    {lesson.duration_s > 0 ? clock(lesson.duration_s) : '—'}
                    {lesson.status === 'done' ? ' · пройден' : ''}
                  </span>
                  <Icon name="play" size={16} />
                </button>
              </li>
            ))}
          </ol>
        </section>
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="page-head__kicker">Life OS · v3</p>
          <h1 className="page-head__title">Курсы</h1>
          <p className="page-head__sub">Видео и аудио с таймкодами, конспектами и прогрессом.</p>
        </div>
        <span className="status-pill">
          <span className="status-pill__dot" aria-hidden="true" />
          {courses.length} курсов
        </span>
      </header>

      {error ? (
        <div className="card empty-note" role="status">
          <Icon name="sync" size={18} />
          <span>API не ответил ({error}).</span>
          <button className="btn btn--ghost" type="button" onClick={loadCourses}>
            Повторить
          </button>
        </div>
      ) : null}

      {courses.length === 0 && !error ? (
        <div className="card empty-note" role="status">
          <Icon name="book" size={20} />
          <span>Курсов пока нет. Добавьте урок в Obsidian или через бота — он появится здесь.</span>
        </div>
      ) : null}

      <section className="bento" aria-label="Список курсов">
        {courses.map((item) => (
          <button className="card course-card" type="button" key={item.id} onClick={() => openCourse(item.id)}>
            <span className="course-card__kicker">{item.category || 'Курс'}</span>
            <span className="course-card__title">{item.title}</span>
            <span className="course-card__meter" aria-hidden="true">
              <span className="course-card__bar" style={{ width: `${Math.round(item.progress * 100)}%` }} />
            </span>
            <span className="course-card__meta">
              {item.lessons_done} / {item.lessons_total} уроков · {Math.round(item.progress * 100)}%
            </span>
          </button>
        ))}
      </section>

      <p className="page-foot">
        <button className="btn btn--ghost" type="button" onClick={() => navigate('/today')}>
          <Icon name="today" size={16} />
          <span>К экрану «Сегодня»</span>
        </button>
      </p>
    </div>
  );
}
