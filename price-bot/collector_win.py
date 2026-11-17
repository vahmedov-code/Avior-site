"""Сборщик цен для Windows-сервера.

  python collector_win.py login    — открыть окно браузера и войти в Telegram по QR-коду (один раз)
  python collector_win.py run      — прочитать канал поставщика и отправить на сервер бота
  python collector_win.py fetch    — только прочитать канал и сохранить collected.json (для проверки)
  python collector_win.py watch    — есть ли запрос «обновить цены сейчас» из бота (/prices); если да — run.
                                     Запускается Планировщиком каждые 5 минут, без запроса завершается сразу.

Настройки — в collector.env рядом с этим файлом (см. collector.env.example).
Нужны только Python, playwright и встроенный в Windows ssh. Токен бота здесь не нужен:
цены разбирает и отчёт присылает бот на Linux-сервере.
"""
import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from app import collector

HERE = Path(__file__).resolve().parent
ATTEMPTS = 3
RETRY_PAUSE = 120  # секунд между попытками
log = logging.getLogger("collector")


def load_settings() -> dict:
    settings = {
        "SSH_HOST": "root@89.167.29.54",
        "SSH_KEY": "",
        "REMOTE_DIR": "/opt/avior/price-bot",
        "PROFILE_DIR": str(HERE / "browser"),
    }
    env = HERE / "collector.env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                settings[key.strip()] = value.strip()
    return settings


def ssh_base(s: dict) -> list[str]:
    opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    if s["SSH_KEY"]:
        opts += ["-i", s["SSH_KEY"]]
    return opts


def remote(s: dict, args: str) -> str:
    cmd = f"cd {s['REMOTE_DIR']} && sudo -u avior-bot .venv/bin/python manage.py {args}"
    done = subprocess.run(["ssh", *ssh_base(s), s["SSH_HOST"], cmd],
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    if done.returncode != 0:
        # причина ошибки — в конце вывода Python, показываем хвост
        raise RuntimeError(f"сервер бота ответил ошибкой: …{(done.stderr or done.stdout).strip()[-1500:]}")
    return done.stdout


def upload(s: dict, local: Path, remote_path: str) -> None:
    done = subprocess.run(["scp", *ssh_base(s), str(local), f"{s['SSH_HOST']}:{remote_path}"],
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    if done.returncode != 0:
        raise RuntimeError(f"не удалось отправить файл на сервер бота: {done.stderr.strip()[:500]}")


class Busy(Exception):
    """Сборщик уже работает (по расписанию или по запросу) — второй браузер с тем же профилем не запускаем."""


class RunLock:
    """Файл-замок: не даёт запустить два сборщика одновременно. Брошенный замок старше часа не мешает."""
    path = HERE / "collector.lock"

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - self.path.stat().st_mtime < 3600:
                raise Busy()
            self.path.unlink(missing_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def watch(s: dict) -> int:
    """Запрос из бота (/prices → «Обновить цены сейчас»): забираем его с сервера и запускаем сборщик."""
    try:
        answer = remote(s, "collect-request --take").strip()
    except Exception as e:  # сервер на минуту недоступен — проверим через 5 минут, журнал не засоряем
        log.warning("Не удалось спросить сервер о запросе: %s", str(e)[:150])
        return 0
    if answer != "run":
        return 0
    log.info("Запрос из бота: обновить цены сейчас")
    return run(s, report=True)  # по запросу отчёт присылаем всегда, даже без изменений


def run(s: dict, report: bool) -> int:
    try:
        with RunLock():
            return _run(s, report)
    except Busy:
        log.info("Сборщик уже работает — этот запуск пропускаю")
        return 0


def _run(s: dict, report: bool) -> int:
    supplier = json.loads(remote(s, "supplier"))
    log.info("Канал %s, кнопок меню: %s", supplier.get("name"), len(supplier["menu"]))
    for attempt in range(1, ATTEMPTS + 1):
        result = asyncio.run(collector.fetch(supplier, s["PROFILE_DIR"]))
        log.info("Прочитано постов: %s, успех: %s", len(result.get("posts", {})), result["ok"])
        if result["ok"] or not result.get("retry", True) or attempt == ATTEMPTS:
            break
        # Временный сбой (Telegram долго грузится, сеть): не тревожим владельца, пробуем ещё раз
        log.warning("Попытка %s из %s не удалась: %s — повтор через %s с", attempt, ATTEMPTS,
                    result.get("error"), RETRY_PAUSE)
        time.sleep(RETRY_PAUSE)
    local = HERE / "collected.json"
    local.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    remote_file = f"{s['REMOTE_DIR']}/data/collected.json"
    upload(s, local, remote_file)
    print(remote(s, f"import-collected {remote_file}" + (" --report" if report else "")))
    return 0 if result["ok"] else 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "collector.log", encoding="utf-8")],
    )
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["login", "run", "fetch", "watch"])
    parser.add_argument("--report", action="store_true", help="прислать отчёт в бота, даже если цены не менялись")
    args = parser.parse_args()
    s = load_settings()
    try:
        if args.command == "login":
            sys.exit(0 if asyncio.run(collector.login(s["PROFILE_DIR"])) else 1)
        if args.command == "fetch":
            supplier = json.loads(remote(s, "supplier"))
            result = asyncio.run(collector.fetch(supplier, s["PROFILE_DIR"]))
            (HERE / "collected.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            print("Сохранено в collected.json:", "успех" if result["ok"] else result["error"])
            sys.exit(0 if result["ok"] else 1)
        if args.command == "watch":
            sys.exit(watch(s))
        sys.exit(run(s, args.report))
    except Exception:
        log.exception("Сборщик упал")
        sys.exit(2)


if __name__ == "__main__":
    main()
