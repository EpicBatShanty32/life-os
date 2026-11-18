"""Тесты выключателя расшифровки: ``transcribe.engine = off`` по умолчанию.

Правило, которое здесь закреплено: мак не считает whisper сам по себе.
Если движок выключен, задача получает ``pending_toolchain`` с кодом
``transcribe_disabled`` и причиной «выключено», а не «не хватает ffmpeg»:
бот не должен врать про установку пакетов, которую никто не просил.

Только stdlib и временные каталоги: ffmpeg, faster-whisper и сеть не нужны.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.bot import media  # noqa: E402  (после правки sys.path)
from app.bot.handlers import BotService  # noqa: E402
from app.server import db  # noqa: E402
from app.server import settings as settings_module  # noqa: E402
from app.server import transcribe  # noqa: E402

CHAT_ID = 424242

_AUDIO = b"OggS" + b"0" * 120


class _TempAudio:
    """Временный «аудиофайл»: адаптер проверяет только существование и размер."""

    def __enter__(self) -> Path:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "voice.ogg"
        path.write_bytes(_AUDIO)
        return path

    def __exit__(self, *exc: object) -> None:
        self._dir.cleanup()


def _missing_probe(*names: str):
    return lambda: {
        "ffmpeg": None,
        "ffprobe": None,
        "whisper": None,
        "model": None,
        "missing": list(names),
        "ready": False,
    }


class ResolveEngineTestCase(unittest.TestCase):
    def test_default_is_off(self):
        self.assertEqual(transcribe.DEFAULT_ENGINE, "off")
        self.assertEqual(transcribe.resolve_engine(env={}), "off")

    def test_explicit_engine_wins_over_env(self):
        self.assertEqual(
            transcribe.resolve_engine("local", env={"LIFEOS_TRANSCRIBE_ENGINE": "off"}),
            "local",
        )

    def test_values_are_normalized(self):
        self.assertEqual(
            transcribe.resolve_engine(env={"LIFEOS_TRANSCRIBE_ENGINE": " LOCAL "}),
            "local",
        )

    def test_unknown_engine_falls_back_to_off(self):
        self.assertEqual(
            transcribe.resolve_engine(env={"LIFEOS_TRANSCRIBE_ENGINE": "whisper"}),
            "off",
        )


class DisabledTranscriberTestCase(unittest.TestCase):
    def test_get_transcriber_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIFEOS_TRANSCRIBE_ENGINE", None)
            worker = transcribe.get_transcriber()
        self.assertIsInstance(worker, transcribe.DisabledTranscriber)
        self.assertFalse(worker.available())

    def test_result_is_honest_about_being_off(self):
        with _TempAudio() as path:
            result = transcribe.DisabledTranscriber().transcribe(path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], transcribe.STATUS_PENDING_TOOLCHAIN)
        self.assertEqual(result["code"], transcribe.STATUS_DISABLED)
        self.assertEqual(result["text"], "")
        self.assertIn("выключена", result["reason"])
        self.assertIn("transcribe.engine", result["reason"])
        # Выключено — значит не «не хватает компонентов».
        self.assertEqual(result["missing"], [])

    def test_missing_file_is_reported_first(self):
        result = transcribe.DisabledTranscriber().transcribe("/nope/voice.ogg")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "file_not_found")

    def test_transcribe_helper_uses_disabled_engine(self):
        with _TempAudio() as path:
            result = transcribe.transcribe(path, engine="off")
        self.assertEqual(result["code"], transcribe.STATUS_DISABLED)

    def test_local_engine_still_probes_toolchain(self):
        worker = transcribe.get_transcriber(
            probe=_missing_probe("ffmpeg", "faster-whisper"), engine="local"
        )
        self.assertIsInstance(worker, transcribe.NullTranscriber)


class ToolchainStatusTestCase(unittest.TestCase):
    def test_off_status_is_not_ready_and_not_missing(self):
        info = transcribe.toolchain_status(engine="off")
        self.assertEqual(info["engine"], "off")
        self.assertFalse(info["enabled"])
        self.assertFalse(info["ready"])
        self.assertEqual(info["status"], transcribe.STATUS_PENDING_TOOLCHAIN)
        self.assertEqual(info["missing"], [])
        self.assertIn("выключена", info["reason"])

    def test_local_status_still_names_missing_binaries(self):
        info = transcribe.toolchain_status(
            engine="local",
            env={"LIFEOS_FFMPEG": "/nope/ffmpeg", "LIFEOS_FFPROBE": "/nope/ffprobe"},
        )
        self.assertTrue(info["enabled"])
        self.assertIn("engine", info)
        self.assertEqual(info["engine"], "local")
        if not info["ready"]:
            self.assertIn("ffmpeg", info["missing"])
            self.assertIn("ffmpeg", info["reason"])

    def test_env_switches_status_to_local(self):
        info = transcribe.toolchain_status(env={"LIFEOS_TRANSCRIBE_ENGINE": "local"})
        self.assertEqual(info["engine"], "local")
        self.assertTrue(info["enabled"])


class SettingsTestCase(unittest.TestCase):
    def _load(self, env: dict, config: dict | None = None):
        root = tempfile.mkdtemp(prefix="lifeos-cfg-")
        if config is not None:
            path = Path(root) / "config" / "config.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return settings_module.load_settings(env=env, root=root)

    def test_defaults_are_off(self):
        cfg = self._load({})
        self.assertEqual(cfg.transcribe_engine, "off")
        self.assertEqual(cfg.transcribe_language, "ru")
        self.assertIn("transcribe", settings_module.DEFAULTS)

    def test_env_can_enable_local(self):
        cfg = self._load({"LIFEOS_TRANSCRIBE_ENGINE": "local"})
        self.assertEqual(cfg.transcribe_engine, "local")

    def test_config_file_can_enable_local(self):
        cfg = self._load({}, config={"transcribe": {"engine": "local", "language": "en"}})
        self.assertEqual(cfg.transcribe_engine, "local")
        self.assertEqual(cfg.transcribe_language, "en")

    def test_unknown_engine_is_a_config_error(self):
        with self.assertRaises(settings_module.ConfigError):
            self._load({"LIFEOS_TRANSCRIBE_ENGINE": "whisper"})

    def test_config_file_unknown_engine_is_a_config_error(self):
        with self.assertRaises(settings_module.ConfigError):
            self._load({}, config={"transcribe": {"engine": "server"}})


class BotTextTestCase(unittest.TestCase):
    """Текст бота отличает «выключено» от «не хватает пакетов»."""

    def test_off_is_detected(self):
        service = SimpleNamespace(toolchain={"engine": "off", "enabled": False, "ready": False})
        self.assertTrue(BotService._transcribe_off(service))

    def test_local_is_not_off(self):
        service = SimpleNamespace(toolchain={"engine": "local", "enabled": True, "ready": False})
        self.assertFalse(BotService._transcribe_off(service))

    def test_status_without_flag_is_treated_as_enabled(self):
        service = SimpleNamespace(toolchain={"ready": False, "missing": ["ffmpeg"]})
        self.assertFalse(BotService._transcribe_off(service))


class VoiceReplyTestCase(unittest.TestCase):
    """Живой путь: голосовое с выключенным движком — файл сохранён, текста нет."""

    def _service(self, tmp: Path, engine: str):
        cfg = settings_module.load_settings(
            env={
                "LIFEOS_ADMIN_TELEGRAM_ID": str(CHAT_ID),
                "LIFEOS_DB_PATH": str(tmp / "lifeos.db"),
                "LIFEOS_TRANSCRIBE_ENGINE": engine,
            },
            root=tmp,
        )
        cfg.ensure_dirs()
        conn = db.init_db(cfg.db_path)
        self.addCleanup(conn.close)
        return BotService(cfg, conn)

    def test_voice_reply_says_disabled_not_missing(self):
        tmp = Path(tempfile.mkdtemp(prefix="lifeos-voice-"))
        service = self._service(tmp, "off")
        self.assertTrue(service._transcribe_off())
        reply = service.handle_media(
            media.MediaDescriptor(
                kind="voice", file_id="voice-off", duration=5.0, size=5, chat_id=CHAT_ID
            ),
            b"OggS" + b"0" * 64,
            CHAT_ID,
        )
        self.assertIn("Сохранил", reply.text)
        self.assertIn("выключена на этом устройстве", reply.text)
        self.assertNotIn("не хватает", reply.text)
        # Задача всё равно в очереди: файл не теряется.
        queued = service.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'transcribe'"
        ).fetchone()[0]
        self.assertEqual(queued, 1)

    def test_start_and_stats_do_not_promise_whisper(self):
        tmp = Path(tempfile.mkdtemp(prefix="lifeos-voice-"))
        service = self._service(tmp, "off")
        start = service.handle_start().text
        self.assertIn("Расшифровка голоса выключена", start)
        self.assertNotIn("не хватает", start)
        stats = service.handle_stats().text
        self.assertIn("Транскрибация: выключена на этом устройстве", stats)


if __name__ == "__main__":
    unittest.main()
