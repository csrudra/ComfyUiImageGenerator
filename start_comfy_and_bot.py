import os
import socket
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
COMFY_PY = r"C:\Users\patel\AppData\Local\Comfy-Desktop\ComfyUI-Installs\comfy\ComfyUI\.venv\Scripts\python.exe"
COMFY_CWD = r"C:\Users\patel\AppData\Local\Comfy-Desktop\ComfyUI-Installs\comfy"
COMFY_LOGS = (os.path.join(BASE, "comfy_out.log"),
              os.path.join(BASE, "comfy_err.log"))
MODEL_PATHS = r"C:\Users\patel\AppData\Roaming\Comfy Desktop\instance-model-paths\inst-1790155057193.yaml"
IN_DIR = r"C:\Users\patel\AppData\Local\Comfy-Desktop\ComfyUI-Shared\input"
OUT_DIR = r"C:\Users\patel\AppData\Local\Comfy-Desktop\ComfyUI-Shared\output"

COMFY_ARGS = [
    "-s", r"ComfyUI\main.py",
    "--listen", "0.0.0.0",
    "--feature-flag", "show_signin_button=true",
    "--enable-manager",
    "--extra-model-paths-config", MODEL_PATHS,
    "--input-directory", IN_DIR,
    "--output-directory", OUT_DIR,
]

BOT_ARGS = ["telegram_bot.py"]
BOT_LOGS = (os.path.join(BASE, "telegram_bot_out.log"),
            os.path.join(BASE, "telegram_bot_err.log"))


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def http_ok(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_comfyui():
    if port_open("127.0.0.1", 8188):
        print("ComfyUI already running on 127.0.0.1:8188 - skipping start.")
        return None
    print("Starting ComfyUI (0.0.0.0:8188)...")
    fout = open(COMFY_LOGS[0], "ab")
    ferr = open(COMFY_LOGS[1], "ab")
    proc = subprocess.Popen(
        [COMFY_PY] + COMFY_ARGS,
        cwd=COMFY_CWD,
        stdout=fout,
        stderr=ferr,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(f"ComfyUI PID {proc.pid}")
    deadline = time.time() + 180
    while time.time() < deadline:
        if http_ok("http://127.0.0.1:8188/system_stats"):
            print("ComfyUI is ready.")
            return proc
        time.sleep(3)
    print("WARNING: ComfyUI did not answer within 180s - check its log.")
    return proc


def start_bot():
    print("Starting Telegram bot...")
    fout = open(BOT_LOGS[0], "ab")
    ferr = open(BOT_LOGS[1], "ab")
    proc = subprocess.Popen(
        [sys.executable] + BOT_ARGS,
        cwd=BASE,
        stdout=fout,
        stderr=ferr,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(f"Bot PID {proc.pid}")
    time.sleep(6)
    try:
        with open(BOT_LOGS[1], "r", errors="ignore") as f:
            tail = f.read()[-2000:]
        if "409" in tail or "Conflict" in tail:
            print("WARNING: bot reports 409 Conflict - another instance is polling "
                  "with this token (old bot process or another machine/phone). "
                  "It will take over once that one stops, or revoke the token in @BotFather.")
    except OSError:
        pass
    return proc


def net_info():
    try:
        import socket as s
        socks = s.socket(s.AF_INET, s.SOCK_DGRAM)
        socks.connect(("8.8.8.8", 80))
        ip = socks.getsockname()[0]
        socks.close()
    except OSError:
        ip = "unknown"
    print(f"Phone-side ComfyUI address: http://{ip}:8188")


if __name__ == "__main__":
    comfy = start_comfyui()
    bot = start_bot()
    net_info()
    print("Done. Services keep running in the background.")
    print("Close this window safely; ComfyUI and the bot stay alive.")