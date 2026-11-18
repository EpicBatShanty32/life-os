"""Адаптер транскрибации Life OS v3: whisper, если есть; иначе честный pending.

Модуль не ходит в сеть и не вызывает LLM. Если в системе нет ``ffmpeg``
и/или ``faster-whisper`` (``whisper``), возвращается словарь со статусом
``pending_toolchain`` и понятным человеку объяснением, чего не хватает.
Пайплайн при этом не падает: файл остаётся в хранилище, расшифровка
появится после установки инструментария.

Интерфейс::

    transcribe(path) -> {
        "ok": bool,            # удалось ли получить текст
        "text": str,           # распознанный текст (пусто, если нет)
        "lang": str | None,    # язык, определённый whisper
        "duration": float | None,  # длительность, секунды
        "reason": str,         # объяснение для человека (пусто при успехе)
        "status": "ok" | "pending_toolchain" | "error",
        "code": str,           # машинный код: ok/pending_toolchain/file_not_found/...
    }

Whisper загружается лениво и только с ``local_files_only=True``: адаптер
никогда не скачивает модель из интернета. Модель должна быть уже скачана
заранее; как именно — описано в reason при ошибке.

Совместимость с Python 3.9 (stdlib), без сети.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

__all__ = [
    "DEFAULT_LANGUAGE",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_PENDING_TOOLCHAIN",
    "NullTranscriber",
    "Transcriber",
    "WhisperTranscriber",
    "find_binary",
    "find_whisper",
    "get_transcriber",
    "probe_duration",
    "probe_toolchain",
    "resolve_engine",
    "toolchain_status",
    "transcribe",
    "DisabledTranscriber",
    "ENGINES",
    "DEFAULT_ENGINE",
]

STATUS_OK = "ok"
STATUS_PENDING_TOOLCHAIN = "pending_toolchain"
STATUS_ERROR = "error"

#: Код результата, когда движок расшифровки выключен на этом устройстве.
STATUS_DISABLED = "transcribe_disabled"

#: Движки: ``off`` — не запускаем на этой машине, ``local`` — whisper здесь же.
ENGINES = ("off", "local")
DEFAULT_ENGINE = "off"

DEFAULT_LANGUAGE = "ru"

PathLike = Union[str, os.PathLike]


class Transcriber:
    """Базовый интерфейс транскрибатора (реализации: whisper и null)."""

    name = "base"

    def available(self) -> bool:
        """Готов ли транскрибатор работать прямо сейчас."""
        return False

    def transcribe(self, path: PathLike) -> Dict[str, Any]:
        """Возвращает результат в общем формате; не должен ронять вызывающий код."""
        raise NotImplementedError("нужен toolchain: ffmpeg и whisper")


class NullTranscriber(Transcriber):
    """Заглушка для среды без whisper/ffmpeg: статус ``pending_toolchain``.

    Не притворяется, что расшифровка есть: текст пустой, причина называет
    недостающие компоненты, а файл остаётся сохранённым для будущей обработки.
    """

    name = "null"

    def __init__(self, probe: Optional[Any] = None) -> None:
        self._probe = probe or probe_toolchain

    def available(self) -> bool:
        return False

    def transcribe(self, path: PathLike) -> Dict[str, Any]:
        invalid = _validate_path(path)
        if invalid is not None:
            return invalid
        info = self._probe()
        missing = list(info.get("missing") or [])
        return _result(
            False,
            STATUS_PENDING_TOOLCHAIN,
            STATUS_PENDING_TOOLCHAIN,
            reason=_pending_reason(missing),
            missing=missing,
            toolchain="none",
        )


class DisabledTranscriber(Transcriber):
    """Расшифровка выключена на этом устройстве (``transcribe.engine = off``).

    Ничего не запускает и не ищет: файл остаётся в хранилище, задача получает
    ``pending_toolchain`` с честной причиной «выключено», а не «чего-то не хватает».
    """

    name = "off"

    def __init__(self, engine: str = DEFAULT_ENGINE) -> None:
        self.engine = engine

    def available(self) -> bool:
        return False

    def transcribe(self, path: PathLike) -> Dict[str, Any]:
        invalid = _validate_path(path)
        if invalid is not None:
            return invalid
        return _result(
            False,
            STATUS_PENDING_TOOLCHAIN,
            STATUS_DISABLED,
            reason=_disabled_reason(self.engine),
            missing=[],
            toolchain="none",
            engine=self.engine,
        )


class WhisperTranscriber(Transcriber):
    """Транскрибация через faster-whisper (локальная модель, без сети).

    ``model_factory`` — точка внедрения для тестов: можно подсунуть фейковую
    модель и проверить разбор сегментов, не устанавливая whisper.
    """

    name = "whisper"

    def __init__(
        self,
        model_name: Optional[str] = None,
        model_path: Optional[PathLike] = None,
        device: str = "cpu",
        compute_type: str = "int8",
        language: Optional[str] = DEFAULT_LANGUAGE,
        probe: Optional[Any] = None,
        model_factory: Optional[Any] = None,
    ) -> None:
        self.model_name = model_name or "small"
        self.model_path = str(Path(model_path).expanduser()) if model_path else None
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._probe = probe or probe_toolchain
        self._model_factory = model_factory
        self._model = None

    def available(self) -> bool:
        try:
            return bool(self._probe().get("ready"))
        except Exception:
            return False

    def _model_source(self) -> str:
        return self.model_path or self.model_name

    def _load_model(self) -> Any:
        if self._model is None:
            if self._model_factory is not None:
                self._model = self._model_factory()
            else:
                # Импорт внутри метода: без whisper модуль импортируется и
                # отдаёт pending_toolchain, не требуя тяжёлых зависимостей.
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self._model_source(),
                    device=self.device,
                    compute_type=self.compute_type,
                    local_files_only=True,
                )
        return self._model

    def transcribe(self, path: PathLike) -> Dict[str, Any]:
        invalid = _validate_path(path)
        if invalid is not None:
            return invalid

        info = self._probe()
        if not info.get("ready"):
            return NullTranscriber(probe=self._probe).transcribe(path)

        try:
            model = self._load_model()
            segments, meta = model.transcribe(
                str(Path(path).expanduser()),
                language=self.language or None,
                vad_filter=True,
            )
            parts = []
            timed_segments = []
            last_end = 0.0
            for segment in segments:
                text = (getattr(segment, "text", "") or "").strip()
                start = getattr(segment, "start", None)
                end = getattr(segment, "end", None)
                if isinstance(end, (int, float)):
                    last_end = max(last_end, float(end))
                    if isinstance(start, (int, float)):
                        timed_segments.append(
                            {
                                "start": round(float(start), 3),
                                "end": round(float(end), 3),
                                "text": text,
                            }
                        )
                if text:
                    parts.append(text)

            language = getattr(meta, "language", None) or self.language or None
            duration = getattr(meta, "duration", None)
            if duration is None and last_end > 0:
                duration = last_end
            if duration is None and info.get("ffprobe"):
                duration = probe_duration(path, ffprobe=info.get("ffprobe"))

            text = " ".join(parts).strip()
            if not text:
                return _result(
                    False,
                    STATUS_ERROR,
                    "empty_transcript",
                    lang=language,
                    duration=_as_float(duration),
                    reason=(
                        "Whisper не распознал речь в файле: текста нет. Возможно, "
                        "запись тихая или там только музыка."
                    ),
                    segments=timed_segments,
                )
            return _result(
                True,
                STATUS_OK,
                STATUS_OK,
                text=text,
                lang=language,
                duration=_as_float(duration),
                segments=timed_segments,
            )
        except Exception as exc:
            return _result(
                False,
                STATUS_ERROR,
                "transcribe_failed",
                reason=_failure_reason(exc),
            )


def probe_toolchain(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Проверяет локальный инструментарий без запуска процессов и сети."""
    environment = os.environ if env is None else env
    ffmpeg = find_binary("ffmpeg", "LIFEOS_FFMPEG", environment)
    ffprobe = find_binary("ffprobe", "LIFEOS_FFPROBE", environment)
    whisper = find_whisper()

    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not whisper:
        missing.append("faster-whisper")

    raw_model = environment.get("LIFEOS_WHISPER_MODEL")
    model = str(raw_model).strip() if raw_model else ""

    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "whisper": whisper,
        "model": model or None,
        "missing": missing,
        "ready": not missing,
    }


def toolchain_status(
    env: Optional[Mapping[str, str]] = None,
    engine: Optional[str] = None,
) -> Dict[str, Any]:
    """Статус для UI/API: включён ли движок и что именно отсутствует.

    Ключи ``engine`` (``off``/``local``) и ``enabled`` (запускаем ли на этой
    машине) добавлены, чтобы бот не врал про «не хватает ffmpeg», когда
    расшифровка просто выключена.
    """
    engine_name = resolve_engine(engine, env)
    info = dict(probe_toolchain(env=env))
    info["engine"] = engine_name
    info["enabled"] = engine_name == "local"
    if not info["enabled"]:
        info["ready"] = False
        info["status"] = STATUS_PENDING_TOOLCHAIN
        info["reason"] = _disabled_reason(engine_name)
        # «Не хватает ffmpeg» здесь было бы неправдой: его никто не просил.
        # Сами пути (ffmpeg/whisper/model) остаются в словаре для диагностики.
        info["missing"] = []
        return info
    info["status"] = STATUS_OK if info["ready"] else STATUS_PENDING_TOOLCHAIN
    info["reason"] = "" if info["ready"] else _pending_reason(info["missing"])
    return info


def find_binary(
    name: str,
    env_key: str,
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Ищет исполняемый файл: env-переменная, PATH, типовые каталоги Homebrew."""
    env = os.environ if environment is None else environment
    explicit = env.get(env_key)
    if explicit:
        candidate = Path(str(explicit)).expanduser()
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
        return None

    located = shutil.which(name)
    if located:
        return located
    for directory in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def find_whisper() -> Optional[str]:
    """Имя установленного модуля whisper или ``None`` (без импорта тяжёлых зависимостей)."""
    for module in ("faster_whisper", "whisper"):
        try:
            if importlib.util.find_spec(module) is not None:
                return module
        except (ImportError, AttributeError, ValueError):
            continue
    return None


def probe_duration(
    path: PathLike,
    ffprobe: Optional[str] = None,
    timeout: float = 15.0,
) -> Optional[float]:
    """Длительность через локальный ffprobe; ``None``, если ffprobe нет или он не смог."""
    executable = ffprobe
    if not executable:
        executable = find_binary("ffprobe", "LIFEOS_FFPROBE", os.environ)
    if not executable:
        return None

    source = Path(path).expanduser()
    if not source.is_file():
        return None
    command = [
        str(executable),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        raw = completed.stdout.decode("utf-8", "replace").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    return _as_float(raw)


def resolve_engine(
    engine: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Имя движка: аргумент, затем ``LIFEOS_TRANSCRIBE_ENGINE``, затем ``off``.

    Неизвестное значение трактуется как ``off``: расшифровка не запускается
    сама собой из-за опечатки в конфиге.
    """
    raw: Optional[str] = engine
    if raw is None:
        environment = os.environ if env is None else env
        raw = environment.get("LIFEOS_TRANSCRIBE_ENGINE")
    value = str(raw or "").strip().lower()
    return value if value in ENGINES else DEFAULT_ENGINE


def get_transcriber(
    probe: Optional[Any] = None,
    engine: Optional[str] = None,
) -> Transcriber:
    """Выбирает рабочую реализацию по движку из настроек.

    ``off`` (по умолчанию) — :class:`DisabledTranscriber`: на этой машине ничего
    не считается и не устанавливается. ``local`` — whisper при полном
    toolchain, иначе null с честным ``pending_toolchain``.
    """
    engine_name = resolve_engine(engine)
    if engine_name != "local":
        return DisabledTranscriber(engine=engine_name)

    probe_fn = probe or probe_toolchain
    info = probe_fn()
    if not info.get("ready"):
        return NullTranscriber(probe=probe_fn)

    model = info.get("model")
    if model and Path(str(model)).expanduser().exists():
        return WhisperTranscriber(model_path=model, probe=probe_fn)
    return WhisperTranscriber(model_name=model or None, probe=probe_fn)


def transcribe(
    path: PathLike,
    transcriber: Optional[Transcriber] = None,
    probe: Optional[Any] = None,
    engine: Optional[str] = None,
) -> Dict[str, Any]:
    """Транскрибирует файл и никогда не бросает исключение наружу."""
    try:
        worker = (
            transcriber
            if transcriber is not None
            else get_transcriber(probe=probe, engine=engine)
        )
        return worker.transcribe(path)
    except Exception as exc:
        return _result(
            False,
            STATUS_ERROR,
            "transcribe_failed",
            reason=(
                "Внутренняя ошибка транскрибации (%s): %s."
                % (type(exc).__name__, (str(exc) or "без описания")[:200])
            ),
        )


def _validate_path(path: PathLike) -> Optional[Dict[str, Any]]:
    """Проверяет входной файл; возвращает готовую ошибку или ``None``."""
    if path is None:
        return _result(False, STATUS_ERROR, "file_not_found", reason="Файл не передан.")
    try:
        source = Path(path).expanduser()
    except TypeError:
        return _result(
            False,
            STATUS_ERROR,
            "file_not_found",
            reason="Неверный путь к файлу.",
        )
    try:
        if not source.exists():
            return _result(
                False,
                STATUS_ERROR,
                "file_not_found",
                reason="Файл не найден: %s." % source.name,
            )
        if not source.is_file():
            return _result(
                False,
                STATUS_ERROR,
                "not_a_file",
                reason="Путь ведёт не на файл: %s." % source.name,
            )
        if source.stat().st_size <= 0:
            return _result(
                False,
                STATUS_ERROR,
                "empty_file",
                reason="Файл пустой — расшифровывать нечего.",
            )
    except OSError:
        return _result(
            False,
            STATUS_ERROR,
            "file_unreadable",
            reason="Файл недоступен для чтения: %s." % source.name,
        )
    return None


def _result(
    ok: bool,
    status: str,
    code: str,
    text: str = "",
    lang: Optional[str] = None,
    duration: Optional[float] = None,
    reason: str = "",
    **extra: Any
) -> Dict[str, Any]:
    """Единый словарь результата с обязательными ключами интерфейса."""
    result = {
        "ok": bool(ok),
        "status": status,
        "code": code,
        "text": text,
        "lang": lang,
        "duration": duration,
        "reason": reason,
        "segments": extra.pop("segments", []),
    }
    result.update(extra)
    return result


def _disabled_reason(engine: str = DEFAULT_ENGINE) -> str:
    return (
        "Расшифровка голоса выключена на этом устройстве (transcribe.engine = %s): "
        "текст не создаётся, файл остаётся в хранилище. Задачи с расшифровкой "
        "ждут, пока движок не включат (transcribe.engine = local)." % engine
    )


def _pending_reason(missing: Optional[List[str]]) -> str:
    names = set(missing or [])
    if "ffmpeg" in names and any("whisper" in item for item in names):
        return (
            "Транскрибация пока недоступна: в системе нет ffmpeg и whisper. "
            "Установите ffmpeg (brew install ffmpeg) и faster-whisper "
            "(python3 -m pip install faster-whisper) — файл сохранён, "
            "расшифровка появится после установки."
        )
    if "ffmpeg" in names:
        return (
            "Транскрибация пока недоступна: не хватает ffmpeg. "
            "Установите: brew install ffmpeg — файл сохранён, расшифровка "
            "появится после установки."
        )
    if names:
        return (
            "Транскрибация пока недоступна: не хватает whisper (faster-whisper). "
            "Установите: python3 -m pip install faster-whisper — файл сохранён, "
            "расшифровка появится после установки."
        )
    return (
        "Транскрибация недоступна: не найден инструментарий (ffmpeg/whisper). "
        "Файл сохранён, расшифровка появится после установки."
    )


def _failure_reason(exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")[:200]
    return (
        "Не удалось расшифровать аудио (%s): %s. Файл сохранён; проверьте, "
        "что модель whisper скачана локально и ffmpeg доступен."
        % (type(exc).__name__, message or "без описания")
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return round(number, 3)
