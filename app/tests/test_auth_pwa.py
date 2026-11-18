"""Тесты безопасности и PWA (MASTER_SPEC §2.6).

Две группы проверок:

* auth: при заданном ``api_token`` всё под ``/api`` и ``/media`` требует
  ``X-LifeOS-Token`` или cookie ``lifeos_token``; ``/api/health`` и статика
  остаются открытыми; обход каталога медиа даёт 404;
* PWA: манифест, иконки, service worker и офлайн-оболочка лежат в
  ``app/web/public`` (исходники) и корректно раздаются из собранного ``dist``
  (временного — тест не зависит от того, собран ли проект).

Секреты не должны попадать в ``/api/settings`` — это отдельная проверка.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.server.db import init_db
from app.server.main import create_app
from app.server.settings import load_settings

TOKEN = "owner-token-123"
PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
PUBLIC = PROJECT_ROOT / "app" / "web" / "public"


@pytest.fixture()
def base_settings(tmp_path):
    return load_settings(env={}, root=tmp_path)


@pytest.fixture()
def dist(tmp_path):
    """Минимальная собранная SPA: index, manifest, sw, offline, asset."""
    directory = tmp_path / "web-dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(
        '<!doctype html><html lang="ru"><div id="root"></div></html>', encoding="utf-8"
    )
    (directory / "manifest.webmanifest").write_text(
        json.dumps({"name": "Life OS", "start_url": "./#/today"}), encoding="utf-8"
    )
    (directory / "sw.js").write_text("const VERSION = 'test';", encoding="utf-8")
    (directory / "offline.html").write_text(
        "<!doctype html><title>Life OS — нет сети</title>", encoding="utf-8"
    )
    (directory / "assets" / "app-abc123.js").write_text("console.log(1);", encoding="utf-8")
    return directory


def _client(settings, *, token: str = TOKEN):
    app = create_app(settings, start_worker=False, api_token=token)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_api_requires_owner_token(base_settings) -> None:
    with _client(base_settings) as client:
        response = client.get("/api/overview")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "LifeOS-Token"
    assert "токен" in response.json()["detail"].lower()


def test_header_token_opens_api(base_settings) -> None:
    with _client(base_settings) as client:
        response = client.get("/api/overview", headers={"X-LifeOS-Token": TOKEN})

    assert response.status_code == 200
    assert "kpis" in response.json()


def test_cookie_token_opens_api(base_settings) -> None:
    with _client(base_settings) as client:
        client.cookies.set("lifeos_token", TOKEN)
        assert client.get("/api/overview").status_code == 200


def test_wrong_token_is_rejected(base_settings) -> None:
    with _client(base_settings) as client:
        assert client.get("/api/overview", headers={"X-LifeOS-Token": "nope"}).status_code == 401
        client.cookies.set("lifeos_token", "nope")
        assert client.get("/api/overview").status_code == 401


def test_health_is_public(base_settings) -> None:
    with _client(base_settings) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_media_requires_token(base_settings) -> None:
    with _client(base_settings) as client:
        assert client.get("/media/1").status_code == 401
        assert client.get("/media/1", headers={"X-LifeOS-Token": TOKEN}).status_code == 404


def test_media_traversal_is_404(base_settings) -> None:
    connection = init_db(base_settings.db_path)
    with connection:
        course = connection.execute(
            "INSERT INTO courses (title, category) VALUES ('Курс', 'тест')"
        ).lastrowid
        lesson = connection.execute(
            "INSERT INTO lessons (course_id, idx, title, media_path, duration_s) "
            "VALUES (?, 1, 'Урок', '../../etc/passwd', 60)",
            (course,),
        ).lastrowid
    connection.close()

    with _client(base_settings) as client:
        response = client.get("/media/%s" % lesson, headers={"X-LifeOS-Token": TOKEN})

    assert response.status_code == 404


def test_no_token_configured_keeps_api_open(base_settings) -> None:
    """Пустой токен — режим localhost без auth (обратная совместимость)."""
    with _client(base_settings, token="") as client:
        assert client.get("/api/overview").status_code == 200


def test_settings_hides_secrets(tmp_path) -> None:
    settings = load_settings(
        env={
            "LIFEOS_TELEGRAM_TOKEN": "tg-secret-777",
            "LIFEOS_LLM_API_KEY": "sk-secret-888",
        },
        root=tmp_path,
    )
    with _client(settings) as client:
        response = client.get("/api/settings", headers={"X-LifeOS-Token": TOKEN})

    assert response.status_code == 200
    body = response.text
    assert "tg-secret-777" not in body
    assert "sk-secret-888" not in body
    payload = response.json()
    assert payload["telegram"]["token_set"] is True
    assert payload["llm"]["api_key_set"] is True


# ---------------------------------------------------------------------------
# Статика и PWA
# ---------------------------------------------------------------------------


def test_static_shell_served_without_token(base_settings, dist) -> None:
    settings = replace(base_settings, web_dist=dist)
    with _client(settings) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "text/html" in index.headers["content-type"]
        assert 'id="root"' in index.text
        assert "no-cache" in index.headers.get("cache-control", "")

        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == 200
        assert "manifest+json" in manifest.headers["content-type"]
        assert manifest.json()["name"] == "Life OS"

        worker = client.get("/sw.js")
        assert worker.status_code == 200
        assert "javascript" in worker.headers["content-type"]
        assert "no-cache" in worker.headers.get("cache-control", "")

        offline = client.get("/offline.html")
        assert offline.status_code == 200
        assert "нет сети" in offline.text


def test_hashed_assets_are_immutable(base_settings, dist) -> None:
    settings = replace(base_settings, web_dist=dist)
    with _client(settings) as client:
        response = client.get("/assets/app-abc123.js")

    assert response.status_code == 200
    assert "max-age=31536000" in response.headers.get("cache-control", "")
    assert "immutable" in response.headers.get("cache-control", "")


def test_pwa_manifest_source_is_valid() -> None:
    manifest = json.loads((PUBLIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == "Life OS"
    assert manifest["short_name"] == "Life OS"
    assert manifest["lang"] == "ru"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"].upper() == "#F5F4F1"
    assert manifest["start_url"].endswith("#/")
    assert len(manifest["icons"]) >= 3
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
    assert manifest["shortcuts"]


def test_pwa_icons_are_real_png() -> None:
    manifest = json.loads((PUBLIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        path = PUBLIC / icon["src"]
        assert path.is_file(), "нет иконки %s" % path
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "иконка %s не PNG" % path


def test_service_worker_caches_shell_and_bypasses_api() -> None:
    source = (PUBLIC / "sw.js").read_text(encoding="utf-8")
    assert "lifeos-v4" in source
    assert "offline.html" in source
    assert "manifest.webmanifest" in source
    assert "skipWaiting" in source
    assert "clients.claim" in source
    # API и медиа — только из сети, оболочка — из кеша при офлайне
    assert "'/api/'" in source
    assert "'/media/'" in source
    assert "caches.match('./index.html')" in source


def test_index_links_pwa_and_service_worker_registers() -> None:
    index = (PROJECT_ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in index
    assert "manifest.webmanifest" in index
    assert 'name="theme-color"' in index
    assert 'rel="apple-touch-icon"' in index
    assert 'lang="ru"' in index

    main = (PROJECT_ROOT / "app" / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
    assert "serviceWorker" in main
    assert "import.meta.env.PROD" in main
    assert "sw.js" in main


def test_offline_shell_is_readable() -> None:
    offline = (PUBLIC / "offline.html").read_text(encoding="utf-8")
    assert 'lang="ru"' in offline
    assert "нет сети" in offline.lower()


def test_built_dist_has_pwa_files_when_built() -> None:
    built = PROJECT_ROOT / "app" / "web" / "dist"
    if not (built / "index.html").is_file():
        pytest.skip("SPA не собрана — проверка сборки живёт в check.sh")
    assert (built / "manifest.webmanifest").is_file()
    assert (built / "sw.js").is_file()
    assert (built / "offline.html").is_file()
    assert (built / "icon-192.png").is_file()
    built_index = (built / "index.html").read_text(encoding="utf-8")
    assert "manifest.webmanifest" in built_index
