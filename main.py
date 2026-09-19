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
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("HTTP server started on port %s", PORT)
    server.serve_forever()


user_videos = {}


WORKER_TEMPLATE = r'''import os
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
    print("\n" + "=" * 60)
    print(name)
    print(command)
    print("=" * 60)
    result = subprocess.run(command, shell=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка: {name}")


def check_environment():
    print("Текущая директория:", Path.cwd())
    print("Python:", os.sys.version)
    for command in ("which ffmpeg", "which ffprobe"):
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        print(command, ":", result.stdout.strip() or "НЕ НАЙДЕН")
    result = subprocess.run("nvidia-smi", shell=True, capture_output=True, text=True)
    print("GPU обнаружен:" if result.returncode == 0 else "nvidia-smi не сработал.")
    if result.returncode == 0:
        print(result.stdout)


def download_video(video_url):
    print("1. Скачиваем видео...")
    response = requests.get(video_url, stream=True, timeout=300)
    response.raise_for_status()
    with open(VIDEO_FILE, "wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                output.write(chunk)
    print(f"Видео скачано: {os.path.getsize(VIDEO_FILE) / 1024 / 1024:.2f} MB")


def install_rife():
    print("2. Устанавливаем RIFE...")
    if os.path.exists(RIFE_EXE):
        return
    run(f"wget -q --show-progress -O '{RIFE_ZIP}' '{RIFE_URL}'", "Скачивание RIFE")
    run(f"unzip -q -o '{RIFE_ZIP}'", "Распаковка RIFE")
    if not os.path.exists(RIFE_EXE):
        raise RuntimeError("RIFE executable не найден")
    run(f"chmod +x '{RIFE_EXE}'", "Права запуска RIFE")


def install_vulkan_runtime():
    print("6. Устанавливаем Vulkan runtime...")
    run("export DEBIAN_FRONTEND=noninteractive && apt-get update -y && apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers vulkan-tools && ldconfig", "Установка Vulkan runtime")
    result = subprocess.run("ldconfig -p | grep libvulkan.so.1", shell=True, capture_output=True, text=True)
    print("Проверка libvulkan.so.1:", result.stdout.strip())
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("После установки не найден libvulkan.so.1")


def configure_nvidia_vulkan():
    """List Vulkan devices, then force RIFE onto the NVIDIA ICD/device."""
    result = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True)
    inventory = result.stdout + "\n" + result.stderr
    if result.returncode != 0 and not inventory.strip():
        raise RuntimeError("Не удалось получить список Vulkan devices через vulkaninfo")

    names = re.findall(r"(?:deviceName|Device Name)\s*=\s*(.+)", inventory)
    if not names:
        names = re.findall(r"GPU\d+\s*:\s*(.+)", inventory)
    print("Доступные Vulkan devices:")
    for index, name in enumerate(names):
        print(f"  Vulkan device {index}: {name.strip()}")
    if not names:
        print(inventory)

    nvidia_names = [name.strip() for name in names if "nvidia" in name.lower()]
    if not nvidia_names:
        raise RuntimeError("NVIDIA Vulkan device недоступен; остановка вместо запуска через llvmpipe")

    icd_dirs = (Path("/usr/share/vulkan/icd.d"), Path("/etc/vulkan/icd.d"))
    icd_files = []
    for directory in icd_dirs:
        if directory.exists():
            icd_files.extend(sorted(directory.glob("*nvidia*.json")))
    if not icd_files:
        raise RuntimeError("Найден NVIDIA Vulkan device, но NVIDIA ICD не найден")

    # With only the NVIDIA ICD visible, ncnn's -g 0 cannot select llvmpipe.
    os.environ["VK_ICD_FILENAMES"] = os.pathsep.join(str(path) for path in icd_files)
    forced = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, env=os.environ)
    forced_inventory = forced.stdout + "\n" + forced.stderr
    forced_names = re.findall(r"(?:deviceName|Device Name)\s*=\s*(.+)", forced_inventory)
    if not any("nvidia" in name.lower() for name in forced_names):
        raise RuntimeError("NVIDIA Vulkan ICD не предоставил usable device; llvmpipe запрещён")
    selected = next(name.strip() for name in forced_names if "nvidia" in name.lower())
    print(f"Vulkan device selected: {selected}")
    print("RIFE Vulkan device index: 0 (NVIDIA ICD forced)")


def get_video_fps():
    command = "ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of default=noprint_wrappers=1:nokey=1 '" + VIDEO_FILE + "'"
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Не удалось определить FPS")
    value = result.stdout.strip()
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        fps = float(numerator) / float(denominator)
    else:
        fps = float(value)
    print(f"Исходный FPS: {fps}")
    return fps


def has_audio():
    result = subprocess.run("ffprobe -v error -select_streams a:0 -show_entries stream=index -of csv=p=0 '" + VIDEO_FILE + "'", shell=True, capture_output=True, text=True)
    return bool(result.stdout.strip())


def extract_audio():
    if not has_audio():
        print("Аудио отсутствует.")
        return False
    run(f"ffmpeg -y -i '{VIDEO_FILE}' -vn -c:a aac -b:a 192k '{AUDIO_FILE}'", "Извлечение аудио")
    return True


def extract_frames():
    print("5. Извлекаем кадры...")
    if os.path.exists(INPUT_FRAMES):
        shutil.rmtree(INPUT_FRAMES)
    os.makedirs(INPUT_FRAMES)
    run(f"ffmpeg -y -i '{VIDEO_FILE}' '{INPUT_FRAMES}/frame_%08d.png'", "Извлечение кадров")
    frames = sorted(Path(INPUT_FRAMES).glob("*.png"))
    if not frames:
        raise RuntimeError("Кадры не были созданы")
    print(f"Получено кадров: {len(frames)}")
    return len(frames)


def run_rife(source_fps, source_frames, target_fps):
    print(f"6. Обработка RIFE: {source_fps} → {target_fps} FPS")
    if os.path.exists(OUTPUT_FRAMES):
        shutil.rmtree(OUTPUT_FRAMES)
    os.makedirs(OUTPUT_FRAMES)
    if target_fps <= source_fps:
        print("Целевой FPS не выше исходного. RIFE не требуется.")
        for frame in sorted(Path(INPUT_FRAMES).glob("*.png")):
            shutil.copy2(frame, Path(OUTPUT_FRAMES) / frame.name)
        return
    target_frames = round((source_frames / source_fps) * target_fps)
    print(f"Целевое количество кадров: {target_frames}")
    install_vulkan_runtime()
    configure_nvidia_vulkan()
    run(f"{RIFE_EXE} -g 0 -i '{INPUT_FRAMES}' -o '{OUTPUT_FRAMES}' -n {target_frames} -m rife-v4.6", "RIFE interpolation")
    output_frames = sorted(Path(OUTPUT_FRAMES).glob("*.png"))
    if not output_frames:
        raise RuntimeError("RIFE не создал выходных кадры")
    print(f"Получено выходных кадров: {len(output_frames)}")


def encode_video(target_fps, audio):
    print("7. Создаём итоговый MP4...")
    if os.path.exists(OUTPUT_FILE):
        os.remove(OUTPUT_FILE)
    command = f"ffmpeg -y -framerate {target_fps} -i '{OUTPUT_FRAMES}/%08d.png' "
    if audio:
        command += f"-i '{AUDIO_FILE}' -map 0:v:0 -map 1:a:0 -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -shortest "
    else:
        command += "-c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p "
    run(command + f"'{OUTPUT_FILE}'", "Создание MP4")


def send_result():
    print("8. Отправляем видео в Telegram...")
    with open(OUTPUT_FILE, "rb") as video:
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo", data={"chat_id": CHAT_ID}, files={"video": ("rife_output.mp4", video, "video/mp4")}, timeout=600)
    print(response.text)
    if not response.ok:
        raise RuntimeError("Telegram не смог принять видео")


def send_error(error):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": f"❌ Ошибка обработки:\n\n{str(error)[:3500]}"}, timeout=30)
    except Exception:
        pass


def cleanup():
    for folder in (INPUT_FRAMES, OUTPUT_FRAMES):
        if os.path.exists(folder):
            shutil.rmtree(folder)
    for filename in (VIDEO_FILE, AUDIO_FILE, OUTPUT_FILE, RIFE_ZIP):
        if os.path.exists(filename):
            os.remove(filename)


def main():
    print("RIFE TELEGRAM WORKER")
    print(f"Target FPS: {TARGET_FPS}")
    try:
        check_environment()
        download_video(VIDEO_URL)
        install_rife()
        source_fps = get_video_fps()
        audio = extract_audio()
        source_frames = extract_frames()
        run_rife(source_fps, source_frames, TARGET_FPS)
        encode_video(TARGET_FPS, audio)
        send_result()
        print("ГОТОВО")
    except Exception as error:
        print(f"ОШИБКА: {error}")
        send_error(error)
        raise
    finally:
        cleanup()


if __name__ == "__main__":
    main()
'''


def build_worker_code(video_url, chat_id, target_fps):
    worker_config = {"VIDEO_URL": video_url, "CHAT_ID": str(chat_id), "BOT_TOKEN": BOT_TOKEN, "TARGET_FPS": int(target_fps)}
    code = WORKER_TEMPLATE.replace("__JOB_CONFIG__", json.dumps(worker_config, ensure_ascii=False))
    if "load_config()" in code or "job_config.json" in code:
        raise RuntimeError("Сформирован устаревший Kaggle worker")
    return code


def prepare_kernel(video_url, chat_id, target_fps):
    if WORKER_DIR.exists():
        if not WORKER_DIR.is_dir():
            WORKER_DIR.unlink()
        else:
            shutil.rmtree(WORKER_DIR)
    WORKER_DIR.mkdir(parents=True, exist_ok=False)
    worker_path = WORKER_DIR / "rife-worker.py"
    metadata_path = WORKER_DIR / "kernel-metadata.json"
    requirements_path = WORKER_DIR / "requirements.txt"
    worker_code = build_worker_code(video_url, chat_id, target_fps)
    worker_path.write_text(worker_code, encoding="utf-8")
    requirements_path.write_text("requests\n", encoding="utf-8")
    metadata = {"id": KERNEL_ID, "title": "rife-worker", "code_file": "rife-worker.py", "language": "python", "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_internet": True, "machine_shape": "NvidiaTeslaT4"}
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    expected_files = {"rife-worker.py", "requirements.txt", "kernel-metadata.json"}
    actual_files = {path.name for path in WORKER_DIR.iterdir() if path.is_file()}
    if actual_files != expected_files:
        raise RuntimeError(f"Неверное содержимое Kaggle project: {actual_files}")
    if "load_config()" in worker_code or "job_config.json" in worker_code:
        raise RuntimeError("В worker обнаружена старая конфигурация")
    loaded_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if loaded_metadata["code_file"] != "rife-worker.py" or loaded_metadata["kernel_type"] != "script":
        raise RuntimeError("Некорректные Kaggle metadata")
    if loaded_metadata["enable_gpu"] is not True or loaded_metadata["enable_internet"] is not True:
        raise RuntimeError("GPU и Internet должны быть включены")
    logger.info("Fresh Kaggle project prepared: %s; files=%s", WORKER_DIR, sorted(actual_files))


def push_kaggle_job(video_url, chat_id, target_fps):
    prepare_kernel(video_url, chat_id, target_fps)
    api.kernels_push(str(WORKER_DIR))
    logger.info("Kaggle job успешно отправлен: %s", KERNEL_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет!\n\nОтправь мне видео, которое нужно улучшить с помощью RIFE.")


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    if not video:
        return
    user_videos[update.effective_user.id] = {"file_id": video.file_id}
    await update.message.reply_text("🎬 Видео получено!\n\nДо скольки FPS улучшить?\n\nНапример: 60")


async def fps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_videos:
        await update.message.reply_text("Сначала отправь видео.")
        return
    try:
        target_fps = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ FPS должен быть числом.\nНапример: 60")
        return
    if not 1 <= target_fps <= 240:
        await update.message.reply_text("❌ FPS должен быть от 1 до 240.")
        return
    await update.message.reply_text("⏳ Подготавливаю задачу для Kaggle...")
    try:
        telegram_file = await context.bot.get_file(user_videos[user_id]["file_id"])
        file_path = telegram_file.file_path
        if file_path.startswith(("http://", "https://")):
            file_url = file_path
        else:
            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        await asyncio.to_thread(push_kaggle_job, file_url, update.effective_chat.id, target_fps)
        await update.message.reply_text(f"✅ Задача отправлена в Kaggle!\n\n🎞 Целевой FPS: {target_fps}\n\nКогда обработка закончится, я отправлю готовое видео сюда.")
        del user_videos[user_id]
    except Exception as error:
        logger.exception("Ошибка отправки задачи в Kaggle")
        await update.message.reply_text(f"❌ Не удалось отправить задачу в Kaggle.\n\n{type(error).__name__}: {error}")


def main():
    logger.info("Запуск RIFE Telegram Bot; Kaggle Kernel: %s", KERNEL_ID)
    threading.Thread(target=start_http_server, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fps_handler))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
