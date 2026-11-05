"""Manage the local Streamlit process without interrupting another application."""

import socket
import subprocess
import time
from urllib.request import urlopen

from src.common.config import PROJECT_ROOT


def start_dashboard(command, env, port, report_dir):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"Port {port} is already in use. Choose another --port or stop the existing Streamlit server.") from None
    log_path = report_dir / "streamlit.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Streamlit stopped during startup. Inspect the Streamlit log in the report directory.")
            try:
                with urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1) as response:
                    if response.status == 200:
                        return process
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Streamlit did not become ready. Inspect the Streamlit log in the report directory.")
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise


def stop_dashboard(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
