"""Корпус живых фраз для приёмки Telegram-бота Life OS v4.

Зачем: бот — главный способ ввода (``studio/BOT-MAIN.md``). Этот корпус
фиксирует, как пишет реальный человек с телефона, и что бот обязан с этим
сделать. Автотест :mod:`app.tests.test_bot_acceptance` прогоняет каждую фразу
через ``app.bot.intent`` (только правила, без сети) и сверяет **смысл**, а не
строку: тип записи, ключевые поля, дату.

Формат одной фразы::

    {
        "id": "task-bank-tomorrow",       # стабильный id, латиницей
        "group": "tasks",                 # для отчёта по группам
        "phrase": "завтра позвонить в банк",
        "expect": {
            "action": "create_task",      # см. ACTIONS ниже
            "title_any": ["банк"],        # любая из подстрок в заголовке/тексте
            "due": ["tomorrow"],          # допустимые даты (символ. или ISO)
        },
        "may_ask": False,                 # True — честный вопрос допустим
        "note": "человеческое пояснение для QA-отчёта",
    }

Правила сверки:

* ``action`` — ожидаемый смысл; ``ask_clarify`` принимается как честный
  вопрос уточнения, если у фразы ``may_ask=True`` (или если запись
  неоднозначна по своей природе). Молчаливое «не понял» и потеря сообщения
  запрещены всегда.
* ``title_any`` / ``text_any`` / ``category_any`` / ``items_any`` /
  ``goal_any`` / ``book_any`` / ``habit_any`` / ``account_any`` — списки
  подстрок, достаточно совпадения любой (регистр не важен).
* числа (``amount``, ``weight``, ``fat``, ``water_ml``, ``sleep_h``,
  ``calories``, ``protein``, ``energy``, ``mood``, ``pages``,
  ``duration_min``, ``delta_pct``) сверяются с допуском.
* ``due`` — символические даты: ``today``, ``tomorrow``,
  ``day_after_tomorrow``, ``yesterday``, ``next_monday`` … ``next_sunday``,
  ``day_15``, ``day_20``; можно ISO ``YYYY-MM-DD``. Для «в пятницу» и
  «15 числа» допускается несколько вариантов — см. ``resolve_dates`` в тесте.
* ``time`` — ``"15:00"`` и т.п.

``KNOWN_GAPS`` — честный список того, чего текущий ``app.bot.intent`` ещё не
умеет (бот v4 в разработке, t23). Для этих id тест помечается ``xfail`` с
причиной, а не падает. Как только фича появляется, тест начинает проходить —
тогда id убирают из списка (об этом напоминает отдельный тест).

Корпус не правит рабочий код бота и не требует сети.
"""

from __future__ import annotations

#: Допустимые смыслы (действия) бота v4.
ACTIONS = frozenset(
    {
        "create_task",
        "complete_task",
        "reschedule_task",
        "update_last",
        "create_transaction",
        "create_book_progress",
        "create_book",
        "create_quote",
        "create_insight",
        "create_goal",
        "goal_progress",
        "checkin_energy",
        "checkin_mood",
        "mark_habit",
        "log_food",
        "log_sleep",
        "log_water",
        "log_weight",
        "log_workout",
        "query",
        "review",
        "delete_last",
        "inbox",
        "note",
        "ask_clarify",
    }
)

#: Группы для сводки QA.
GROUPS = (
    "tasks",
    "money",
    "library",
    "food",
    "body",
    "habits",
    "goals",
    "queries",
    "one_word",
    "messy",
    "long",
)

PHRASES = [
    # ------------------------------------------------------------------
    # Дела: даты, время, повторы, закрытие, перенос
    # ------------------------------------------------------------------
    {
        "id": "task-bank-tomorrow",
        "group": "tasks",
        "phrase": "завтра позвонить в банк",
        "expect": {"action": "create_task", "title_any": ["банк"], "due": ["tomorrow"]},
        "note": "строчные, без запятых",
    },
    {
        "id": "task-dentist-friday-15",
        "group": "tasks",
        "phrase": "в пятницу стоматолог в 15",
        "expect": {
            "action": "create_task",
            "title_any": ["стоматолог"],
            "due": ["next_friday"],
            "time": "15:00",
        },
        "note": "дата словом + время числом",
    },
    {
        "id": "task-report-day-after",
        "group": "tasks",
        "phrase": "послезавтра сдать отчёт",
        "expect": {
            "action": "create_task",
            "title_any": ["отчёт", "отчет"],
            "due": ["day_after_tomorrow"],
        },
    },
    {
        "id": "task-planning-monday-10",
        "group": "tasks",
        "phrase": "в понедельник планёрка в 10",
        "expect": {
            "action": "create_task",
            "title_any": ["планёрка", "планерка"],
            "due": ["next_monday"],
            "time": "10:00",
        },
    },
    {
        "id": "task-internet-15th",
        "group": "tasks",
        "phrase": "15 числа оплатить интернет",
        "expect": {
            "action": "create_task",
            "title_any": ["интернет"],
            "due": ["day_15"],
        },
    },
    {
        "id": "task-training-tomorrow-9",
        "group": "tasks",
        "phrase": "завтра в 9 утра тренировка",
        "expect": {
            "action": "create_task",
            "title_any": ["тренировка"],
            "due": ["tomorrow"],
            "time": "09:00",
        },
    },
    {
        "id": "task-mom-birthday",
        "group": "tasks",
        "phrase": "напомни завтра про день рождения мамы",
        "expect": {
            "action": "create_task",
            "title_any": ["день рождения", "мамы", "мама"],
            "due": ["tomorrow"],
        },
    },
    {
        "id": "task-bread-milk",
        "group": "tasks",
        "phrase": "купить хлеб и молоко",
        "expect": {"action": "create_task", "title_any": ["хлеб", "молоко"]},
        "may_ask": True,
        "note": "без даты — допустим вопрос «на когда?»",
    },
    {
        "id": "task-call-anya-wednesday",
        "group": "tasks",
        "phrase": "в среду созвон с Аней в 11:30",
        "expect": {
            "action": "create_task",
            "title_any": ["созвон", "аня"],
            "due": ["next_wednesday"],
            "time": "11:30",
        },
    },
    {
        "id": "task-presentation-week",
        "group": "tasks",
        "phrase": "до конца недели доделать презентацию",
        "expect": {"action": "create_task", "title_any": ["презентац"]},
        "note": "дедлайн «конец недели» — проверяем тип, дату допускаем любую",
    },
    {
        "id": "task-bank-call-late-order",
        "group": "tasks",
        "phrase": "позвонить в банк завтра",
        "expect": {
            "action": "create_task",
            "title_any": ["банк"],
            "due": ["tomorrow"],
        },
        "note": "обратный порядок слов",
    },
    {
        "id": "task-dentist-15-1430",
        "group": "tasks",
        "phrase": "стоматолог 15 числа в 14:30",
        "expect": {
            "action": "create_task",
            "title_any": ["стоматолог"],
            "due": ["day_15"],
            "time": "14:30",
        },
    },
    {
        "id": "task-report-next-tuesday",
        "group": "tasks",
        "phrase": "в следующий вторник отчёт",
        "expect": {
            "action": "create_task",
            "title_any": ["отчёт", "отчет"],
            "due": ["next_tuesday"],
        },
    },
    {
        "id": "task-documents-20th",
        "group": "tasks",
        "phrase": "до 20 числа сдать документы",
        "expect": {
            "action": "create_task",
            "title_any": ["документ"],
            "due": ["day_20"],
        },
    },
    {
        "id": "task-mail-check",
        "group": "tasks",
        "phrase": "задача: проверить почту",
        "expect": {"action": "create_task", "title_any": ["почт"]},
        "may_ask": True,
    },
    {
        "id": "task-tomorrow-remind-call",
        "group": "tasks",
        "phrase": "завтра напомнить про созвон в 10",
        "expect": {
            "action": "create_task",
            "title_any": ["созвон"],
            "due": ["tomorrow"],
            "time": "10:00",
        },
    },
    {
        "id": "task-gym-thursday",
        "group": "tasks",
        "phrase": "в четверг зал в 19",
        "expect": {
            "action": "create_task",
            "title_any": ["зал", "тренировк"],
            "due": ["next_thursday"],
            "time": "19:00",
        },
        "may_ask": True,
        "note": "«зал» может быть и планом, и отметкой тренировки",
    },
    {
        "id": "task-done",
        "group": "tasks",
        "phrase": "готово",
        "expect": {"action": "complete_task"},
        "note": "закрывает последнюю подходящую задачу",
    },
    {
        "id": "task-did",
        "group": "tasks",
        "phrase": "сделал",
        "expect": {"action": "complete_task"},
    },
    {
        "id": "task-close-call",
        "group": "tasks",
        "phrase": "закрыть созвон",
        "expect": {"action": "complete_task", "title_any": ["созвон"]},
        "may_ask": True,
        "note": "если задача не найдена — честный вопрос",
    },
    {
        "id": "task-reschedule-tomorrow",
        "group": "tasks",
        "phrase": "перенеси на завтра",
        "expect": {"action": "reschedule_task", "due": ["tomorrow"]},
    },
    {
        "id": "task-to-work",
        "group": "tasks",
        "phrase": "это в работу",
        "expect": {"action": "update_last"},
    },
    {
        "id": "task-cancel-last",
        "group": "tasks",
        "phrase": "удали последнее",
        "expect": {"action": "delete_last"},
    },
    {
        "id": "task-cancel-last-short",
        "group": "tasks",
        "phrase": "отмени последнее",
        "expect": {"action": "delete_last"},
    },
    # ------------------------------------------------------------------
    # Деньги: суммы, словами, счета, сводки
    # ------------------------------------------------------------------
    {
        "id": "money-taxi-450",
        "group": "money",
        "phrase": "такси 450",
        "expect": {
            "action": "create_transaction",
            "amount": 450,
            "kind": "expense",
            "category_any": ["такси", "транспорт"],
        },
    },
    {
        "id": "money-salary-120000",
        "group": "money",
        "phrase": "зарплата 120000",
        "expect": {
            "action": "create_transaction",
            "amount": 120000,
            "kind": "income",
            "category_any": ["зарплат", "доход"],
        },
    },
    {
        "id": "money-coffee-cash-320",
        "group": "money",
        "phrase": "кофе 320 наличными",
        "expect": {
            "action": "create_transaction",
            "amount": 320,
            "kind": "expense",
            "account_any": ["наличн"],
        },
    },
    {
        "id": "money-words-taxi-1200",
        "group": "money",
        "phrase": "потратил тысячу двести на такси",
        "expect": {
            "action": "create_transaction",
            "amount": 1200,
            "kind": "expense",
            "category_any": ["такси", "транспорт"],
        },
        "note": "числа словами",
    },
    {
        "id": "money-groceries-2500",
        "group": "money",
        "phrase": "купил продукты на 2500",
        "expect": {
            "action": "create_transaction",
            "amount": 2500,
            "kind": "expense",
            "category_any": ["продукт"],
        },
    },
    {
        "id": "money-lunch-600",
        "group": "money",
        "phrase": "обед 600",
        "expect": {"action": "create_transaction", "amount": 600, "kind": "expense"},
        "may_ask": True,
        "note": "может быть и еда, и деньги — вопрос уместен",
    },
    {
        "id": "money-project-50000",
        "group": "money",
        "phrase": "получил 50 тысяч за проект",
        "expect": {
            "action": "create_transaction",
            "amount": 50000,
            "kind": "income",
        },
    },
    {
        "id": "money-metro-200",
        "group": "money",
        "phrase": "потратил 200 на метро",
        "expect": {
            "action": "create_transaction",
            "amount": 200,
            "kind": "expense",
            "category_any": ["метро", "транспорт"],
        },
    },
    {
        "id": "money-pharmacy-870",
        "group": "money",
        "phrase": "аптека 870",
        "expect": {
            "action": "create_transaction",
            "amount": 870,
            "kind": "expense",
            "category_any": ["аптек", "здоров"],
        },
    },
    {
        "id": "money-rent-45000",
        "group": "money",
        "phrase": "заплатил за квартиру 45000",
        "expect": {
            "action": "create_transaction",
            "amount": 45000,
            "kind": "expense",
            "category_any": ["квартир", "жиль", "дом"],
        },
    },
    {
        "id": "money-coffee-card-320",
        "group": "money",
        "phrase": "кофе 320 картой",
        "expect": {
            "action": "create_transaction",
            "amount": 320,
            "account_any": ["карт"],
        },
    },
    {
        "id": "money-fuel-3000",
        "group": "money",
        "phrase": "бензин 3000",
        "expect": {
            "action": "create_transaction",
            "amount": 3000,
            "kind": "expense",
        },
    },
    {
        "id": "money-taxi-typo",
        "group": "messy",
        "phrase": "купил кафе 3 20",
        "expect": {
            "action": "create_transaction",
            "amount": 320,
            "kind": "expense",
        },
        "may_ask": True,
        "note": "голосовая опечатка: «кофе 320»",
    },
    {
        "id": "money-yesterday-lunch",
        "group": "money",
        "phrase": "вчера потратил 500 на обед",
        "expect": {
            "action": "create_transaction",
            "amount": 500,
            "kind": "expense",
            "date": ["yesterday"],
        },
        "may_ask": True,
        "note": "дата в прошлом: «вчера»",
    },
    # ------------------------------------------------------------------
    # Библиотека: книги, цитаты, мысли
    # ------------------------------------------------------------------
    {
        "id": "book-sapiens-120",
        "group": "library",
        "phrase": "читаю Sapiens стр 120",
        "expect": {
            "action": "create_book_progress",
            "book_any": ["sapiens", "сапиенс"],
            "pages": 120,
        },
    },
    {
        "id": "book-quote-constant",
        "group": "library",
        "phrase": "цитата: мы то, что мы делаем постоянно",
        "expect": {
            "action": "create_quote",
            "text_any": ["делаем постоянно"],
        },
    },
    {
        "id": "book-quote-great-things",
        "group": "library",
        "phrase": "цитата из книги: единственный способ делать великие дела — любить то, что делаешь",
        "expect": {
            "action": "create_quote",
            "text_any": ["великие дела", "любить"],
        },
    },
    {
        "id": "book-thought-sleep",
        "group": "library",
        "phrase": "мысль: сон важнее продуктивности",
        "expect": {
            "action": "create_insight",
            "accept_actions": ["note"],
            "text_any": ["сон", "продуктивност"],
        },
    },
    {
        "id": "book-thought-morning",
        "group": "library",
        "phrase": "мысль про утро: вставать в 6 и сразу писать",
        "expect": {
            "action": "create_insight",
            "accept_actions": ["note"],
            "text_any": ["вставать", "писать"],
        },
    },
    {
        "id": "book-sapiens-done-5",
        "group": "library",
        "phrase": "дочитал Sapiens, 5 из 5",
        "expect": {
            "action": "create_book_progress",
            "book_any": ["sapiens", "сапиенс"],
            "book_status": "done",
            "rating": 5,
        },
        "may_ask": True,
    },
    {
        "id": "book-atomic-habits-take",
        "group": "library",
        "phrase": "взял в работу Атомные привычки",
        "expect": {
            "action": "create_book",
            "book_any": ["атомные привычки"],
            "book_status": "reading",
        },
        "may_ask": True,
    },
    {
        "id": "book-pages-30",
        "group": "library",
        "phrase": "прочитал 30 страниц",
        "expect": {"action": "create_book_progress", "pages": 30},
        "may_ask": True,
        "note": "если текущей книги нет — честный вопрос",
    },
    {
        "id": "book-think-slow-buy",
        "group": "library",
        "phrase": "купил книгу Думай медленно решай быстро",
        "expect": {
            "action": "create_book",
            "book_any": ["думай медленно"],
            "book_status": "queue",
        },
        "may_ask": True,
    },
    {
        "id": "book-1984-dropped",
        "group": "library",
        "phrase": "отложил книгу 1984",
        "expect": {
            "action": "create_book_progress",
            "book_any": ["1984"],
            "book_status": "dropped",
        },
        "may_ask": True,
    },
    {
        "id": "book-idea-water",
        "group": "library",
        "phrase": "идея для проекта: бот напоминает про воду",
        "expect": {
            "action": "create_insight",
            "accept_actions": ["note"],
            "text_any": ["бот", "воду", "вода"],
        },
    },
    {
        "id": "book-insight-from-book",
        "group": "library",
        "phrase": "инсайт из книги: сон — основа продуктивности",
        "expect": {
            "action": "create_insight",
            "text_any": ["сон", "продуктивност"],
        },
    },
    {
        "id": "book-thought-from-sapiens",
        "group": "library",
        "phrase": "мысль из книги Sapiens: люди верят в общие истории",
        "expect": {
            "action": "create_insight",
            "text_any": ["общие истории", "верят"],
        },
    },
    {
        "id": "book-note-domain",
        "group": "library",
        "phrase": "заметка: не забыть продлить домен",
        "expect": {
            "action": "note",
            "accept_actions": ["create_task"],
            "title_any": ["домен"],
        },
    },
    # ------------------------------------------------------------------
    # Еда и вода
    # ------------------------------------------------------------------
    {
        "id": "food-buckwheat-chicken",
        "group": "food",
        "phrase": "на обед гречка 200 г и курица 150",
        "expect": {
            "action": "log_food",
            "items_any": ["гречка", "курица"],
            "grams_total": 350,
        },
    },
    {
        "id": "food-600kcal-protein",
        "group": "food",
        "phrase": "съел 600 ккал и 40 белка",
        "expect": {"action": "log_food", "calories": 600, "protein": 40},
    },
    {
        "id": "food-600kcal",
        "group": "food",
        "phrase": "съел 600 ккал",
        "expect": {"action": "log_food", "calories": 600},
    },
    {
        "id": "food-breakfast-oatmeal",
        "group": "food",
        "phrase": "завтрак овсянка 250 и кофе",
        "expect": {"action": "log_food", "items_any": ["овсянка", "кофе"]},
        "may_ask": True,
    },
    {
        "id": "food-banana-100",
        "group": "food",
        "phrase": "банан 100 г",
        "expect": {"action": "log_food", "items_any": ["банан"]},
        "may_ask": True,
    },
    {
        "id": "food-cottage-protein",
        "group": "food",
        "phrase": "творог 200 г и 18 белка",
        "expect": {
            "action": "log_food",
            "items_any": ["творог"],
            "protein": 18,
        },
    },
    {
        "id": "food-dinner-fish",
        "group": "food",
        "phrase": "ужин: рыба 150 и овощи",
        "expect": {"action": "log_food", "items_any": ["рыба", "овощи"]},
        "may_ask": True,
    },
    {
        "id": "food-snack-200",
        "group": "food",
        "phrase": "перекус 200 ккал",
        "expect": {"action": "log_food", "calories": 200},
    },
    {
        "id": "water-two-liters",
        "group": "food",
        "phrase": "выпил 2 л",
        "expect": {
            "action": "mark_habit",
            "habit_any": ["вод"],
            "water_ml": 2000,
        },
    },
    {
        "id": "water-300",
        "group": "food",
        "phrase": "вода 300",
        "expect": {"action": "log_water", "water_ml": 300},
    },
    {
        "id": "water-two-liters-words",
        "group": "food",
        "phrase": "выпил два литра воды",
        "expect": {
            "action": "mark_habit",
            "habit_any": ["вод"],
            "water_ml": 2000,
        },
        "note": "число словами",
    },
    # ------------------------------------------------------------------
    # Тело, сон, тренировки
    # ------------------------------------------------------------------
    {
        "id": "body-weight-fat",
        "group": "body",
        "phrase": "вес 81.2, жир 18.5",
        "expect": {"action": "log_weight", "weight": 81.2, "fat": 18.5},
    },
    {
        "id": "body-weigh-82",
        "group": "body",
        "phrase": "вешу 82",
        "expect": {"action": "log_weight", "weight": 82},
    },
    {
        "id": "body-fat-only",
        "group": "body",
        "phrase": "жир 18,5",
        "expect": {"action": "log_weight", "fat": 18.5},
    },
    {
        "id": "sleep-7-5",
        "group": "body",
        "phrase": "спал 7.5",
        "expect": {"action": "log_sleep", "sleep_h": 7.5},
    },
    {
        "id": "sleep-yesterday-6",
        "group": "body",
        "phrase": "вчера спал 6 часов",
        "expect": {"action": "log_sleep", "sleep_h": 6},
        "note": "«вчера» — дата сна в прошлом",
    },
    {
        "id": "sleep-seven-half-words",
        "group": "body",
        "phrase": "спал семь с половиной часов",
        "expect": {"action": "log_sleep", "sleep_h": 7.5},
        "note": "число словами",
    },
    {
        "id": "sleep-8-hours",
        "group": "body",
        "phrase": "сон 8 часов",
        "expect": {"action": "log_sleep", "sleep_h": 8},
    },
    {
        "id": "body-bench-80x5",
        "group": "body",
        "phrase": "жим 80х5",
        "expect": {"action": "log_workout", "load_kg": 80, "reps": 5},
    },
    {
        "id": "body-squat-100x3",
        "group": "body",
        "phrase": "присед 100 на 3",
        "expect": {"action": "log_workout", "load_kg": 100, "reps": 3},
    },
    {
        "id": "body-workout-45",
        "group": "body",
        "phrase": "тренировка 45 минут",
        "expect": {"action": "log_workout", "duration_min": 45},
    },
    {
        "id": "body-pushups-sets",
        "group": "body",
        "phrase": "отжался 3 подхода по 20",
        "expect": {"action": "log_workout", "reps": 20},
        "may_ask": True,
    },
    {
        "id": "body-run-5km",
        "group": "habits",
        "phrase": "пробежал 5 км",
        "expect": {"action": "mark_habit", "habit_any": ["бег", "пробеж", "спорт"]},
    },
    {
        "id": "energy-3",
        "group": "body",
        "phrase": "энергия 3",
        "expect": {"action": "checkin_energy", "energy": 3},
    },
    {
        "id": "energy-4-tired",
        "group": "body",
        "phrase": "энергия 4, устал после созвонов",
        "expect": {"action": "checkin_energy", "energy": 4},
    },
    {
        "id": "mood-7",
        "group": "body",
        "phrase": "настроение 7",
        "expect": {"action": "checkin_mood", "mood": 7},
    },
    {
        "id": "mood-8",
        "group": "body",
        "phrase": "настроение 8",
        "expect": {"action": "checkin_mood", "mood": 8},
    },
    # ------------------------------------------------------------------
    # Привычки
    # ------------------------------------------------------------------
    {
        "id": "habit-no-smoking",
        "group": "habits",
        "phrase": "не курил",
        "expect": {"action": "mark_habit", "habit_any": ["кур"]},
    },
    {
        "id": "habit-walk-30",
        "group": "habits",
        "phrase": "прогулка 30 минут",
        "expect": {"action": "mark_habit", "habit_any": ["прогул"]},
        "may_ask": True,
    },
    {
        "id": "habit-exercise",
        "group": "habits",
        "phrase": "сделал зарядку",
        "expect": {"action": "mark_habit", "habit_any": ["зарядк"]},
    },
    {
        "id": "habit-read-20",
        "group": "habits",
        "phrase": "читал 20 минут",
        "expect": {"action": "mark_habit", "habit_any": ["чтени", "чита"]},
        "may_ask": True,
    },
    {
        "id": "habit-no-sugar",
        "group": "habits",
        "phrase": "без сахара",
        "expect": {"action": "mark_habit", "habit_any": ["сахар"]},
    },
    {
        "id": "habit-mark-sport",
        "group": "habits",
        "phrase": "отметь привычку спорт",
        "expect": {"action": "mark_habit", "habit_any": ["спорт"]},
    },
    # ------------------------------------------------------------------
    # Цели
    # ------------------------------------------------------------------
    {
        "id": "goal-plus-10-health",
        "group": "goals",
        "phrase": "+10% к цели Здоровье",
        "expect": {
            "action": "goal_progress",
            "goal_any": ["здоров"],
            "delta_pct": 10,
        },
    },
    {
        "id": "goal-milestone-done",
        "group": "goals",
        "phrase": "веха выполнена",
        "expect": {"action": "update_last"},
        "may_ask": True,
    },
    {
        "id": "goal-new-read-12",
        "group": "goals",
        "phrase": "цель: прочитать 12 книг за год",
        "expect": {"action": "create_goal", "text_any": ["12 книг", "книг"]},
    },
    {
        "id": "goal-progress-finance-30",
        "group": "goals",
        "phrase": "прогресс по цели Финансы 30%",
        "expect": {
            "action": "goal_progress",
            "goal_any": ["финанс"],
            "delta_pct": 30,
        },
        "may_ask": True,
    },
    {
        "id": "goal-milestone-close-sport",
        "group": "goals",
        "phrase": "закрой веху спорт",
        "expect": {"action": "update_last", "goal_any": ["спорт"]},
        "may_ask": True,
    },
    # ------------------------------------------------------------------
    # Вопросы и сводки
    # ------------------------------------------------------------------
    {
        "id": "query-today",
        "group": "queries",
        "phrase": "что на сегодня",
        "expect": {"action": "query", "query_kind": "today"},
    },
    {
        "id": "query-spent-week",
        "group": "queries",
        "phrase": "сколько за неделю",
        "expect": {"action": "query", "query_kind": "spent"},
    },
    {
        "id": "query-spent",
        "group": "queries",
        "phrase": "сколько потратил",
        "expect": {"action": "query", "query_kind": "spent"},
    },
    {
        "id": "query-money-month",
        "group": "queries",
        "phrase": "сколько денег за месяц",
        "expect": {"action": "query", "query_kind": "spent"},
    },
    {
        "id": "query-read-sleep",
        "group": "queries",
        "phrase": "что читал про сон",
        "expect": {"action": "query", "query_kind": "read"},
    },
    {
        "id": "query-tasks-tomorrow",
        "group": "queries",
        "phrase": "какие задачи на завтра",
        "expect": {"action": "query", "query_kind": "today"},
    },
    {
        "id": "query-sleep-week",
        "group": "queries",
        "phrase": "как я спал за неделю",
        "expect": {"action": "query", "query_kind": "sleep"},
    },
    {
        "id": "query-energy-yesterday",
        "group": "queries",
        "phrase": "сколько энергии вчера",
        "expect": {"action": "query", "query_kind": "energy"},
    },
    {
        "id": "query-books",
        "group": "queries",
        "phrase": "покажи книги",
        "expect": {"action": "query"},
    },
    {
        "id": "query-review-week",
        "group": "queries",
        "phrase": "итоги недели",
        "expect": {"action": "review", "accept_actions": ["query"]},
        "may_ask": True,
    },
    # ------------------------------------------------------------------
    # Одно слово и короткие
    # ------------------------------------------------------------------
    {
        "id": "one-word-water",
        "group": "one_word",
        "phrase": "вода",
        "expect": {"action": "log_water"},
        "may_ask": True,
        "note": "без объёма — допустим вопрос «сколько?»",
    },
    {
        "id": "one-word-taxi",
        "group": "one_word",
        "phrase": "такси",
        "expect": {"action": "create_transaction"},
        "may_ask": True,
        "note": "без суммы — вопрос обязателен",
    },
    {
        "id": "one-word-tired",
        "group": "one_word",
        "phrase": "устал",
        "expect": {"action": "checkin_energy"},
        "may_ask": True,
    },
    # ------------------------------------------------------------------
    # Грязный ввод: опечатки, голосовые огрехи
    # ------------------------------------------------------------------
    {
        "id": "typo-call-bank",
        "group": "messy",
        "phrase": "звтра пазванить в банк",
        "expect": {
            "action": "create_task",
            "title_any": ["банк"],
            "due": ["tomorrow"],
        },
        "note": "опечатки «завтра позвонить»",
    },
    {
        "id": "typo-weight",
        "group": "messy",
        "phrase": "вес 81,2 жир 18,5 кг",
        "expect": {"action": "log_weight", "weight": 81.2, "fat": 18.5},
        "note": "единица после жира не должна ломать разбор",
    },
    {
        "id": "typo-sleep",
        "group": "messy",
        "phrase": "спал 7 с половиной",
        "expect": {"action": "log_sleep", "sleep_h": 7.5},
    },
    {
        "id": "messy-no-capitals-no-commas",
        "group": "messy",
        "phrase": "после работы зайти в аптеку купить витамин д",
        "expect": {
            "action": "create_task",
            "title_any": ["аптек", "витамин"],
        },
        "may_ask": True,
    },
    {
        "id": "inbox-gibberish",
        "group": "messy",
        "phrase": "абракадабра блабла",
        "expect": {"action": "inbox"},
        "note": "нераспознанное обязано лечь в Инбокс с исходным текстом",
    },
    {
        "id": "inbox-random",
        "group": "messy",
        "phrase": "qwerty asdf",
        "expect": {"action": "inbox"},
    },
    {
        "id": "inbox-uncertain",
        "group": "messy",
        "phrase": "хочу что-то но не знаю что",
        "expect": {"action": "inbox"},
    },
    # ------------------------------------------------------------------
    # Длинная мысль на 5–8 строк
    # ------------------------------------------------------------------
    {
        "id": "long-thought-no-data",
        "group": "long",
        "phrase": (
            "сегодня был странный день\n"
            "утром не мог встать, потом разогнался\n"
            "сделал пару дел по дому\n"
            "вечером понял, что устал не от работы, а от шума\n"
            "надо чаще выключать телефон\n"
            "и больше ходить пешком"
        ),
        "expect": {"action": "note", "title_any": ["день", "шум", "телефон"]},
    },
    {
        "id": "long-thought-with-data",
        "group": "long",
        "phrase": (
            "проснулся в шесть\n"
            "спал 6 часов\n"
            "выпил кофе\n"
            "на обед гречка 200 г\n"
            "потратил 400 на такси\n"
            "вечером устал, энергия 2\n"
            "завтра надо позвонить в банк"
        ),
        "expect": {
            "action": "inbox",
            "accept_actions": ["checkin_energy", "note", "create_task"],
            "raw_text": True,
        },
        "may_ask": True,
        "note": "длинный смешанный текст: либо разбор по частям, либо цельная запись с исходником; терять нельзя",
    },
]

#: id → короткая причина, почему фича ещё не в текущем `app.bot.intent`
#: (бот v4 — t23). Эти id помечаются xfail, а не падают.
#: Список заполняется по фактическому прогону (см. `studio/BOT-QA.md`).
KNOWN_GAPS = {
}


def by_id(item_id: str):
    """Фраза по id (для адресных проверок и отчёта)."""
    for item in PHRASES:
        if item["id"] == item_id:
            return item
    raise KeyError(item_id)


def validate() -> None:
    """Проверяет целостность корпуса; вызывается тестом, а не при импорте."""
    ids = [item["id"] for item in PHRASES]
    duplicates = {value for value in ids if ids.count(value) > 1}
    if duplicates:
        raise AssertionError("дубли id в корпусе: %s" % ", ".join(sorted(duplicates)))
    phrases = [item["phrase"] for item in PHRASES]
    duplicated_phrases = {value for value in phrases if phrases.count(value) > 1}
    if duplicated_phrases:
        raise AssertionError(
            "одинаковые фразы в корпусе: %s" % "; ".join(sorted(duplicated_phrases)[:5])
        )
    for item in PHRASES:
        for key in ("id", "group", "phrase", "expect"):
            if not item.get(key):
                raise AssertionError("у фразы %r нет поля %s" % (item.get("id"), key))
        if item["group"] not in GROUPS:
            raise AssertionError("неизвестная группа %r у %s" % (item["group"], item["id"]))
        action = item["expect"].get("action")
        if action not in ACTIONS:
            raise AssertionError("неизвестное действие %r у %s" % (action, item["id"]))
        if item["id"] in KNOWN_GAPS and not str(KNOWN_GAPS[item["id"]]).strip():
            raise AssertionError("пустая причина в KNOWN_GAPS для %s" % item["id"])
