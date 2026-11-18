"""Запуск tools/backup.sh из службы launchd.

Зачем прослойка: macOS не пускает фоновые задания launchd в папку «Документы»,
если само задание — это /bin/bash: процесс получает «Operation not permitted».
Python из официальной сборки (Python.framework) — это приложение-бандл
Python.app, и ему доступ к «Документам» разрешён. Поэтому служба com.lifeos.backup
запускает этот файл, а он уже вызывает shell-скрипт бэкапа с теми же флагами.

Логика бэкапа живёт в tools/backup.sh (одно место, одна правда). Здесь только
передача аргументов и кода выхода.

Проверка вручную::

    /usr/local/bin/python3 tools/backup_launcher.py
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "backup.sh")


def main(argv: list[str]) -> int:
    if not os.path.isfile(SCRIPT):
        sys.stderr.write("нет скрипта бэкапа: %s\n" % SCRIPT)
        return 1
    return subprocess.call(["/bin/bash", SCRIPT, *argv], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
