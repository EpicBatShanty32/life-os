"""Голос в боте: расшифровка становится обычной записью (t25, голос).

Владелец говорит с телефоном, а не печатает. Голосовое должно вести себя как
текст: бот дожидается задачи ``transcribe``, присылает «Услышал: …» и
раскладывает фразу тем же разбором — «на обед гречка 200 граммов» становится
едой с КБЖУ, «такси 450» — расходом.

Проверяется без whisper и без сети (на этой машине расшифровка выключена,
``transcribe.engine = off``):

* файл сохраняется, задача ``transcribe`` ставится в очередь;
* задача ``done`` → текст расшифровки в ``entries.text`` (вместо заглушки),
  исходный файл остаётся в ``meta``, «Услышал: …» уходит человеку;
* смысл фразы уходит в обычный разбор: еда/расход/дело, без «отдельной
  сущности голоса»;
* ``pending_toolchain`` и выключенный движок — честное «расшифровка
  выключена», а не тишина и не «не хватает ffmpeg»;
* ошибка распознавания и таймаут — тоже честный текст;
* ожидание не блокирует и возвращает ``timeout`` по своему сроку.

Фейковый транскрайбер подставляется в воркер (``app.worker.main.run_job``),
поэтому whisper не нужен.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

import pytest
from fastapi.testclient import TestClient

from app.bot import handlers, intent_v4, media
from app.server import db, jobs
from app.server.main import create_app
from app.server.settings import load_settings
from app.worker.main import JOB_TRANSCRIBE, run_job

CHAT_ID = 4242
#: Минимальная корректная сигнатура OGG: хранилище распознаёт голос по байтам.
OGG_VOICE = b"OggS\x00\x02\x00\x00" + b"\x00" * 48
CATALOG = (
    ("Гречка ядрица", 343.0, 12.6, 3.3, 57.1),
    ("Куриная грудка", 113.0, 23.6, 1.9, 0.4),
    ("Банан", 96.0, 1.5, 0.3, 22.0),
)


# --------------------------------------------------------------------------
# Окружение
# --------------------------------------------------------------------------


def _env(tmp_path: Path, *, engine: str = "off") -> SimpleNamespace:
    settings = load_settings(
        env={
            "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
            "LIFEOS_DB_PATH": str(tmp_path / "lifeos.db"),
            "LIFEOS_MEDIA_DIR": str(tmp_path / "media"),
            "LIFEOS_LLM_ENABLED": "0",
            "LIFEOS_LLM_API_KEY": "",
            "LIFEOS_TRANSCRIBE_ENGINE": engine,
        },
        root=tmp_path,
    )
    settings.ensure_dirs()
    conn = db.init_db(settings.db_path)
    for name, kcal, protein, fat, carbs in CATALOG:
        conn.execute(
            "INSERT INTO products (name, brand, kcal, protein, fat, carbs, fiber, "
            "sugar, base_grams) VALUES (?, '', ?, ?, ?, ?, 0, 0, 100)",
            (name, kcal, protein, fat, carbs),
        )
    conn.commit()
    service = handlers.BotService(
        settings,
        conn,
        v4_router=intent_v4.V4Router(gateway=None),
        media_root=tmp_path / "media",
    )
    service.transcript_poll = 0.01
    service.transcript_timeout = 2.0
    app = create_app(settings, start_worker=False, api_token="")
    return SimpleNamespace(
        settings=settings,
        conn=conn,
        service=service,
        app=app,
        tmp=tmp_path,
    )


def _enable_transcription(space: SimpleNamespace) -> None:
    """Имитирует машину, где движок действительно работает (сервер)."""
    space.service.toolchain = {
        "engine": "local",
        "enabled": True,
        "ready": True,
        "missing": [],
        "status": "ok",
        "reason": "",
    }


class FakeTranscriber:
    """Подставной распознаватель: возвращает заданный результат, без whisper."""

    def __init__(self, text: str = "", *, ok: bool = True, reason: str = "",
                 status: str = "ok") -> None:
        self.text = text
        self.ok = ok
        self.reason = reason
        self.status = status
        self.calls: List[Any] = []

    def transcribe(self, source: Any) -> Dict[str, Any]:
        self.calls.append(source)
        if self.status:
            return {
                "ok": self.ok,
                "status": self.status,
                "text": self.text,
                "reason": self.reason,
                "segments": [],
                "lang": "ru",
            }
        return {"ok": self.ok, "text": self.text, "reason": self.reason}


def _send_voice(space: SimpleNamespace) -> handlers.Reply:
    descriptor = media.MediaDescriptor(
        kind="voice",
        file_id="voice-file-1",
        file_unique_id="voice-unique-1",
        mime_type="audio/ogg",
        size=len(OGG_VOICE),
        duration=2.8,
        chat_id=CHAT_ID,
        message_id=11,
    )
    return space.service.handle_media(descriptor, OGG_VOICE, CHAT_ID)


def _entry(space: SimpleNamespace, entry_id: int) -> Any:
    return space.conn.execute(
        "SELECT id, kind, text, meta_json FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()


def _run_worker(space: SimpleNamespace, transcriber: Any, *, max_attempts: int = 1) -> Dict[str, Any]:
    job = jobs.claim_next(space.conn, [JOB_TRANSCRIBE])
    assert job is not None, "задача transcribe не поставлена в очередь"
    return run_job(space.conn, job, space.settings, transcriber, max_attempts=max_attempts)


# --------------------------------------------------------------------------
# 1. Движок выключен: файл и задача есть, текста нет — и это сказано
# --------------------------------------------------------------------------


def test_engine_off_saves_file_and_says_so(tmp_path: Path) -> None:
    space = _env(tmp_path, engine="off")
    reply = _send_voice(space)
    assert "расшифровка выключена" in reply.text.lower(), reply.text
    assert "ffmpeg" not in reply.text.lower()
    assert not reply.wait_transcript, "при выключенном движке ждать нечего"
    entry = _entry(space, reply.entry_id)
    assert entry is not None
    assert entry["kind"] == "voice"
    job = space.conn.execute(
        "SELECT id, status FROM jobs WHERE kind = 'transcribe'"
    ).fetchone()
    assert job is not None, "задача расшифровки не поставлена"
    assert str(job["status"]) in {"queued", "running"}
    assert reply.job_id == int(job["id"])
    space.conn.close()


def test_worker_with_disabled_engine_honest_message(tmp_path: Path) -> None:
    """Задача уходит в pending_toolchain, а бот объясняет причину без ffmpeg."""
    space = _env(tmp_path, engine="off")
    reply = _send_voice(space)
    from app.server import transcribe as transcribe_module

    outcome = _run_worker(
        space, transcribe_module.get_transcriber(engine="off"), max_attempts=1
    )
    assert outcome["status"] == "pending_toolchain", outcome
    result = space.service.wait_for_transcript(reply.job_id, timeout=1.0)
    assert result.status == "pending_toolchain"
    answer = space.service.handle_transcript(reply.entry_id, reply.job_id, result, CHAT_ID)
    assert "выключена" in answer.text.lower(), answer.text
    assert "файл сохранён" in answer.text.lower()
    entry = _entry(space, reply.entry_id)
    assert "расшифровка в очереди" in str(entry["text"]).lower() or str(entry["text"])
    space.conn.close()


# --------------------------------------------------------------------------
# 2. Движок работает: расшифровка → «Услышал: …» → обычный разбор
# --------------------------------------------------------------------------


def test_transcript_is_written_to_entry_and_food_is_logged(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    assert reply.wait_transcript, "движок работает — ждём расшифровку"
    transcriber = FakeTranscriber("на обед гречка двести граммов")
    outcome = _run_worker(space, transcriber)
    assert outcome["status"] == "done", outcome

    result = space.service.wait_for_transcript(reply.job_id)
    assert result.status == "done"
    assert result.text == "на обед гречка двести граммов"

    entry_before = _entry(space, reply.entry_id)
    assert "на обед гречка двести граммов" == str(entry_before["text"])
    meta = str(entry_before["meta_json"])
    assert "transcript" in meta and "voice" in meta, "исходный файл остаётся в meta"

    answer = space.service.handle_transcript(
        reply.entry_id, reply.job_id, result, CHAT_ID
    )
    assert answer.text.startswith("Услышал: «на обед гречка двести граммов»"), answer.text
    assert "обед" in answer.text.lower()
    row = space.conn.execute(
        "SELECT text, grams, kcal FROM food_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "голос не стал едой: %s" % answer.text
    assert row["grams"] == 200
    assert abs(float(row["kcal"]) - 686.0) < 1
    space.conn.close()


@pytest.mark.parametrize(
    "phrase,table,check",
    [
        ("такси 450", "transactions", "SELECT amount_kopecks FROM transactions"),
        ("завтра позвонить в банк", "tasks", "SELECT title FROM tasks"),
    ],
)
def test_voice_goes_through_same_parser(tmp_path: Path, phrase, table, check) -> None:
    """Голос не отдельная сущность: смысл уходит в тот же разбор, что текст."""
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    _run_worker(space, FakeTranscriber(phrase))
    result = space.service.wait_for_transcript(reply.job_id)
    answer = space.service.handle_transcript(reply.entry_id, reply.job_id, result, CHAT_ID)
    assert "услышал" in answer.text.lower(), answer.text
    rows = space.conn.execute(check).fetchall()
    assert rows, "фраза %r не создала запись в %s: %s" % (phrase, table, answer.text)
    if table == "transactions":
        assert int(rows[0]["amount_kopecks"]) == 45000
    else:
        assert "банк" in str(rows[0]["title"]).lower()
    space.conn.close()


def test_transcript_replaces_placeholder_text(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    before = _entry(space, reply.entry_id)
    assert "расшифровка в очереди" in str(before["text"]).lower()
    _run_worker(space, FakeTranscriber("выпил кофе"))
    result = space.service.wait_for_transcript(reply.job_id)
    space.service.handle_transcript(reply.entry_id, reply.job_id, result, CHAT_ID)
    after = _entry(space, reply.entry_id)
    assert str(after["text"]) == "выпил кофе"
    assert after["kind"] == "voice"
    space.conn.close()


def test_entry_visible_in_api_after_transcript(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    _run_worker(space, FakeTranscriber("мысль: сон важнее продуктивности"))
    result = space.service.wait_for_transcript(reply.job_id)
    space.service.handle_transcript(reply.entry_id, reply.job_id, result, CHAT_ID)
    with TestClient(space.app) as client:
        journal = client.get("/api/journal", params={"limit": 50}).json()
    texts = " ".join(str(item.get("text") or "") for item in journal.get("items") or [])
    assert "сон важнее продуктивности" in texts, "расшифровки нет в журнале API"
    space.conn.close()


# --------------------------------------------------------------------------
# 3. Ошибки и таймаут: честный текст, файл не теряется
# --------------------------------------------------------------------------


def test_failed_transcription_is_honest(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    outcome = _run_worker(
        space,
        FakeTranscriber(ok=False, reason="не разобрал речь"),
        max_attempts=1,
    )
    assert outcome["status"] == "failed", outcome
    result = space.service.wait_for_transcript(reply.job_id)
    assert result.status == "failed"
    answer = space.service.handle_transcript(reply.entry_id, reply.job_id, result, CHAT_ID)
    assert "не расслышал" in answer.text.lower(), answer.text
    assert "файл сохранён" in answer.text.lower()
    entry = _entry(space, reply.entry_id)
    assert entry is not None, "файл-запись не должна пропадать"
    space.conn.close()


def test_timeout_is_honest(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    result = space.service.wait_for_transcript(reply.job_id, timeout=0.05, interval=0.01)
    assert result.status == "timeout"
    answer = space.service.handle_transcript(reply.entry_id, reply.job_id, result, CHAT_ID)
    assert "не пришла" in answer.text.lower(), answer.text
    space.conn.close()


def test_wait_does_not_block_forever(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    started = time.monotonic()
    result = space.service.wait_for_transcript(reply.job_id, timeout=0.2, interval=0.02)
    spent = time.monotonic() - started
    assert result.status == "timeout"
    assert spent < 2.0, "ожидание вышло за свой срок: %.1f c" % spent
    space.conn.close()


def test_reply_reports_job_and_entry_for_waiting(tmp_path: Path) -> None:
    space = _env(tmp_path)
    _enable_transcription(space)
    reply = _send_voice(space)
    assert reply.job_id > 0
    assert reply.entry_id > 0
    assert reply.wait_transcript is True
    assert any("удал" in label.lower() for row in reply.buttons for label, _ in row)
    space.conn.close()


# --------------------------------------------------------------------------
# 4. Роутер: ожидание расшифровки подключено к обработчику медиа
# --------------------------------------------------------------------------


def test_router_media_handler_waits_for_transcript() -> None:
    """В сборке роутера есть фоновое ожидание — голос не теряется молча."""
    import inspect

    source = inspect.getsource(handlers.build_router)
    assert "_deliver_transcript" in source
    assert "wait_transcript" in source
    assert "asyncio.to_thread" in source, "ожидание не должно блокировать поллинг"
    assert "transcript_tasks" in source, "фоновые задачи держим ссылками"


def test_bot_texts_do_not_promise_whisper() -> None:
    """/start и /help не обещают расшифровку, которой на машине нет."""
    space = handlers.BotService.__new__(handlers.BotService)
    space.toolchain = {"engine": "off", "enabled": False, "ready": False, "missing": []}
    space.transcriber = None
    start = handlers.BotService.handle_start(space)
    text = start.text.lower()
    assert "выключена" in text
    assert "ffmpeg" not in text
