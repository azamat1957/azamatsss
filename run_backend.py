from __future__ import annotations

import os
import subprocess
import sys
import time


def run_single(script: str) -> int:
    return subprocess.call([sys.executable, script])


def start(script: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, script])


def main() -> None:
    mode = os.getenv("SERVICE_MODE", "all").strip().lower()
    redis_enabled = bool(os.getenv("REDIS_URL", "").strip())

    if mode == "bot":
        raise SystemExit(run_single("bot.py"))
    if mode == "worker":
        raise SystemExit(run_single("worker.py"))

    # Default: one Railway service. Redis bo'lmasa bot inline fallback rejimida ishlaydi.
    if not redis_enabled:
        print("REDIS_URL sozlanmagan: bot inline fallback rejimida ishga tushadi.", flush=True)
        raise SystemExit(run_single("bot.py"))

    bot_proc = start("bot.py")
    worker_proc = start("worker.py")
    worker_restart_delay = 3

    try:
        while True:
            bot_rc = bot_proc.poll()
            if bot_rc is not None:
                if worker_proc.poll() is None:
                    worker_proc.terminate()
                raise SystemExit(bot_rc)

            worker_rc = worker_proc.poll()
            if worker_rc is not None:
                # Botni o'chirmaymiz: queue ishlamasa botning inline fallback'i bor.
                print(
                    f"Worker to'xtadi (code={worker_rc}). {worker_restart_delay}s dan keyin qayta ishga tushadi.",
                    flush=True,
                )
                time.sleep(worker_restart_delay)
                worker_proc = start("worker.py")
                worker_restart_delay = min(30, worker_restart_delay * 2)
            else:
                worker_restart_delay = 3

            time.sleep(1)
    except KeyboardInterrupt:
        if bot_proc.poll() is None:
            bot_proc.terminate()
        if worker_proc.poll() is None:
            worker_proc.terminate()
        raise


if __name__ == "__main__":
    main()
