# Life OS v3 — фаза 2: Telegram-бот и медиапайплайн

- **Статус:** готово (кроме живого запуска — нет токена)
- **Дата:** 2026-11-17
- **Коммит волны:** c45bb2a / `{{commit}}`
- **Требование:** MASTER_SPEC §2.3, волна `t8` на доске

## Что сделано

| Файл | Строк | Содержание |
|---|---:|---|
| `app/bot/handlers.py` | 687 | `BotService`, whitelist по `ADMIN_TELEGRAM_ID` (пустой id = никому нельзя), команды `/start` `/help` `/stats`, свободный текст → интент → запись, ответы с инлайн-кнопками |
| `app/bot/intent.py` | 602 | `OpenAICompatibleGateway` (таймаут, `User-Agent`, 429/5xx), `ScriptedFallback` без LLM, `LLMRouter`, `parse_text`, строгая валидация интента |
| `app/bot/media.py` | 285 | тип файла по сигнатуре (не по расширению), лимиты 20 МБ (Bot API) и 2 ГБ (локальный сервер), безопасный id |
| `app/bot/media_store.py` | 668 | раскладка `media/YYYY/MM/<id>.<ext>`, атомарная запись через `os.replace`, хэш, откат при сбое |
| `app/worker/main.py` | 613 | очередь `jobs` в SQLite: `claim_next`, прогресс, backoff 2→60 с, `run_once`/`worker_loop`, обработчик транскрибации |
| `app/server/transcribe.py` | 495 | интерфейс `Transcriber`: `WhisperTranscriber` и `NullTranscriber` (`pending_toolchain`), зонды `ffmpeg`/`ffprobe`/`whisper`, длительность, сегменты с таймкодами |

## Чем проверено

```text
python3 -m pytest app/tests/test_bot_intent.py app/tests/test_bot_media.py -q
50 passed (14 + 36, включая 15 subtests)
```

Проверено без сети и без Telegram: фейковый aiogram-апдейт, определение формата по сигнатуре (OGG/MP3/MP4/M4A/PDF/ZIP), лимиты, атомарность записи, честный статус `pending_toolchain`, разбор результата whisper на фейковой модели, backoff воркера.

## Чего нет и почему

- **Живой бот не запускался:** нет токена @BotFather и `chat_id` владельца. Код готов, `python3 -m app.bot.main --dry-run` проходит.
- **Транскрибация реальным whisper не проверена:** в среде нет `ffmpeg`/`faster-whisper`. Вместо падения пайплайн пишет `pending_toolchain` и показывает это в UI.
- `app/bot/transcribe.py` (13 строк) — реэкспорт серверного адаптера, чтобы у бота не было второго источника правды.

## Что осталось

Только внешнее: токен + chat_id → `bash install.sh --reload` и один живой smoke (текст, фото, голос).
