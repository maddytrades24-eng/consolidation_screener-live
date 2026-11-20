
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = os.getenv("PORT", "10000")

engine = None
dashboard = None


def stop_processes():
    for process in (dashboard, engine):
        if process and process.poll() is None:
            process.terminate()

    for process in (dashboard, engine):
        if process and process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


try:
    print("[START] Starting screener engine...", flush=True)

    engine = subprocess.Popen(
        [sys.executable, str(ROOT / "screener_engine.py")],
        cwd=str(ROOT),
    )

    print("[START] Starting Streamlit dashboard...", flush=True)

    dashboard = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ROOT / "app.py"),
            "--server.address",
            "0.0.0.0",
            "--server.port",
            PORT,
            "--server.headless",
            "true",
        ],
        cwd=str(ROOT),
    )

    while True:
        if dashboard.poll() is not None:
            print("[ERROR] Dashboard stopped.", flush=True)
            break

        if engine.poll() is not None:
            print(
                "[ERROR] Screener engine exited. "
                "Stopping deployment.",
                flush=True,
            )
            break

        time.sleep(3)

except KeyboardInterrupt:
    print("[START] Shutdown requested.", flush=True)

finally:
    stop_processes()
    print("[START] Processes stopped.", flush=True)
