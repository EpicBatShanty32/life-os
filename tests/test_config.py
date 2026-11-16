"""Тесты src/config.py: defaults, merge, env LIFEOS_CONFIG, битый JSON, типы."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (правит sys.path, импортирует config)
from src import config as config_mod


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lifeos-cfg-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------ помощники
    def write_config(self, payload, name: str = "config.json") -> str:
        path = self.tmp / name
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    # --------------------------------------------------------------- тесты
    def test_defaults_fill_empty_config(self) -> None:
        cfg = config_mod.load_config(self.write_config({}))
        for section in ("telegram", "obsidian", "llm", "schedule", "paths", "app"):
            self.assertIn(section, cfg)
        self.assertEqual(cfg["app"]["timezone"], "Europe/Moscow")
        self.assertEqual(cfg["schedule"]["morning"]["time"], "08:30")
        self.assertEqual(cfg["llm"]["max_followups"], 2)
        self.assertEqual(cfg["telegram"]["token"], "")
        # computed-поля
        self.assertTrue(os.path.isabs(cfg["_root"]))
        self.assertEqual(cfg["_root"], str(config_mod.project_root()))
        self.assertTrue(os.path.isabs(cfg["_config_path"]))
        self.assertTrue(cfg["_config_path"].endswith("config.json"))

    def test_example_config_is_valid(self) -> None:
        example = config_mod.project_root() / "config" / "config.example.json"
        if not example.is_file():
            self.skipTest("config/config.example.json ещё не создан ops")
        cfg = config_mod.load_config(str(example))
        self.assertEqual(cfg["app"]["name"], "Life OS")
        self.assertEqual(cfg["telegram"]["token"], "", "в примере не должно быть токена")
        self.assertEqual(cfg["llm"]["api_key"], "", "в примере не должно быть ключа")

    def test_deep_merge_keeps_defaults(self) -> None:
        path = self.write_config({
            "telegram": {"token": "123:ABC", "allowed_chat_id": 42},
            "app": {"name": "Test OS"},
            "schedule": {"morning": {"time": "07:15"}},
        })
        cfg = config_mod.load_config(path)
        self.assertEqual(cfg["telegram"]["token"], "123:ABC")
        self.assertEqual(cfg["telegram"]["allowed_chat_id"], 42)
        self.assertEqual(cfg["telegram"]["api_base"], "https://api.telegram.org")
        self.assertEqual(cfg["app"]["name"], "Test OS")
        self.assertEqual(cfg["app"]["lang"], "ru")
        self.assertEqual(cfg["schedule"]["morning"]["time"], "07:15")
        self.assertTrue(cfg["schedule"]["morning"]["enabled"])
        self.assertEqual(cfg["schedule"]["midday"]["time"], "14:00")
        self.assertEqual(cfg["llm"]["model"], "deepseek-v4.1-flash-1017")

    def test_defaults_not_mutated_between_loads(self) -> None:
        before = json.dumps(config_mod.DEFAULTS, sort_keys=True, ensure_ascii=False)
        config_mod.load_config(self.write_config({"app": {"name": "X"}}))
        after = json.dumps(config_mod.DEFAULTS, sort_keys=True, ensure_ascii=False)
        self.assertEqual(before, after)
        cfg2 = config_mod.load_config(self.write_config({}, "second.json"))
        self.assertEqual(cfg2["app"]["name"], "Life OS")

    def test_env_lifeos_config(self) -> None:
        path = self.write_config({"app": {"name": "From Env"}}, "env.json")
        with mock.patch.dict(os.environ, {"LIFEOS_CONFIG": path}, clear=False):
            cfg = config_mod.load_config()
        self.assertEqual(cfg["app"]["name"], "From Env")
        self.assertEqual(cfg["_config_path"], str(Path(path).resolve()))

    def test_explicit_path_beats_env(self) -> None:
        env_path = self.write_config({"app": {"name": "From Env"}}, "env.json")
        explicit = self.write_config({"app": {"name": "Explicit"}}, "explicit.json")
        with mock.patch.dict(os.environ, {"LIFEOS_CONFIG": env_path}, clear=False):
            cfg = config_mod.load_config(explicit)
        self.assertEqual(cfg["app"]["name"], "Explicit")

    def test_missing_env_file_raises(self) -> None:
        with mock.patch.dict(os.environ, {"LIFEOS_CONFIG": str(self.tmp / "nope.json")},
                             clear=False):
            with self.assertRaises(config_mod.ConfigError):
                config_mod.load_config()

    def test_missing_explicit_file_raises(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_config(str(self.tmp / "nope.json"))

    def test_broken_json_raises(self) -> None:
        path = self.write_config('{"telegram": {"token": "x",}}')
        with self.assertRaises(config_mod.ConfigError) as ctx:
            config_mod.load_config(path)
        self.assertIn("JSON", str(ctx.exception))

    def test_json_not_object_raises(self) -> None:
        path = self.write_config("[1, 2, 3]")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_config(path)

    def test_wrong_types_raise(self) -> None:
        cases = [
            {"telegram": {"token": 123}},
            {"telegram": {"allowed_chat_id": "not-a-number"}},
            {"telegram": {"api_base": ""}},
            {"llm": {"enabled": "yes"}},
            {"llm": {"max_followups": 99}},
            {"llm": {"timeout": -5}},
            {"schedule": {"morning": {"time": "8:60"}}},   # минут 60 не бывает
            {"schedule": {"midday": {"time": "25:00"}}},
            {"schedule": {"weekly": {"weekday": 9}}},
            {"app": {"timezone": "Mars/Olympus"}},
            {"paths": {"db": ""}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                path = self.write_config(payload)
                with self.assertRaises(config_mod.ConfigError):
                    config_mod.load_config(path)

    def test_chat_id_from_string_and_negative_rejected(self) -> None:
        cfg = config_mod.load_config(self.write_config(
            {"telegram": {"allowed_chat_id": "123456"}}
        ))
        self.assertEqual(cfg["telegram"]["allowed_chat_id"], 123456)
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_config(self.write_config(
                {"telegram": {"allowed_chat_id": -1}}
            ))

    def test_unknown_keys_allowed(self) -> None:
        cfg = config_mod.load_config(self.write_config({
            "custom": {"x": 1},
            "schedule": {"catchup_minutes": 60},
        }))
        self.assertEqual(cfg["custom"]["x"], 1)
        self.assertEqual(cfg["schedule"]["catchup_minutes"], 60)

    def test_resolve_path(self) -> None:
        cfg = config_mod.load_config(self.write_config({}))
        relative = config_mod.resolve_path(cfg, "data/lifeos.db")
        self.assertTrue(os.path.isabs(relative))
        self.assertEqual(relative, str(config_mod.project_root() / "data" / "lifeos.db"))
        absolute = str(self.tmp / "x.db")
        self.assertEqual(config_mod.resolve_path(cfg, absolute), absolute)
        # относительный путь без cfg["_root"] считается от project_root
        self.assertTrue(os.path.isabs(config_mod.resolve_path({}, "a/b")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
