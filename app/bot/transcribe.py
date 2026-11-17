"""Совместимость: адаптер транскрибации живёт в :mod:`app.server.transcribe`.

Исторически файл был создан как ``app/bot/transcribe.py`` (изолированная
подготовка бота), затем перенесён в серверный пакет согласно MASTER_SPEC §1.
Этот модуль оставлен тонким реэкспортом, чтобы старые импорты
``app.bot.transcribe`` и тесты продолжали работать. Новый код должен
импортировать :mod:`app.server.transcribe`.
"""

from app.server.transcribe import *  # noqa: F401,F403
from app.server.transcribe import __all__ as _ALL

__all__ = list(_ALL)
