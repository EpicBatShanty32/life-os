"""Life OS — персональный Telegram-агент.

Пакет `src` с модулями агента. Запуск: ``python3 -m src.main`` или
``python3 src/main.py`` из каталога проекта (project_root).

Модули импортируются как ``from src import config`` — так работают и
`python3 -m src.main`, и `python3 src/main.py` (main.py добавляет project_root
в sys.path), и тесты из project_root.
"""

__version__ = "0.1.0"
