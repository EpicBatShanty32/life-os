# Skills — что установлено и когда чем пользоваться

Установка: `npx skills@latest add <owner>/<repo> -g -a universal -s '*' -y`
Каталог: `~/.agents/skills/` (его читает DeepSeek Harness); в `~/.claude/skills/` — частичная
копия для Claude Code (55 каталогов).
Проверка: `ls ~/.agents/skills | wc -l` (было 45, на 2026-11-17 — 82).

## Карта паков

| Пак | Источник | Что даёт | Когда звать |
|---|---|---|---|
| Superpowers | `obra/superpowers` | `using-superpowers`, `writing-plans`, `executing-plans`, `test-driven-development`, `systematic-debugging`, `verification-before-completion`, `requesting-code-review`, `receiving-code-review`, `dispatching-parallel-agents`, `subagent-driven-development`, `using-git-worktrees`, `finishing-a-development-branch`, `writing-skills`, `diagnosing-superpowers`, `brainstorming` | Перед любой задачей: план → TDD → ревью → верификация. «Готово» без прогнанных команд не существует. |
| gstack | `garrytan/gstack` | Роутер продуктовых ролей: CEO (стоит ли делать), Designer (как выглядит), Eng Manager (как режем), Release Manager (что и когда выкатываем), Doc Engineer | Когда нужен взгляд «снаружи роли», а не только код: приоритизация, ретро, релизный чек-лист, документация. |
| SEO & GEO | `AgriciDaniel/claude-seo` | 29 скиллов `seo-*`: `seo-audit`, `seo-page`, `seo-technical`, `seo-geo` (AI Overviews/Perplexity), `seo-agentic` (llms.txt, agent-ready), `seo-schema`, `seo-content`, `seo-drift`, `seo-sitemap` и др. | Аудит страницы/сайта, GEO-цитируемость, подготовка демо и README к внешнему взгляду. Часть скиллов требует платных расширений (DataForSEO, Ahrefs) — они нам не нужны. |
| Frontend Slides | `zarazhangrui/frontend-slides` | Генерация HTML-презентаций: анимации, конвертация PPTX, подбор эстетики | Показать продукт людям: питч, демо-день, передача проекта. |
| Commit | `commit` (свой, по мотивам Anthropic `commit-commands`) | Читает реальный diff, стейджит по смыслу, пишет сообщение в стиле репозитория, ловит секреты и мусор | Перед каждым коммитом. Пуш — только по явной просьбе. |

**Сверка 2026-11-17:** в `~/.agents/skills/` — 82 каталога; паки таблицы закрывают 47
(Superpowers 15, `seo-*` 29, `gstack`, `frontend-slides`, `commit`). Остальные 35 — дизайн/UI,
анимация, доступность, данные и тестирование веба (`impeccable`, `design-taste-frontend`,
`animate`, `accessibility-scan`, `data-viz`, `webapp-testing`, `write-swift` и др.); их пак
в таблице не указан.

## Какие скиллы в каких ролях

- **Lead:** `writing-plans` (план и нарезка файлов), `dispatching-parallel-agents` (волна), `executing-plans` (сведение), `gstack` (взгляд релиз-менеджера).
- **bot-core, brain, vault:** `test-driven-development` (RED → GREEN), `systematic-debugging` (упавший тест), `verification-before-completion` (перед сдачей).
- **dash:** `impeccable` и `design-taste-frontend` (вид), `data-viz` (графики), `webapp-testing` (страница в браузере).
- **qa:** `webapp-testing` (e2e дашборда), `executing-plans` (прогон по шагам), `verification-before-completion` (числа и rc).
- **review, security, adversarial:** `requesting-code-review` (что смотреть, формат вердикта), `receiving-code-review` (ответ на замечания), `systematic-debugging` (воспроизведение).
- **release:** `commit` (коммит), `finishing-a-development-branch` (закрытие ветки), `verification-before-completion` (релизный чек-лист).
- **ops:** `verification-before-completion` (dry-run и rc), `systematic-debugging` (launchd, 409).
- **studio:** `writing-skills` (этот файл и роли), `find-skills` (что доустановить), `diagnosing-superpowers` (разбор провалов волны).

## Правила пользования

- Скилл — это инструкция, а не разрешение: системные ограничения проекта (только stdlib Python, без CDN, кириллические пути, не трогать личные файлы) важнее советов скилла.
- Скиллы дизайна (`impeccable`, `design-taste-*`, `frontend-slides`) не отменяют нашу палитру: фон `#F6F7FB`, текст `#17181C`, акценты `#A18BB7`, `#AEDEC2`, `#F2C9B6`, `#B9CBE8`.
- `gstack` и часть `seo-*` рассчитаны на Claude Code с MCP-расширениями. Без расширений используем только те части, что работают на файлах и тексте.
- Обновление: `npx skills@latest update -g -y`.
