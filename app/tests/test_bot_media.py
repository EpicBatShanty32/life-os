"""Тесты ``app/bot``: медиахранилище и адаптер транскрибации.

Проверяются:

* определение типа по сигнатуре и игнорирование расширения файла;
* раскладка ``media/<YYYY>/<MM>/<id>.<ext>`` и безопасный идентификатор;
* лимиты 20 МБ (Bot API) и 2 ГБ (локальный сервер);
* атомарная запись через ``os.replace``: нет временных файлов, при сбое
  прежняя версия цела;
* честный статус ``pending_toolchain`` без ffmpeg/whisper;
* разбор результата whisper на фейковой модели (без установки whisper).

Только stdlib и временные каталоги: сеть, Telegram и настоящие
ffmpeg/whisper не нужны. Картинки не создаются.

Запуск из корня проекта::

    python3 -m unittest discover -s app/tests -q
    PYTHONPYCACHEPREFIX=/tmp/lifeos-pyc /usr/bin/python3 -m unittest discover -s app/tests -q
"""

import hashlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.bot import media_store, transcribe  # noqa: E402  (после правки sys.path)

FIXED_NOW = datetime(2026, 11, 17, 12, 0, 0)

# Минимальные корректные сигнатуры: содержимое не обязано быть играбельным,
# проверяется только распознавание формата по первым байтам.
OGG_VOICE = b"OggS\x00\x02\x00\x00" + b"\x00" * 48
MP3_AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 40
MP4_VIDEO = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32
M4A_AUDIO = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * 24
PDF_DOCUMENT = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b" " * 32
ZIP_DOCUMENT = b"PK\x03\x04\x14\x00\x00\x00" + b"\x00" * 40
UNKNOWN = b"\x99\x88\x77\x66" + b"no known signature" * 3


class _TempDirTestCase(unittest.TestCase):
    """Каждый тест получает свежий временный каталог и ничего не трогает вне него."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="lifeos-bot-test-")
        self.addCleanup(self._temporary.cleanup)
        self.tmp = Path(self._temporary.name).resolve()
        self.media_root = self.tmp / "data" / "media"

    def _files(self):
        if not self.media_root.exists():
            return []
        return [item for item in self.media_root.rglob("*") if item.is_file()]

    def _temp_files(self):
        return [item for item in self._files() if item.name.startswith(".tmp-")]


class SniffTestCase(_TempDirTestCase):
    def test_audio_signature_wins_over_extension(self):
        path = self.tmp / "voice.txt"
        path.write_bytes(OGG_VOICE)
        self.assertEqual(media_store.detect_kind(path.read_bytes()), media_store.KIND_AUDIO)
        signature = media_store.read_signature(path)
        self.assertEqual(
            signature,
            media_store.Signature(media_store.KIND_AUDIO, "ogg", "audio/ogg"),
        )

    def test_video_and_document_signatures(self):
        video = self.tmp / "clip.mp3"
        video.write_bytes(MP4_VIDEO)
        signature = media_store.read_signature(video)
        self.assertEqual((signature.kind, signature.ext), (media_store.KIND_VIDEO, "mp4"))

        document = self.tmp / "note.ogg"
        document.write_bytes(PDF_DOCUMENT)
        signature = media_store.read_signature(document)
        self.assertEqual(
            signature,
            media_store.Signature(media_store.KIND_DOCUMENT, "pdf", "application/pdf"),
        )

    def test_m4a_is_audio_and_zip_is_document(self):
        self.assertEqual(media_store.detect_kind(M4A_AUDIO), media_store.KIND_AUDIO)
        self.assertEqual(media_store.detect_kind(ZIP_DOCUMENT), media_store.KIND_DOCUMENT)
        self.assertEqual(media_store.detect_kind(MP3_AUDIO), media_store.KIND_AUDIO)

    def test_unknown_or_short_signature_returns_none(self):
        self.assertIsNone(media_store.sniff(UNKNOWN))
        self.assertIsNone(media_store.sniff(b""))
        self.assertIsNone(media_store.sniff(b"ID"))


class MediaLayoutTestCase(_TempDirTestCase):
    def test_layout_year_month_and_result_fields(self):
        result = media_store.save_bytes(
            self.media_root, "tg-777", OGG_VOICE, now=FIXED_NOW
        )
        self.assertTrue(result["ok"])
        expected = self.media_root / "2026" / "11" / "tg-777.ogg"
        self.assertEqual(Path(result["path"]), expected)
        self.assertEqual(expected.read_bytes(), OGG_VOICE)
        self.assertEqual(result["rel_path"], "2026/11/tg-777.ogg")
        self.assertEqual(result["kind"], media_store.KIND_AUDIO)
        self.assertEqual(result["mime"], "audio/ogg")
        self.assertEqual(result["ext"], "ogg")
        self.assertEqual(result["size"], len(OGG_VOICE))
        self.assertEqual(result["sha256"], hashlib.sha256(OGG_VOICE).hexdigest())
        self.assertEqual(result["year"], "2026")
        self.assertEqual(result["month"], "11")
        self.assertEqual(result["created_at"], "2026-11-17T12:00:00")
        self.assertIsNone(result["declared_kind"])
        self.assertFalse(result["signature_mismatch"])

    def test_extension_comes_from_signature_not_filename(self):
        result = media_store.save_bytes(
            self.media_root,
            "doc-1",
            PDF_DOCUMENT,
            now=FIXED_NOW,
            filename="lecture.txt",
            kind_hint="document",
        )
        self.assertEqual(result["ext"], "pdf")
        self.assertEqual(Path(result["path"]).name, "doc-1.pdf")

        result = media_store.save_bytes(
            self.media_root,
            "video-1",
            MP4_VIDEO,
            now=FIXED_NOW,
            filename="clip.mp3",
            kind_hint="video",
        )
        self.assertEqual(result["ext"], "mp4")
        self.assertEqual(result["kind"], media_store.KIND_VIDEO)

    def test_unknown_document_uses_safe_fallback_extension(self):
        result = media_store.save_bytes(
            self.media_root,
            "file-1",
            UNKNOWN,
            now=FIXED_NOW,
            filename="../../report.docx",
        )
        self.assertEqual(result["kind"], media_store.KIND_DOCUMENT)
        self.assertEqual(result["ext"], "docx")
        self.assertEqual(result["mime"], "application/octet-stream")
        self.assertEqual(Path(result["path"]).name, "file-1.docx")

    def test_unknown_media_type_is_rejected(self):
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_bytes(
                self.media_root,
                "fake-photo",
                UNKNOWN,
                now=FIXED_NOW,
                kind_hint="photo",
            )
        self.assertEqual(caught.exception.code, "unknown_type")
        self.assertIn("не сохранён", caught.exception.reason)
        self.assertEqual(self._files(), [])

    def test_voice_hint_is_normalized_to_audio(self):
        result = media_store.save_bytes(
            self.media_root, "voice-1", OGG_VOICE, now=FIXED_NOW, kind_hint="voice"
        )
        self.assertEqual(result["kind"], media_store.KIND_AUDIO)
        self.assertEqual(result["declared_kind"], media_store.KIND_AUDIO)
        self.assertFalse(result["signature_mismatch"])

    def test_signature_mismatch_is_recorded_but_signature_wins(self):
        result = media_store.save_bytes(
            self.media_root, "mismatch-1", OGG_VOICE, now=FIXED_NOW, kind_hint="video"
        )
        self.assertEqual(result["kind"], media_store.KIND_AUDIO)
        self.assertTrue(result["signature_mismatch"])

    def test_same_id_replaces_atomically_and_keeps_one_file(self):
        first = media_store.save_bytes(self.media_root, "dup", OGG_VOICE, now=FIXED_NOW)
        second_payload = OGG_VOICE + b"\x01" * 8
        second = media_store.save_bytes(self.media_root, "dup", second_payload, now=FIXED_NOW)
        self.assertEqual(first["path"], second["path"])
        path = Path(second["path"])
        self.assertEqual(path.read_bytes(), second_payload)
        self.assertEqual(
            [item.name for item in path.parent.iterdir() if item.is_file()],
            ["dup.ogg"],
        )
        self.assertEqual(self._temp_files(), [])

    def test_save_file_streams_source(self):
        source = self.tmp / "incoming.bin"
        source.write_bytes(OGG_VOICE)
        result = media_store.save_file(
            self.media_root, "from-disk", source, now=FIXED_NOW
        )
        self.assertEqual(result["ext"], "ogg")
        self.assertEqual(Path(result["path"]).read_bytes(), OGG_VOICE)

    def test_save_file_missing_source(self):
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_file(self.media_root, "missing", self.tmp / "nope.ogg")
        self.assertEqual(caught.exception.code, "not_found")

    def test_course_lesson_folder_layout(self):
        result = media_store.save_bytes(
            self.media_root,
            "lesson-part",
            OGG_VOICE,
            now=FIXED_NOW,
            folder="Курс по бегу/3",
        )
        expected = self.media_root / "Курс по бегу" / "3" / "lesson-part.ogg"
        self.assertEqual(Path(result["path"]), expected)
        self.assertEqual(result["rel_path"], "Курс по бегу/3/lesson-part.ogg")
        self.assertTrue(expected.is_file())

    def test_bad_folders_are_rejected(self):
        bad_folders = ["../escape", "/absolute", ".hidden", "a/../b", ""]
        for folder in bad_folders:
            with self.subTest(folder=folder):
                with self.assertRaises(media_store.MediaError) as caught:
                    media_store.save_bytes(
                        self.media_root, "id-1", OGG_VOICE, now=FIXED_NOW, folder=folder
                    )
                self.assertEqual(caught.exception.code, "bad_folder")
        self.assertEqual(self._files(), [])


class LimitsTestCase(_TempDirTestCase):
    def test_bot_api_limit_is_20_mb(self):
        self.assertEqual(media_store.BOT_API_MAX_BYTES, 20 * 1024 * 1024)
        self.assertTrue(media_store.fits_bot_api(media_store.BOT_API_MAX_BYTES))
        self.assertFalse(media_store.fits_bot_api(media_store.BOT_API_MAX_BYTES + 1))
        self.assertFalse(media_store.fits_bot_api(0))

        media_store.check_size(
            media_store.BOT_API_MAX_BYTES, media_store.BOT_API_MAX_BYTES
        )
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.check_size(
                media_store.BOT_API_MAX_BYTES + 1, media_store.BOT_API_MAX_BYTES
            )
        self.assertEqual(caught.exception.code, "too_large")
        self.assertIn("Bot API", caught.exception.reason)
        self.assertIn("20 МБ", caught.exception.reason)

    def test_local_limit_is_2_gb(self):
        self.assertEqual(media_store.LOCAL_MAX_BYTES, 2 * 1024 * 1024 * 1024)
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.check_size(media_store.LOCAL_MAX_BYTES + 1)
        self.assertEqual(caught.exception.code, "too_large")
        self.assertIn("2 ГБ", caught.exception.reason)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_bytes(self.media_root, "empty", b"", now=FIXED_NOW)
        self.assertEqual(caught.exception.code, "empty")
        self.assertEqual(self._files(), [])

    def test_stream_limit_aborts_without_partial_file(self):
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_stream(
                self.media_root,
                "too-big",
                io.BytesIO(b"x" * 11),
                now=FIXED_NOW,
                max_bytes=10,
            )
        self.assertEqual(caught.exception.code, "too_large")
        self.assertEqual(self._files(), [])

    def test_save_bytes_honours_custom_limit(self):
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_bytes(
                self.media_root, "small-limit", OGG_VOICE, now=FIXED_NOW, max_bytes=8
            )
        self.assertEqual(caught.exception.code, "too_large")
        self.assertEqual(self._files(), [])


class AtomicWriteTestCase(_TempDirTestCase):
    def test_failed_replace_keeps_previous_version_and_cleans_temp(self):
        first = media_store.save_bytes(self.media_root, "atomic", OGG_VOICE, now=FIXED_NOW)
        path = Path(first["path"])
        previous = path.read_bytes()

        with mock.patch.object(media_store.os, "replace", side_effect=OSError("диск переполнен")):
            with self.assertRaises(media_store.MediaError) as caught:
                media_store.save_bytes(
                    self.media_root, "atomic", OGG_VOICE + b"new", now=FIXED_NOW
                )

        self.assertEqual(caught.exception.code, "write_failed")
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self._temp_files(), [])
        self.assertEqual([item.name for item in path.parent.iterdir() if item.is_file()], ["atomic.ogg"])

    def test_failed_replace_does_not_leave_new_file(self):
        with mock.patch.object(media_store.os, "replace", side_effect=OSError("нет места")):
            with self.assertRaises(media_store.MediaError) as caught:
                media_store.save_bytes(
                    self.media_root, "brand-new", PDF_DOCUMENT, now=FIXED_NOW
                )
        self.assertEqual(caught.exception.code, "write_failed")
        self.assertEqual(self._files(), [])
        self.assertEqual(self._temp_files(), [])

    def test_symlinked_year_directory_cannot_escape_media_root(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.media_root.mkdir(parents=True)
        try:
            (self.media_root / "2026").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("симлинки недоступны в этой файловой системе")

        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_bytes(self.media_root, "escape", OGG_VOICE, now=FIXED_NOW)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])


class ValidationTestCase(_TempDirTestCase):
    def test_bad_media_ids_are_rejected(self):
        bad_ids = ["", "   ", "../evil", "a/b", "a\\b", ".hidden", "..", "x" * 121, None, 123]
        for media_id in bad_ids:
            with self.subTest(media_id=media_id):
                with self.assertRaises(media_store.MediaError) as caught:
                    media_store.save_bytes(self.media_root, media_id, OGG_VOICE, now=FIXED_NOW)
                self.assertEqual(caught.exception.code, "bad_media_id")
        self.assertEqual(self._files(), [])

    def test_unknown_kind_hint_is_rejected(self):
        with self.assertRaises(media_store.MediaError) as caught:
            media_store.save_bytes(
                self.media_root, "id-1", OGG_VOICE, now=FIXED_NOW, kind_hint="hologram"
            )
        self.assertEqual(caught.exception.code, "bad_kind")

    def test_extension_sanitizer(self):
        self.assertEqual(media_store.sanitize_extension(".OGG"), "ogg")
        self.assertEqual(media_store.sanitize_extension("../../sh"), "bin")
        self.assertEqual(media_store.sanitize_extension(""), "bin")
        self.assertEqual(media_store.fallback_extension("file.tar.gz"), "gz")
        self.assertEqual(media_store.fallback_extension("no-extension"), "bin")
        self.assertEqual(media_store.fallback_extension("evil.<script>"), "bin")


class _FakeSegment:
    def __init__(self, text, end, start=None):
        self.text = text
        self.end = end
        if start is not None:
            self.start = start


class _FakeInfo:
    def __init__(self, language="ru", duration=1.5):
        self.language = language
        self.duration = duration


class _FakeModel:
    def __init__(self, segments=None, info=None, error=None):
        self.segments = (
            segments
            if segments is not None
            else [_FakeSegment(" привет", 0.8), _FakeSegment(" мир.", 1.5)]
        )
        self.info = info if info is not None else _FakeInfo()
        self.error = error
        self.calls = []

    def transcribe(self, path, language=None, vad_filter=True):
        self.calls.append({"path": path, "language": language, "vad_filter": vad_filter})
        if self.error is not None:
            raise self.error
        return iter(self.segments), self.info


def _ready_probe():
    return {
        "ffmpeg": "/usr/bin/ffmpeg",
        "ffprobe": None,
        "whisper": "faster_whisper",
        "model": None,
        "missing": [],
        "ready": True,
    }


def _missing_probe(*missing):
    names = list(missing) or ["ffmpeg", "faster-whisper"]
    return {
        "ffmpeg": None,
        "ffprobe": None,
        "whisper": None,
        "model": None,
        "missing": names,
        "ready": False,
    }


class TranscribeTestCase(_TempDirTestCase):
    def _audio(self, payload=OGG_VOICE):
        path = self.tmp / "voice.ogg"
        path.write_bytes(payload)
        return path

    def test_result_has_required_interface_keys(self):
        result = transcribe.transcribe(self._audio())
        for key in ("ok", "text", "lang", "duration", "reason", "status", "code"):
            self.assertIn(key, result)
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["text"], str)
        if not result["ok"]:
            self.assertIn(result["status"], ("pending_toolchain", "error"))
            self.assertTrue(result["reason"])

    def test_null_transcriber_reports_pending_toolchain(self):
        worker = transcribe.NullTranscriber(probe=lambda: _missing_probe("ffmpeg", "faster-whisper"))
        self.assertFalse(worker.available())
        result = worker.transcribe(self._audio())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], transcribe.STATUS_PENDING_TOOLCHAIN)
        self.assertEqual(result["code"], transcribe.STATUS_PENDING_TOOLCHAIN)
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["lang"])
        self.assertIsNone(result["duration"])
        self.assertEqual(result["segments"], [])
        self.assertIn("ffmpeg", result["reason"])
        self.assertIn("whisper", result["reason"])
        self.assertIn("сохранён", result["reason"])
        self.assertIn("ffmpeg", result["missing"])

    def test_pending_reason_names_only_missing_component(self):
        whisper_missing = transcribe.NullTranscriber(
            probe=lambda: _missing_probe("faster-whisper")
        ).transcribe(self._audio())
        self.assertIn("whisper", whisper_missing["reason"])
        self.assertNotIn("ffmpeg", whisper_missing["reason"])

        ffmpeg_missing = transcribe.NullTranscriber(
            probe=lambda: _missing_probe("ffmpeg")
        ).transcribe(self._audio())
        self.assertIn("ffmpeg", ffmpeg_missing["reason"])

    def test_missing_and_empty_files_are_reported_without_raising(self):
        missing = transcribe.transcribe(self.tmp / "nope.ogg")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "file_not_found")

        empty = self.tmp / "empty.ogg"
        empty.write_bytes(b"")
        result = transcribe.transcribe(empty)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "empty_file")

    def test_whisper_success_with_fake_model(self):
        fake = _FakeModel(
            segments=[
                _FakeSegment(" привет", 0.8, 0.0),
                _FakeSegment(" мир.", 1.5, 0.8),
            ]
        )
        worker = transcribe.WhisperTranscriber(
            probe=_ready_probe, model_factory=lambda: fake
        )
        self.assertTrue(worker.available())
        result = worker.transcribe(self._audio())
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], transcribe.STATUS_OK)
        self.assertEqual(result["text"], "привет мир.")
        self.assertEqual(result["lang"], "ru")
        self.assertEqual(result["duration"], 1.5)
        self.assertEqual(result["reason"], "")
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][0]["text"], "привет")
        self.assertEqual(result["segments"][1]["start"], 0.8)
        self.assertEqual(fake.calls[0]["language"], "ru")
        self.assertTrue(fake.calls[0]["vad_filter"])

    def test_whisper_duration_falls_back_to_segments(self):
        fake = _FakeModel(
            segments=[_FakeSegment(" раз", 2.0), _FakeSegment(" два", 3.5)],
            info=_FakeInfo(language="ru", duration=None),
        )
        worker = transcribe.WhisperTranscriber(
            probe=_ready_probe, model_factory=lambda: fake
        )
        result = worker.transcribe(self._audio())
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration"], 3.5)

    def test_whisper_failure_is_reported_not_raised(self):
        def broken_factory():
            raise RuntimeError("модель не найдена локально")

        worker = transcribe.WhisperTranscriber(
            probe=_ready_probe, model_factory=broken_factory
        )
        result = worker.transcribe(self._audio())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], transcribe.STATUS_ERROR)
        self.assertEqual(result["code"], "transcribe_failed")
        self.assertIn("RuntimeError", result["reason"])
        self.assertIn("Файл сохранён", result["reason"])

    def test_whisper_without_toolchain_returns_pending(self):
        worker = transcribe.WhisperTranscriber(
            probe=lambda: _missing_probe("ffmpeg"), model_factory=lambda: _FakeModel()
        )
        result = worker.transcribe(self._audio())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], transcribe.STATUS_PENDING_TOOLCHAIN)

    def test_empty_transcript_is_reported(self):
        fake = _FakeModel(segments=[_FakeSegment("   ", 1.0)])
        worker = transcribe.WhisperTranscriber(
            probe=_ready_probe, model_factory=lambda: fake
        )
        result = worker.transcribe(self._audio())
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "empty_transcript")

    def test_probe_toolchain_shape_and_probe_duration(self):
        info = transcribe.probe_toolchain(env={})
        for key in ("ffmpeg", "ffprobe", "whisper", "model", "missing", "ready"):
            self.assertIn(key, info)
        self.assertIsInstance(info["missing"], list)
        self.assertIsInstance(info["ready"], bool)
        self.assertIsNone(transcribe.probe_duration(self._audio(), ffprobe="/nonexistent/ffprobe"))


if __name__ == "__main__":
    unittest.main()
