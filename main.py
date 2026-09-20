import asyncio
import json
import logging
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from kaggle.api.kaggle_api_extended import KaggleApi
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME", "kdidid")
KAGGLE_API_TOKEN = os.getenv("KAGGLE_API_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
KERNEL_ID = f"{KAGGLE_USERNAME}/rife-worker"
WORKER_DIR = Path("./kaggle_worker").resolve()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN")
if not KAGGLE_API_TOKEN:
    raise RuntimeError("Не найден KAGGLE_API_TOKEN")
os.environ["KAGGLE_API_TOKEN"] = KAGGLE_API_TOKEN
os.environ["KAGGLE_USERNAME"] = KAGGLE_USERNAME
api = KaggleApi()
api.authenticate()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"RIFE Telegram Bot is running")

    def log_message(self, format, *args):
        pass


def start_http_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


user_videos = {}

WORKER_TEMPLATE = r'''import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import requests

JOB_CONFIG = __JOB_CONFIG__
VIDEO_URL = JOB_CONFIG["VIDEO_URL"]
CHAT_ID = JOB_CONFIG["CHAT_ID"]
BOT_TOKEN = JOB_CONFIG["BOT_TOKEN"]
TARGET_FPS = int(JOB_CONFIG["TARGET_FPS"])
RIFE_URL = ("https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
            "20221029/rife-ncnn-vulkan-20221029-ubuntu.zip")
RIFE_ZIP = "rife.zip"
RIFE_DIR = "rife-ncnn-vulkan-20221029-ubuntu"
RIFE_EXE = f"./{RIFE_DIR}/rife-ncnn-vulkan"
VIDEO_FILE = "input.mp4"
AUDIO_FILE = "audio.m4a"
INPUT_FRAMES = "input_frames"
OUTPUT_FRAMES = "output_frames"
OUTPUT_FILE = "output.mp4"


def run(command, name):
    print("\n" + "=" * 60 + "\n" + name + "\n" + command + "\n" + "=" * 60)
    result = subprocess.run(command, shell=True)
    if result.returncode:
        raise RuntimeError(f"Ошибка: {name}")


def _run(command, env=None):
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + (("\n" + result.stderr) if result.stderr else "")


def _lines(command):
    code, output = _run(["bash", "-lc", command])
    return code, output.strip()


def _icd_files():
    files = []
    for directory in (Path("/usr/share/vulkan/icd.d"), Path("/etc/vulkan/icd.d"),
                      Path("/usr/local/share/vulkan/icd.d")):
        if directory.exists():
            files.extend(sorted(directory.glob("*.json")))
    return list(dict.fromkeys(files))


def _is_nvidia_icd(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        text = json.dumps(data).lower()
    except (OSError, ValueError):
        text = path.name.lower()
    return "nvidia" in path.name.lower() or "nvidia" in text


def _nvidia_icds():
    return [p for p in _icd_files() if _is_nvidia_icd(p)]


def _print_diagnostics(title="NVIDIA Vulkan diagnostics"):
    print("\n" + "=" * 80 + "\n" + title + "\n" + "=" * 80)
    for command in ("nvidia-smi", "nvidia-smi --query-gpu=driver_version --format=csv,noheader",
                    "cat /etc/os-release", "dpkg-query -W -f='${Package} ${Version}\n' 'nvidia*' 'libnvidia*' 2>/dev/null || true",
                    "ldconfig -p | grep -Ei 'nvidia|vulkan' || true",
                    "find /usr /lib /opt -type f \\(... -iname '*nvidia*vulkan*' -o -iname 'libnvidia-vulkan-producer.so*' -o -iname 'libGLX_nvidia.so*' \\) 2>/dev/null | sort -u || true"):
        code, output = _lines(command)
        print(f"$ {command} (exit {code})\n{output or '<нет вывода>'}")
    for directory in (Path("/usr/share/vulkan/icd.d"), Path("/etc/vulkan/icd.d")):
        print(f"ICD directory {directory}: {'exists' if directory.exists() else 'absent'}")
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            print(f"--- {path} ---\n{path.read_text(encoding='utf-8', errors='replace')}")
    print("Environment:")
    for name in ("VK_ICD_FILENAMES", "VK_DRIVER_FILES", "LD_LIBRARY_PATH"):
        print(f"  {name}={os.environ.get(name, '<не установлена>')}")


def _driver_major():
    code, output = _lines("nvidia-smi --query-gpu=driver_version --format=csv,noheader")
    match = re.search(r"(\\d+)", output)
    if code or not match:
        raise RuntimeError(f"Не удалось определить версию NVIDIA driver: {output or '<нет вывода>'}")
    return match.group(1), output.splitlines()[0].strip()


def _apt_package_candidates(major):
    # Используем только пакеты из текущих apt-репозиториев и только той же major-ветки.
    code, output = _lines("apt-cache search '^\\(nvidia.*vulkan\\|nvidia.*icd\\|libnvidia-gl\\|nvidia-driver\\)' || true")
    print("Доступные NVIDIA apt-пакеты:\n", output or "<не найдены>")
    names = {line.split(None, 1)[0] for line in output.splitlines() if line.strip()}
    exact = [f"nvidia-vulkan-icd-{major}", f"libnvidia-gl-{major}", f"nvidia-driver-{major}"]
    # nvidia-vulkan-icd-<major> is preferred; libnvidia-gl-<major> commonly owns the ICD.
    selected = [name for name in exact if name in names]
    if not selected:
        selected = sorted(name for name in names if re.search(rf"(?:-|){re.escape(major)}$", name)
                          and ("vulkan" in name or "icd" in name or "libnvidia-gl" in name))
    return selected[:2], output


def install_vulkan_runtime():
    """Install only a driver-major-matching NVIDIA Vulkan ICD; never replace Mesa/libvulkan."""
    print("Проверка NVIDIA Vulkan runtime перед RIFE")
    _print_diagnostics("Initial NVIDIA/Vulkan diagnostics")
    if _nvidia_icds():
        print("NVIDIA Vulkan ICD уже найден; установка не требуется.")
        return
    major, full_version = _driver_major()
    print(f"Установленный NVIDIA driver: {full_version}; выбранная major-ветка: {major}")
    os_release = Path("/etc/os-release").read_text(errors="replace") if Path("/etc/os-release").exists() else "<отсутствует>"
    print("ОС:\n" + os_release)
    run("apt-get update -y", "Обновление apt-индексов")
    packages, available = _apt_package_candidates(major)
    if not packages:
        raise RuntimeError(f"Совместимый NVIDIA Vulkan ICD пакет не найден в текущих apt-репозиториях. "
                           f"driver={full_version}, major={major}. Доступные пакеты:\n{available}")
    command = "export DEBIAN_FRONTEND=noninteractive; apt-get install -y --no-install-recommends " + " ".join(packages)
    run(command, "Установка NVIDIA Vulkan ICD/runtime")
    run("ldconfig", "Обновление ldconfig после NVIDIA Vulkan runtime")
    _print_diagnostics("Diagnostics after NVIDIA Vulkan runtime installation")
    found = _nvidia_icds()
    if not found:
        raise RuntimeError(f"После установки пакетов {packages} NVIDIA ICD JSON не найден. "
                           f"driver={full_version}; доступные библиотеки смотрите в diagnostics выше.")


def configure_nvidia_vulkan():
    install_vulkan_runtime()
    icds = _nvidia_icds()
    if not icds:
        raise RuntimeError("NVIDIA Vulkan ICD отсутствует; nvidia-smi не является доказательством Vulkan device")
    os.environ["VK_ICD_FILENAMES"] = os.pathsep.join(str(p) for p in icds)
    os.environ.pop("VK_DRIVER_FILES", None)
    print("VK_ICD_FILENAMES установлен только на NVIDIA ICD:", os.environ["VK_ICD_FILENAMES"])
    code, inventory = _run(["vulkaninfo", "--summary"], env=os.environ.copy())
    print("\nFULL VULKAN CHECK BEFORE RIFE (exit", code, "):\n" + (inventory or "<нет вывода>"))
    names = re.findall(r"(?:deviceName|Device Name)\\s*=\\s*(.+)", inventory)
    if not names:
        names = re.findall(r"GPU\\d+\\s*:\\s*(.+)", inventory)
    names = [name.strip() for name in names]
    nvidia = [name for name in names if "nvidia" in name.lower() and "llvmpipe" not in name.lower()]
    print("Vulkan devices:", names or "<не распознаны>")
    if code or not nvidia:
        _print_diagnostics("Final failed NVIDIA Vulkan diagnostics")
        raise RuntimeError("Vulkan не показывает NVIDIA GPU; llvmpipe запрещён, RIFE остановлен")
    print("Vulkan device selected:", nvidia[0])
    print("RIFE Vulkan device index: 0 (только NVIDIA ICD; llvmpipe не используется)")


def download_video(url):
    print("1. Скачиваем видео...")
    response = requests.get(url, stream=True, timeout=300)
    response.raise_for_status()
    with open(VIDEO_FILE, "wb") as output:
        for chunk in response.iter_content(1024 * 1024):
            if chunk: output.write(chunk)


def install_rife():
    if os.path.exists(RIFE_EXE): return
    run(f"wget -q --show-progress -O '{RIFE_ZIP}' '{RIFE_URL}'", "Скачивание RIFE")
    run(f"unzip -q -o '{RIFE_ZIP}' && chmod +x '{RIFE_EXE}'", "Распаковка RIFE")


def get_video_fps():
    code, value = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", VIDEO_FILE])
    if code: raise RuntimeError("Не удалось определить FPS")
    if "/" in value:
        a, b = value.strip().split("/", 1); return float(a) / float(b)
    return float(value.strip())


def has_audio():
    code, output = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", VIDEO_FILE])
    return not code and bool(output.strip())


def process():
    source_fps = get_video_fps()
    audio = has_audio()
    if audio: run(f"ffmpeg -y -i '{VIDEO_FILE}' -vn -c:a aac -b:a 192k '{AUDIO_FILE}'", "Извлечение аудио")
    shutil.rmtree(INPUT_FRAMES, ignore_errors=True); os.makedirs(INPUT_FRAMES)
    run(f"ffmpeg -y -i '{VIDEO_FILE}' '{INPUT_FRAMES}/frame_%08d.png'", "Извлечение кадров")
    frames = sorted(Path(INPUT_FRAMES).glob("*.png"))
    if not frames: raise RuntimeError("Кадры не были созданы")
    shutil.rmtree(OUTPUT_FRAMES, ignore_errors=True); os.makedirs(OUTPUT_FRAMES)
    if TARGET_FPS <= source_fps:
        for frame in frames: shutil.copy2(frame, Path(OUTPUT_FRAMES) / frame.name)
    else:
        target = round((len(frames) / source_fps) * TARGET_FPS)
        configure_nvidia_vulkan()
        run(f"{RIFE_EXE} -g 0 -i '{INPUT_FRAMES}' -o '{OUTPUT_FRAMES}' -n {target} -m rife-v4.6", "RIFE interpolation")
    command = f"ffmpeg -y -framerate {TARGET_FPS} -i '{OUTPUT_FRAMES}/%08d.png' "
    if audio: command += f"-i '{AUDIO_FILE}' -map 0:v:0 -map 1:a:0 -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -shortest "
    else: command += "-c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p "
    run(command + f"'{OUTPUT_FILE}'", "Создание MP4")


def send_result():
    with open(OUTPUT_FILE, "rb") as video:
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo", data={"chat_id": CHAT_ID}, files={"video": ("rife_output.mp4", video, "video/mp4")}, timeout=600)
    if not response.ok: raise RuntimeError("Telegram не смог принять видео: " + response.text)


def send_error(error):
    try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": f"❌ Ошибка обработки:\n\n{str(error)[:3500]}"}, timeout=30)
    except Exception: pass


def main():
    try:
        print("RIFE TELEGRAM WORKER", "Target FPS:", TARGET_FPS)
        code, output = _run(["nvidia-smi"]); print("nvidia-smi (exit", code, "):\n", output)
        download_video(VIDEO_URL); install_rife(); process(); send_result(); print("ГОТОВО")
    except Exception as error:
        print("ОШИБКА:", error); send_error(error); raise
    finally:
        for folder in (INPUT_FRAMES, OUTPUT_FRAMES): shutil.rmtree(folder, ignore_errors=True)
        for filename in (VIDEO_FILE, AUDIO_FILE, OUTPUT_FILE, RIFE_ZIP):
            try: os.remove(filename)
            except FileNotFoundError: pass


if __name__ == "__main__": main()
'''


def build_worker_code(video_url, chat_id, target_fps):
    config = {"VIDEO_URL": video_url, "CHAT_ID": str(chat_id), "BOT_TOKEN": BOT_TOKEN, "TARGET_FPS": int(target_fps)}
    return WORKER_TEMPLATE.replace("__JOB_CONFIG__", json.dumps(config, ensure_ascii=False))


def prepare_kernel(video_url, chat_id, target_fps):
    shutil.rmtree(WORKER_DIR, ignore_errors=True)
    WORKER_DIR.mkdir(parents=True)
    (WORKER_DIR / "rife-worker.py").write_text(build_worker_code(video_url, chat_id, target_fps), encoding="utf-8")
    (WORKER_DIR / "requirements.txt").write_text("requests\n", encoding="utf-8")
    metadata = {"id": KERNEL_ID, "title": "rife-worker", "code_file": "rife-worker.py", "language": "python", "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_internet": True}
    (WORKER_DIR / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def push_kaggle_job(video_url, chat_id, target_fps):
    prepare_kernel(video_url, chat_id, target_fps)
    api.kernels_push(str(WORKER_DIR))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет!\n\nОтправь мне видео, которое нужно улучшить с помощью RIFE.")


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.video:
        user_videos[update.effective_user.id] = {"file_id": update.message.video.file_id}
        await update.message.reply_text("🎬 Видео получено!\n\nДо скольки FPS улучшить?\n\nНапример: 60")


async def fps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_videos:
        await update.message.reply_text("Сначала отправь видео."); return
    try: target_fps = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ FPS должен быть числом.\nНапример: 60"); return
    if not 1 <= target_fps <= 240:
        await update.message.reply_text("❌ FPS должен быть от 1 до 240."); return
    await update.message.reply_text("⏳ Подготавливаю задачу для Kaggle...")
    try:
        file = await context.bot.get_file(user_videos[user_id]["file_id"])
        file_url = file.file_path if file.file_path.startswith(("http://", "https://")) else f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        await asyncio.to_thread(push_kaggle_job, file_url, update.effective_chat.id, target_fps)
        await update.message.reply_text(f"✅ Задача отправлена в Kaggle!\n\n🎞 Целевой FPS: {target_fps}")
        del user_videos[user_id]
    except Exception as error:
        logger.exception("Ошибка отправки задачи в Kaggle")
        await update.message.reply_text(f"❌ Не удалось отправить задачу в Kaggle.\n\n{type(error).__name__}: {error}")


def main():
    threading.Thread(target=start_http_server, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fps_handler))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__": main()
