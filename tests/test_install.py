"""Тесты установки/ops: bash -n, plutil -lint, config.example.json, README."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import unittest

import _support

ROOT = _support.PROJECT_ROOT


class InstallArtifactsTestCase(unittest.TestCase):
    def test_scripts_exist_and_syntax_ok(self) -> None:
        for name in ("install.sh", "uninstall.sh"):
            path = ROOT / name
            with self.subTest(script=name):
                self.assertTrue(path.is_file(), "%s отсутствует" % name)
                self.assertTrue(os.access(path, os.X_OK), "%s не исполняемый" % name)
                result = subprocess.run(
                    ["bash", "-n", str(path)],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0,
                                 "bash -n %s: %s" % (name, result.stderr))
                first_line = path.read_text(encoding="utf-8").splitlines()[0]
                self.assertTrue(first_line.startswith("#!"),
                                "нет shebang: %r" % first_line)
                self.assertIn("bash", first_line)

    def test_install_help_exits_zero(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertIn("install.sh", output)
        self.assertIn("--dry-run", output)

    def test_uninstall_help_exits_zero(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "uninstall.sh"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--purge", result.stdout + result.stderr)

    def test_no_secrets_in_repository_files(self) -> None:
        patterns = [
            re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
            re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),  # токен бота
        ]
        for name in ("install.sh", "uninstall.sh", "config/config.example.json",
                     "ops/com.lifeos.agent.plist"):
            path = ROOT / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                self.assertIsNone(
                    pattern.search(text),
                    "похоже на секрет в %s: %r" % (name, pattern.pattern),
                )


class PlistTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = ROOT / "ops" / "com.lifeos.agent.plist"
        if not self.path.is_file():
            self.skipTest("ops/com.lifeos.agent.plist ещё не создан ops")

    def test_plutil_lint(self) -> None:
        plutil = shutil.which("plutil") or "/usr/bin/plutil"
        if not os.path.exists(plutil):
            self.skipTest("plutil недоступен (не macOS)")
        result = subprocess.run([plutil, "-lint", str(self.path)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

    def test_plist_structure_and_placeholders(self) -> None:
        with self.path.open("rb") as handle:
            data = plistlib.load(handle)
        self.assertEqual(data["Label"], "com.lifeos.agent")
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertIn("__APP_DIR__", data["WorkingDirectory"])
        self.assertIn("__LOG_DIR__", data["StandardOutPath"])
        args = data["ProgramArguments"]
        self.assertTrue(any("python" in arg for arg in args))
        self.assertTrue(any(arg.endswith("src/main.py") for arg in args))
        # шаблон не должен быть прибит к домашней папке конкретного пользователя
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", raw)
        self.assertNotIn("sk-", raw)


class ConfigExampleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = ROOT / "config" / "config.example.json"
        if not self.path.is_file():
            self.skipTest("config/config.example.json ещё не создан ops")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def test_json_and_sections(self) -> None:
        for section in ("telegram", "obsidian", "llm", "schedule", "paths", "app"):
            self.assertIn(section, self.data)
            self.assertIsInstance(self.data[section], dict)

    def test_safe_defaults(self) -> None:
        telegram = self.data["telegram"]
        self.assertEqual(telegram["token"], "", "в примере не должно быть токена")
        self.assertEqual(telegram["allowed_chat_id"], 0)
        self.assertTrue(telegram["api_base"].startswith("https://"))
        llm = self.data["llm"]
        self.assertEqual(llm["api_key"], "", "в примере не должно быть ключа")
        self.assertTrue(llm["enabled"])
        self.assertEqual(llm["max_followups"], 2)

    def test_schedule_shape(self) -> None:
        schedule = self.data["schedule"]
        for slot in ("morning", "midday", "evening"):
            spec = schedule[slot]
            self.assertRegex(spec["time"], r"^([01]?\d|2[0-3]):[0-5]\d$")
            self.assertIsInstance(spec["enabled"], bool)
        weekly = schedule["weekly"]
        self.assertRegex(weekly["time"], r"^([01]?\d|2[0-3]):[0-5]\d$")
        self.assertIsInstance(weekly["weekday"], int)
        self.assertGreaterEqual(weekly["weekday"], 0)
        self.assertLessEqual(weekly["weekday"], 7)

    def test_paths_and_obsidian(self) -> None:
        paths = self.data["paths"]
        self.assertTrue(paths["db"].endswith(".db"))
        self.assertTrue(paths["dashboard_out"].endswith(".html"))
        obsidian = self.data["obsidian"]
        self.assertIn("vault_path", obsidian)
        self.assertIsInstance(obsidian["daily_folder"], str)
        self.assertIsInstance(obsidian["template_path"], str)
        self.assertIsInstance(obsidian["write_mirror"], bool)

    def test_example_loads_through_config_module(self) -> None:
        from src import config as config_mod

        cfg = config_mod.load_config(str(self.path))
        self.assertEqual(cfg["app"]["timezone"], "Europe/Moscow")
        self.assertEqual(cfg["telegram"]["token"], "")


class ReadmeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = ROOT / "README.md"
        if not self.path.is_file():
            self.skipTest("README.md ещё не создан ops")

    def test_readme_covers_install_and_commands(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        self.assertGreater(len(text), 1500, "README слишком короткий")
        for needle in ("BotFather", "install.sh", "/help", "/pause",
                       "dashboard", "launchctl", "uninstall.sh"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # не должен содержать реальных секретов
        self.assertNotRegex(text, r"sk-[A-Za-z0-9_\-]{12,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
