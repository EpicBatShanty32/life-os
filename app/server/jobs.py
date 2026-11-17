"""Очередь задач Life OS v3 в SQLite и ровно один фоновый воркер.

Таблица ``jobs`` создаётся в :mod:`app.server.db`; миграция v2 добавила
``meta_json`` — туда воркер пишет прогресс и пояснения
(``{"progress": 0.42, "message": "..."}``). Модуль ничего не знает о том, что
именно делают задачи: обработчики передаются словарём ``kind -> handler``.

Жизненный цикл задачи::

    queued --claim_next--> running --handler ok--> done
                                   --исключение--> failed

Правила:

* ``enqueue``/``finish_job``/``fail_job`` коммитят сами — их зовут из API и
  из воркера, транзакция не должна «висеть» на вызывающем коде;
* воркер забирает только те ``kind``, для которых есть обработчик: задачи
  неизвестного типа остаются в ``queued`` и не теряются;
* события ``job.progress`` уходят в WebSocket-брокер, если он передан;
  ошибка брокера не должна ломать обработку задачи;
* в логи попадают только ``id`` и ``kind`` задачи — payload может содержать
  личные пути и не должен утекать в лог.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.server.db import connect
from app.server.settings import Settings

__all__ = [
    "JOB_DONE",
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "JOB_STATUSES",
    "Job",
    "JobHandler",
    "ProgressReporter",
    "WorkerHandle",
    "claim_next",
    "enqueue",
    "fail_job",
    "finish_job",
    "process_one",
    "set_progress",
    "start_worker",
    "worker_loop",
]

logger = logging.getLogger("lifeos.jobs")

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_STATUSES = (JOB_QUEUED, JOB_RUNNING, JOB_DONE, JOB_FAILED)

_MAX_ERROR_CHARS = 500
_MAX_MESSAGE_CHARS = 500

ProgressReporter = Callable[..., None]
JobHandler = Callable[["Job", ProgressReporter], None]


@dataclass(frozen=True)
class Job:
    """Задача очереди в том виде, в каком её видит обработчик."""

    id: int
    kind: str
    payload: dict[str, Any]
    meta: dict[str, Any]
    attempts: int
    created: str


@dataclass
class WorkerHandle:
    """Запущенный воркер: поток + событие остановки."""

    thread: threading.Thread
    stop_event: threading.Event
    kinds: tuple[str, ...] = field(default_factory=tuple)

    def stop(self, timeout: float = 3.0) -> None:
        """Просит воркер завершиться и ждёт поток не дольше ``timeout``."""
        self.stop_event.set()
        self.thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    meta: Mapping[str, Any] | None = None,
) -> int:
    """Ставит задачу в очередь и возвращает её ``id``."""
    kind_value = (kind or "").strip()
    if not kind_value:
        raise ValueError("job.kind не может быть пустым")
    with conn:
        cursor = conn.execute(
            "INSERT INTO jobs(kind, payload_json, meta_json) VALUES (?, ?, ?)",
            (kind_value, _dump_json(payload), _dump_json(meta)),
        )
    return int(cursor.lastrowid or 0)


def claim_next(
    conn: sqlite3.Connection,
    kinds: Iterable[str] | Sequence[str] | None = None,
) -> Job | None:
    """Забирает самую старую задачу ``queued`` и переводит её в ``running``.

    ``attempts`` увеличивается при каждом захвате. Если ``kinds`` задан,
    берутся только задачи этих типов (так воркер не трогает чужие задачи).
    """
    allowed = tuple(dict.fromkeys(str(kind) for kind in (kinds or ()) if kind))
    if allowed:
        placeholders = ", ".join("?" for _ in allowed)
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = ? AND kind IN (%s) "
            "ORDER BY created, id LIMIT 1" % placeholders,
            (JOB_QUEUED, *allowed),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = ? ORDER BY created, id LIMIT 1",
            (JOB_QUEUED,),
        ).fetchone()
    if row is None:
        return None
    job_id = int(row[0])
    with conn:
        updated = conn.execute(
            "UPDATE jobs SET status = ?, attempts = attempts + 1, error = '' "
            "WHERE id = ? AND status = ?",
            (JOB_RUNNING, job_id, JOB_QUEUED),
        )
        if updated.rowcount == 0:
            return None
        fetched = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_from_row(fetched)


def set_progress(
    conn: sqlite3.Connection,
    job_id: int,
    progress: float,
    message: str = "",
    *,
    broker: Any | None = None,
    status: str | None = None,
) -> None:
    """Сохраняет прогресс 0..1 в ``meta_json`` и шлёт событие ``job.progress``.

    ``status`` по умолчанию берётся из БД: внешние обработчики (например,
    транскрибация) используют собственные статусы вроде ``pending_toolchain``,
    и событие должно отражать фактическое состояние задачи, а не ``running``.
    """
    value = _clamp_progress(progress)
    patch: dict[str, Any] = {"progress": value}
    if message:
        patch["message"] = str(message)[:_MAX_MESSAGE_CHARS]
    with conn:
        _patch_meta(conn, job_id, patch)
    _emit_progress(
        broker,
        job_id=job_id,
        status=status or _job_status(conn, job_id),
        progress=value,
        message=patch.get("message", ""),
    )


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    broker: Any | None = None,
    message: str = "",
) -> None:
    """Помечает задачу выполненной: статус ``done``, прогресс 1.0, ``finished``."""
    patch: dict[str, Any] = {"progress": 1.0}
    if message:
        patch["message"] = str(message)[:_MAX_MESSAGE_CHARS]
    with conn:
        _patch_meta(conn, job_id, patch)
        conn.execute(
            "UPDATE jobs SET status = ?, error = '', finished = ? WHERE id = ?",
            (JOB_DONE, _utc_now(), job_id),
        )
    _emit_progress(broker, job_id=job_id, status=JOB_DONE, progress=1.0, message=message)


def fail_job(
    conn: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    broker: Any | None = None,
) -> None:
    """Помечает задачу проваленной: статус ``failed`` и текст ошибки."""
    message = (error or "").strip()[:_MAX_ERROR_CHARS] or "неизвестная ошибка"
    with conn:
        _patch_meta(conn, job_id, {"message": message})
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished = ? WHERE id = ?",
            (JOB_FAILED, message, _utc_now(), job_id),
        )
    row = conn.execute("SELECT meta_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    progress = _load_json(row["meta_json"]).get("progress") if row is not None else None
    _emit_progress(
        broker,
        job_id=job_id,
        status=JOB_FAILED,
        progress=progress,
        message=message,
        error=message,
    )


def process_one(
    conn: sqlite3.Connection,
    handlers: Mapping[str, JobHandler],
    *,
    broker: Any | None = None,
) -> bool:
    """Обрабатывает одну задачу из переданных ``handlers``.

    Возвращает ``True``, если задача была взята и обработана (успешно или с
    ошибкой), и ``False``, если подходящих задач в очереди нет.
    """
    handler_map = dict(handlers or {})
    if not handler_map:
        return False
    job = claim_next(conn, handler_map)
    if job is None:
        return False
    _run_job(conn, job, handler_map[job.kind], broker)
    return True


def worker_loop(
    conn: sqlite3.Connection,
    handlers: Mapping[str, JobHandler],
    stop_event: threading.Event,
    *,
    broker: Any | None = None,
    poll_interval: float = 1.0,
) -> None:
    """Цикл воркера: берёт задачи из очереди до сигнала остановки."""
    handler_map = dict(handlers or {})
    if not handler_map:
        return
    wait = max(0.05, float(poll_interval))
    kinds = tuple(handler_map)
    logger.info("воркер запущен, обработчики: %s", ", ".join(sorted(kinds)))
    while not stop_event.is_set():
        try:
            job = claim_next(conn, kinds)
        except sqlite3.Error as exc:
            logger.warning("воркер: не удалось взять задачу: %s", exc)
            stop_event.wait(wait)
            continue
        if job is None:
            stop_event.wait(wait)
            continue
        _run_job(conn, job, handler_map[job.kind], broker)
    logger.info("воркер остановлен")


def start_worker(
    settings: Settings,
    handlers: Mapping[str, JobHandler],
    *,
    broker: Any | None = None,
    poll_interval: float = 1.0,
    name: str = "lifeos-worker",
) -> WorkerHandle:
    """Запускает воркер в отдельном демон-потоке со своим подключением к БД."""
    handler_map = dict(handlers or {})
    if not handler_map:
        raise ValueError("start_worker: не передан ни один обработчик задач")
    stop_event = threading.Event()

    def _run() -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = connect(settings.db_path)
            worker_loop(
                conn,
                handler_map,
                stop_event,
                broker=broker,
                poll_interval=poll_interval,
            )
        except BaseException:  # поток не должен молча умирать
            logger.exception("воркер упал")
        finally:
            if conn is not None:
                conn.close()

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    return WorkerHandle(thread=thread, stop_event=stop_event, kinds=tuple(handler_map))


# --------------------------------------------------------------------------
# Внутренние помощники
# --------------------------------------------------------------------------


def _run_job(
    conn: sqlite3.Connection,
    job: Job,
    handler: JobHandler,
    broker: Any | None,
) -> None:
    logger.info("задача %d (%s): старт", job.id, job.kind)
    _emit_progress(broker, job_id=job.id, status=JOB_RUNNING, progress=0.0, kind=job.kind)

    def report(progress: float, message: str = "") -> None:
        set_progress(conn, job.id, progress, message, broker=broker)

    try:
        handler(job, report)
    except Exception as exc:  # обработчик не должен ронять воркер
        logger.warning("задача %d (%s) провалена: %s", job.id, job.kind, exc)
        fail_job(conn, job.id, str(exc) or exc.__class__.__name__, broker=broker)
    else:
        finish_job(conn, job.id, broker=broker)
        logger.info("задача %d (%s): готово", job.id, job.kind)


def _emit_progress(broker: Any | None, **payload: Any) -> None:
    if broker is None:
        return
    try:
        broker.publish("job.progress", payload)
    except Exception:  # события не должны ломать обработку задач
        logger.debug("не удалось отправить job.progress", exc_info=True)


def _job_status(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return str(row["status"]) if row is not None else JOB_RUNNING


def _patch_meta(conn: sqlite3.Connection, job_id: int, patch: Mapping[str, Any]) -> None:
    row = conn.execute("SELECT meta_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    meta = _load_json(row["meta_json"])
    meta.update(patch)
    conn.execute("UPDATE jobs SET meta_json = ? WHERE id = ?", (_dump_json(meta), job_id))


def _job_from_row(row: sqlite3.Row | None) -> Job:
    if row is None:
        raise RuntimeError("job не найден")
    data = dict(row)
    return Job(
        id=int(data["id"]),
        kind=str(data["kind"]),
        payload=_load_json(data.get("payload_json")),
        meta=_load_json(data.get("meta_json")),
        attempts=int(data.get("attempts") or 0),
        created=str(data.get("created") or ""),
    )


def _clamp_progress(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return round(max(0.0, min(1.0, number)), 4)


def _dump_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
