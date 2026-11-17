# Life OS v3 — фаза 6: PWA, безопасность, деплой-артефакты

- **Статус:** готово (деплой-артефакты статически; в docker/systemd не запускались)
- **Дата:** 2026-11-17
- **Коммит волны:** c45bb2a, d0a6c9e, `4d093d9`
- **Требование:** MASTER_SPEC §2.6

## PWA

- `app/web/public/manifest.webmanifest`: `name/short_name`, `lang=ru`, `display=standalone`, `theme_color=#F6F7FB`, `start_url=./#/today`, иконки 192/512 + maskable, 3 shortcuts (Сегодня, Журнал, Курсы).
- `sw.js`: оболочка — сеть с обновлением кеша, при офлайне `index.html`/`offline.html`; хешированные ассеты — из кеша; `/api/`, `/media/`, `/ws` — только сеть; `skipWaiting`, `clients.claim`.
- `offline.html` — русская страница «нет сети»; `apple-touch-icon-180.png`, `theme-color` для светлой/тёмной темы, iOS-мета.
- `main.tsx` регистрирует SW только в PROD (`import.meta.env.PROD`), чтобы не мешать HMR.
- Мобильная нижняя навигация (`MobileNav`) с бейджами и `aria-current`.

## Безопасность

- Токен владельца: `LIFEOS_API_TOKEN` или `config.app.api_token`. Пусто = режим localhost без auth.
- `/api/*` и `/media/*` требуют `X-LifeOS-Token` или cookie `lifeos_token`; сравнение через `secrets.compare_digest`; отказ — `401` + `WWW-Authenticate: LifeOS-Token`.
- `/api/health` публичный (для launchd/healthcheck), статика SPA публична.
- `/api/settings` отдаёт `safe_dict()`: секреты заменены флагами `token_set`/`api_key_set`.
- Кеш-политика: `index.html`, `sw.js`, `manifest`, `offline.html` — `no-cache`; `/assets/*` — `max-age=31536000, immutable`.
- CORS разрешён только для `localhost`/`127.0.0.1` (любой порт).
- В репозитории только `deploy/.env.example`; `.env` в `.gitignore` и `.dockerignore`.

## Деплой-артефакты (`deploy/`)

`docker-compose.prod.yml` (6 сервисов), `Dockerfile.api`, `Dockerfile.bot`, `Dockerfile.web`, `Caddyfile`, `nginx-web.conf`, `lifeos.service`, `lifeos-bot.service`, `.env.example`, `check-deploy.sh`; переменные сведены к общим именам (D-16). Инструкция — `DEPLOY_GUIDE.md`.

## Чем проверено

```text
python3 -m pytest app/tests/test_auth_pwa.py -q      # 17 passed
bash deploy/check-deploy.sh
Итог: ✔ 121  ✖ 0  ! 0
```

Проверено: 401 без токена / с неверным токеном, доступ по header и cookie, публичный health, обход каталога медиа → 404, отсутствие секретов в `/api/settings`, MIME `application/manifest+json`, `text/javascript` для SW, `no-cache`/`immutable`, наличие и валидность PNG-иконок, содержимое манифеста и SW.

## Что осталось / не проверено

- **Docker и systemd в среде отсутствуют** — compose/Dockerfile/systemd проверены только статически (`check-deploy.sh`, 121 проверка). Это честная пометка, а не «работает».
- Установка PWA на домашний экран вручную не проверялась (текстовая среда).
- HTTPS/TLS и домен — только в артефактах Caddy, живого домена нет.
