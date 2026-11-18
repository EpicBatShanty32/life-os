"""Воркер Life OS v3: очередь ``jobs`` -> транскрибация -> урок/запись.

Запуск::

    LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.worker.main
    LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.worker.main --once

Очередь переиспользуется из :mod:`app.server.jobs`: ``enqueue`` кладёт задачу,
``claim_next(["transcribe"])`` атомарно забирает её (``status='running'``,
``attempts += 1``), а мы сами выставляем специфичные для транскрибации статусы:

* ``done`` — текст и сегменты сохранены;
* ``pending_toolchain`` — нет ffmpeg/faster-whisper; задача не ретраится и
  ждёт установки, в ``jobs.error``/``meta.message`` понятная причина;
* ``queued`` — временная ошибка, повтор с backoff (5/30/120 с), пока
  ``attempts`` не достигнет ``--max-attempts``; дальше ``failed``.

Прогресс пишется в ``jobs.meta_json`` (``progress`` 0..1, ``message``) через
``jobs.set_progress``, поэтому виден в API/UI. Payload и личные пути в логи не
попадают: только id и kind задачи.

SIGTERM/SIGINT: воркер дочитывает текущую задачу, затем выходит; задача не
теряется — при повторном запуске она либо уже ``done``, либо снова ``queued``.
"""

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from app.server import db, jobs
from app.server import transcribe as transcribe_module
from app.server.settings import ConfigError, Settings, load_settings

logger = logging.getLogger("lifeos.worker")

__all__ = [
    "BACKOFF_SECONDS",
    "JOB_TRANSCRIBE",
    "MAX_ATTEMPTS",
    "PENDING_TOOLCHAIN",
    "TranscribeJobHandler",
    "configure_logging",
    "main",
    "open_database",
    "run_job",
    "run_once",
    "worker_loop",
]

JOB_TRANSCRIBE = "transcribe"
PENDING_TOOLCHAIN = "pending_toolchain"
MAX_ATTEMPTS = 3
#: Пауза перед повтором после 1-й, 2-й... неудачи (attempts уже увеличен).
BACKOFF_SECONDS = (5.0, 30.0, 120.0)
POLL_INTERVAL_S = 2.0
DB_HINT = (
    "База Life OS v0.x не открывается в v3. Укажите отдельный файл: "
    "LIFEOS_DB_PATH=data/lifeos-v3.db python3 -m app.worker.main"
)


class JobPending(Exception):
    """Не хватает инструментария: задача ждёт установки, ретраи не помогут."""


class JobRetry(Exception):
    """Временная ошибка: задачу можно повторить с backoff."""


class TranscribeJobHandler:
    """Расшифровка одного медиафайла и запись результата в БД."""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        transcriber: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.transcriber = transcriber

    def __call__(self, job: jobs.Job, report: Any) -> Dict[str, Any]:
        return self.run(job, report)

    def run(self, job: jobs.Job, report: Any) -> Dict[str, Any]:
        payload = job.payload if isinstance(job.payload, dict) else {}
        source = self._resolve_media(payload)
        report(0.1, "читаю файл")

        transcriber = self.transcriber or transcribe_module.get_transcriber(
            engine=getattr(self.settings, "transcribe_engine", None)
        )
        try:
            result = transcriber.transcribe(source)
        except Exception as exc:  # на случай сторонней реализации
            raise JobRetry(
                "транскрибатор упал (%s): %s" % (type(exc).__name__, _short(exc))
            ) from exc
        if not isinstance(result, dict):
            raise JobRetry("транскрибатор вернул результат неизвестного типа")

        status = str(result.get("status") or "")
        if status == transcribe_module.STATUS_PENDING_TOOLCHAIN:
            raise JobPending(str(result.get("reason") or "нужен ffmpeg и faster-whisper"))
        if not result.get("ok"):
            raise JobRetry(str(result.get("reason") or "не удалось расшифровать файл"))

        text = str(result.get("text") or "").strip()
        if not text:
            raise JobRetry("расшифровка пустая — нечего сохранять")
        lang = str(result.get("lang") or payload.get("language") or "ru").strip()[:10] or "ru"
        segments = _clean_segments(result.get("segments"))
        report(0.6, "сохраняю расшифровку")

        lesson_id = _as_int(payload.get("lesson_id"))
        if lesson_id is not None:
            self._save_lesson(lesson_id, text, lang, segments, result, report)
        entry_id = _as_int(payload.get("entry_id"))
        if entry_id is not None:
            self._save_entry(entry_id, text, lang, segments, lesson_id)
        report(0.9, "готово")
        return {
            "text_chars": len(text),
            "segments": len(segments),
            "lang": lang,
        }

    # -- сохранение -------------------------------------------------------

    def _save_lesson(
        self,
        lesson_id: int,
        text: str,
        lang: str,
        segments: list,
        result: Mapping[str, Any],
        report: Any,
    ) -> None:
        row = self.conn.execute(
            "SELECT id, duration_s FROM lessons WHERE id = ?", (lesson_id,)
        ).fetchone()
        if row is None:
            raise JobRetry("урок %d не найден в БД" % lesson_id)
        duration = _as_float(result.get("duration"))
        with self.conn:
            self.conn.execute(
                "INSERT INTO transcripts(lesson_id, lang, text) VALUES (?, ?, ?) "
                "ON CONFLICT(lesson_id, lang) DO UPDATE SET text = excluded.text",
                (lesson_id, lang, text),
            )
            self.conn.execute("DELETE FROM segments WHERE lesson_id = ?", (lesson_id,))
            for segment in segments:
                self.conn.execute(
                    "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
                    (lesson_id, segment["start"], segment["end"], segment["text"]),
                )
            if duration and duration > 0:
                self.conn.execute(
                    "UPDATE lessons SET duration_s = ? "
                    "WHERE id = ? AND (duration_s IS NULL OR duration_s <= 0)",
                    (duration, lesson_id),
                )
        report(0.8, "сегменты: %d" % len(segments))

    def _save_entry(
        self,
        entry_id: int,
        text: str,
        lang: str,
        segments: list,
        lesson_id: Optional[int],
    ) -> None:
        row = self.conn.execute(
            "SELECT meta_json FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            logger.warning("запись %d исчезла — расшифровка останется в задаче", entry_id)
            return
        meta = _load_json(row["meta_json"])
        meta.update(
            {
                "transcript_status": "done",
                "transcript_lang": lang,
                "transcript_chars": len(text),
                "transcript_segments": len(segments),
                "transcript": text[:2000],
            }
        )
        if lesson_id is not None:
            meta["lesson_id"] = lesson_id
        with self.conn:
            self.conn.execute(
                "UPDATE entries SET meta_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")), entry_id),
            )

    # -- пути -------------------------------------------------------------

    def _resolve_media(self, payload: Mapping[str, Any]) -> Path:
        raw = payload.get("media_path") or payload.get("path")
        if not raw:
            raise JobRetry("в задаче нет media_path")
        relative = Path(str(raw))
        if relative.is_absolute():
            raise JobRetry("абсолютный путь в задаче запрещён")
        root = Path(self.settings.media_dir).expanduser().resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise JobRetry("путь задачи выходит за пределы data/media") from None
        if not candidate.is_file():
            raise JobRetry("файл задачи не найден: %s" % candidate.name)
        return candidate


def run_job(
    conn: sqlite3.Connection,
    job: jobs.Job,
    settings: Settings,
    transcriber: Optional[Any] = None,
    *,
    broker: Any = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Dict[str, Any]:
    """Обрабатывает одну уже захваченную задачу; возвращает исход для цикла."""

    def report(progress: float, message: str = "") -> None:
        jobs.set_progress(conn, job.id, progress, message, broker=broker)

    handler = TranscribeJobHandler(settings, conn, transcriber)
    try:
        result = handler.run(job, report)
    except JobPending as exc:
        reason = _short(exc)
        _mark_pending(conn, job.id, reason, report, broker=broker)
        logger.info("задача %d (%s): pending_toolchain — %s", job.id, job.kind, reason)
        return {
            "job_id": job.id,
            "kind": job.kind,
            "status": PENDING_TOOLCHAIN,
            "reason": reason,
            "retry_in": 0.0,
        }
    except Exception as exc:
        reason = _short(exc)
        if job.attempts >= max(1, int(max_attempts)):
            jobs.fail_job(conn, job.id, reason, broker=broker)
            logger.warning("задача %d (%s): failed — %s", job.id, job.kind, reason)
            return {
                "job_id": job.id,
                "kind": job.kind,
                "status": jobs.JOB_FAILED,
                "reason": reason,
                "retry_in": 0.0,
            }
        delay = backoff_for(job.attempts)
        _requeue(conn, job.id, reason, delay, broker=broker)
        logger.warning(
            "задача %d (%s): повтор через %.0f c — %s",
            job.id,
            job.kind,
            delay,
            reason,
        )
        return {
            "job_id": job.id,
            "kind": job.kind,
            "status": jobs.JOB_QUEUED,
            "reason": reason,
            "retry_in": delay,
        }

    message = "расшифровано: %d символов, %d сегментов" % (
        result.get("text_chars", 0),
        result.get("segments", 0),
    )
    jobs.finish_job(conn, job.id, broker=broker, message=message)
    logger.info("задача %d (%s): done — %s", job.id, job.kind, message)
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": jobs.JOB_DONE,
        "result": result,
        "retry_in": 0.0,
    }


def run_once(
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: Optional[Any] = None,
    *,
    broker: Any = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Optional[Dict[str, Any]]:
    """Забирает и обрабатывает максимум одну задачу; ``None`` — очередь пуста."""
    job = jobs.claim_next(conn, [JOB_TRANSCRIBE])
    if job is None:
        return None
    return run_job(
        conn,
        job,
        settings,
        transcriber,
        broker=broker,
        max_attempts=max_attempts,
    )


def worker_loop(
    conn: sqlite3.Connection,
    settings: Settings,
    stop_event: threading.Event,
    transcriber: Optional[Any] = None,
    *,
    broker: Any = None,
    poll_interval: float = POLL_INTERVAL_S,
    max_attempts: int = MAX_ATTEMPTS,
) -> None:
    """Цикл воркера до ``stop_event``; backoff ждём, не занимая CPU."""
    interval = max(0.05, float(poll_interval))
    logger.info("воркер запущен: kind=%s", JOB_TRANSCRIBE)
    while not stop_event.is_set():
        try:
            job = jobs.claim_next(conn, [JOB_TRANSCRIBE])
        except sqlite3.Error as exc:
            logger.warning("воркер: не удалось взять задачу: %s", exc)
            stop_event.wait(interval)
            continue
        if job is None:
            stop_event.wait(interval)
            continue
        outcome = run_job(
            conn,
            job,
            settings,
            transcriber,
            broker=broker,
            max_attempts=max_attempts,
        )
        delay = float(outcome.get("retry_in") or 0.0)
        if delay > 0 and not stop_event.is_set():
            stop_event.wait(delay)
    logger.info("воркер остановлен")


def backoff_for(attempts: int) -> float:
    """Пауза после неудачи: 5 с, 30 с, 120 с, дальше 120 с."""
    index = max(0, min(int(attempts) - 1, len(BACKOFF_SECONDS) - 1))
    return float(BACKOFF_SECONDS[index])


def open_database(settings: Settings) -> sqlite3.Connection:
    """Открывает БД v3; на базе v0.x бросает :class:`app.server.db.LegacySchemaError`."""
    return db.init_db(settings.db_path)


def configure_logging(settings: Settings, level: int = logging.INFO) -> None:
    """Консоль + ``data/logs/worker.log`` с ротацией; без дублей обработчиков."""
    root = logging.getLogger()
    if getattr(root, "_lifeos_worker_configured", False):
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        Path(settings.logs_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(Path(settings.logs_dir) / "worker.log"),
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        logger.warning("не удалось открыть файл лога воркера — пишу только в консоль")
    root._lifeos_worker_configured = True  # type: ignore[attr-defined]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Life OS v3: обработка очереди задач")
    parser.add_argument("--once", action="store_true", help="обработать одну задачу и выйти")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL_S,
        help="пауза между опросами очереди, секунды",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_ATTEMPTS,
        help="сколько попыток делать до статуса failed",
    )
    parser.add_argument("--config", default=None, help="путь к config.json")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = (
            load_settings(config_path=args.config)
            if args.config
            else load_settings()
        )
    except ConfigError as exc:
        logger.error("конфигурация: %s", exc)
        return 2

    configure_logging(settings)
    settings.ensure_dirs()
    try:
        conn = open_database(settings)
    except db.LegacySchemaError:
        logger.error(DB_HINT)
        return 2
    except db.SchemaVersionError as exc:
        logger.error("файл БД несовместим с этой версией: %s", exc)
        return 2

    transcribe_engine = getattr(settings, "transcribe_engine", None)
    transcriber = transcribe_module.get_transcriber(engine=transcribe_engine)
    info = transcribe_module.toolchain_status(engine=transcribe_engine)
    if not info.get("enabled"):
        logger.warning(
            "транскрибация выключена на этом устройстве (transcribe.engine = %s): "
            "задачи останутся в pending_toolchain, файлы сохранены",
            info.get("engine"),
        )
    elif info.get("ready"):
        logger.info("транскрибация готова: ffmpeg + %s", info.get("whisper"))
    else:
        logger.warning(
            "транскрибация недоступна: нет %s; задачи получат статус pending_toolchain",
            ", ".join(info.get("missing") or ["ffmpeg/whisper"]),
        )

    try:
        if args.once:
            outcome = run_once(
                conn,
                settings,
                transcriber,
                max_attempts=max(1, args.max_attempts),
            )
            if outcome is None:
                logger.info("очередь пуста")
            else:
                logger.info("итог: %s", outcome.get("status"))
            return 0
        stop_event = threading.Event()

        def _stop(signum: int, frame: Any) -> None:
            logger.info("получен сигнал %s — завершаю текущую задачу и выхожу", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        worker_loop(
            conn,
            settings,
            stop_event,
            transcriber,
            poll_interval=args.poll_interval,
            max_attempts=max(1, args.max_attempts),
        )
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Служебные переходы статусов
# --------------------------------------------------------------------------


def _mark_pending(
    conn: sqlite3.Connection,
    job_id: int,
    reason: str,
    report: Any,
    *,
    broker: Any = None,
) -> None:
    """Статус pending_toolchain: задача ждёт установки ffmpeg/whisper."""
    now = _utc_now()
    with conn:
        _patch_meta(conn, job_id, {"progress": 0.5, "message": reason})
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished = ? WHERE id = ?",
            (PENDING_TOOLCHAIN, reason[:500], now, job_id),
        )
    try:
        jobs.set_progress(conn, job_id, 0.5, reason, broker=broker, status=PENDING_TOOLCHAIN)
    except Exception:
        logger.debug("не удалось отправить событие pending_toolchain", exc_info=True)


def _requeue(
    conn: sqlite3.Connection,
    job_id: int,
    reason: str,
    delay: float,
    *,
    broker: Any = None,
) -> None:
    """Возвращает задачу в queued с временем следующей попытки в meta_json."""
    next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(
        timespec="seconds"
    )
    with conn:
        _patch_meta(
            conn,
            job_id,
            {
                "progress": 0.0,
                "message": reason,
                "next_attempt_at": next_attempt,
            },
        )
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished = NULL WHERE id = ?",
            (jobs.JOB_QUEUED, reason[:500], job_id),
        )
    try:
        jobs.set_progress(conn, job_id, 0.0, reason, broker=broker, status=jobs.JOB_QUEUED)
    except Exception:
        logger.debug("не удалось отправить событие retry", exc_info=True)


def _patch_meta(conn: sqlite3.Connection, job_id: int, patch: Mapping[str, Any]) -> None:
    row = conn.execute("SELECT meta_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    meta = _load_json(row["meta_json"])
    meta.update(patch)
    conn.execute(
        "UPDATE jobs SET meta_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")), job_id),
    )


# --------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------


def _clean_segments(raw: Any) -> list:
    if not isinstance(raw, (list, tuple)):
        return []
    cleaned = []
    for item in raw[:5000]:
        if not isinstance(item, Mapping):
            continue
        start = _as_float(item.get("start"))
        end = _as_float(item.get("end"))
        text = str(item.get("text") or "").strip()
        if start is None or end is None or not text or end < start:
            continue
        cleaned.append({"start": float(start), "end": float(end), "text": text[:2000]})
    return cleaned


def _load_json(value: Any) -> Dict[str, Any]:
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


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _short(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")[:400]
    return text or exc.__class__.__name__


if __name__ == "__main__":
    sys.exit(main())
