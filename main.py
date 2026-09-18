import os
import json
import logging
import asyncio
import threading
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from kaggle.api.kaggle_api_extended import KaggleApi

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME", "kdidid")
KAGGLE_API_TOKEN = os.getenv("KAGGLE_API_TOKEN")

PORT = int(os.getenv("PORT", "10000"))

KERNEL_ID = f"{KAGGLE_USERNAME}/rife-worker"

WORKER_DIR = Path("./kaggle_worker")


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN")

if not KAGGLE_API_TOKEN:
    raise RuntimeError("Не найден KAGGLE_API_TOKEN")


# ============================================================
# KAGGLE API
# ============================================================

os.environ["KAGGLE_API_TOKEN"] = KAGGLE_API_TOKEN
os.environ["KAGGLE_USERNAME"] = KAGGLE_USERNAME


api = KaggleApi()

api.authenticate()


# ============================================================
# HTTP SERVER ДЛЯ RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

        self.wfile.write(
            b"RIFE Telegram Bot is running"
        )

    def log_message(self, format, *args):
        pass


def start_http_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    logger.info(
        f"HTTP server started on port {PORT}"
    )

    server.serve_forever()


# ============================================================
# СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================

user_videos = {}


# ============================================================
# WORKER CODE
# ============================================================

WORKER_CODE = r'''
import os
import json
import shutil
import subprocess
import requests
from pathlib import Path

CONFIG_FILE = "job_config.json"

RIFE_URL = (
    "https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
    "20221029/rife-ncnn-vulkan-20221029-ubuntu.zip"
)

RIFE_ZIP = "rife.zip"
RIFE_DIR = "rife-ncnn-vulkan-20221029-ubuntu"
RIFE_EXE = f"./{RIFE_DIR}/rife-ncnn-vulkan"

VIDEO_FILE = "input.mp4"
AUDIO_FILE = "audio.m4a"

INPUT_FRAMES = "input_frames"
OUTPUT_FRAMES = "output_frames"

OUTPUT_FILE = "output.mp4"


def run(command, name):

    print()
    print("=" * 60)
    print(name)
    print("=" * 60)
    print(command)
    print()

    result = subprocess.run(
        command,
        shell=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Ошибка: {name}"
        )


def load_config():

    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(
            "job_config.json не найден"
        )

    with open(
        CONFIG_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def download_video(video_url):

    print("1. Скачиваем видео...")

    response = requests.get(
        video_url,
        stream=True,
        timeout=300
    )

    response.raise_for_status()

    with open(
        VIDEO_FILE,
        "wb"
    ) as f:

        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):

            if chunk:
                f.write(chunk)

    print(
        f"Видео скачано: "
        f"{os.path.getsize(VIDEO_FILE) / 1024 / 1024:.2f} MB"
    )


def install_rife():

    print("2. Устанавливаем RIFE...")

    if os.path.exists(RIFE_EXE):

        print("RIFE уже установлен.")

        return

    run(
        f"wget -q --show-progress "
        f"-O {RIFE_ZIP} '{RIFE_URL}'",
        "Скачивание RIFE"
    )

    run(
        f"unzip -q -o {RIFE_ZIP}",
        "Распаковка RIFE"
    )

    if not os.path.exists(RIFE_EXE):

        raise RuntimeError(
            "RIFE executable не найден"
        )

    run(
        f"chmod +x {RIFE_EXE}",
        "Права запуска RIFE"
    )


def get_video_fps():

    print("3. Определяем FPS исходного видео...")

    command = (
        "ffprobe -v error "
        "-select_streams v:0 "
        "-show_entries stream=r_frame_rate "
        "-of default=noprint_wrappers=1:nokey=1 "
        f"'{VIDEO_FILE}'"
    )

    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Не удалось определить FPS"
        )

    value = result.stdout.strip()

    if "/" in value:

        a, b = value.split("/")

        fps = float(a) / float(b)

    else:

        fps = float(value)

    print(f"Исходный FPS: {fps}")

    return fps


def has_audio():

    command = (
        "ffprobe -v error "
        "-select_streams a:0 "
        "-show_entries stream=index "
        "-of csv=p=0 "
        f"'{VIDEO_FILE}'"
    )

    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True
    )

    return bool(
        result.stdout.strip()
    )


def extract_audio():

    if not has_audio():

        print("Аудио отсутствует.")

        return False

    print("4. Извлекаем аудио...")

    run(
        (
            "ffmpeg -y "
            f"-i '{VIDEO_FILE}' "
            "-vn "
            "-c:a aac "
            "-b:a 192k "
            f"'{AUDIO_FILE}'"
        ),
        "Извлечение аудио"
    )

    return True


def extract_frames():

    print("5. Извлекаем кадры...")

    if os.path.exists(INPUT_FRAMES):
        shutil.rmtree(INPUT_FRAMES)

    os.makedirs(INPUT_FRAMES)

    run(
        (
            "ffmpeg -y "
            f"-i '{VIDEO_FILE}' "
            f"'{INPUT_FRAMES}/frame_%08d.png'"
        ),
        "Извлечение кадров"
    )

    frames = sorted(
        Path(INPUT_FRAMES).glob("*.png")
    )

    if not frames:

        raise RuntimeError(
            "Кадры не были созданы"
        )

    print(
        f"Получено кадров: {len(frames)}"
    )

    return len(frames)


def run_rife(
    source_fps,
    source_frames,
    target_fps
):

    print(
        f"6. Обработка RIFE: "
        f"{source_fps} → {target_fps} FPS"
    )

    if os.path.exists(OUTPUT_FRAMES):
        shutil.rmtree(OUTPUT_FRAMES)

    os.makedirs(OUTPUT_FRAMES)

    if target_fps <= source_fps:

        print(
            "Целевой FPS не выше исходного."
        )

        for frame in sorted(
            Path(INPUT_FRAMES).glob("*.png")
        ):

            shutil.copy2(
                frame,
                Path(OUTPUT_FRAMES) / frame.name
            )

        return

    duration = source_frames / source_fps

    target_frames = round(
        duration * target_fps
    )

    print(
        f"Целевое количество кадров: "
        f"{target_frames}"
    )

    command = (
        f"{RIFE_EXE} "
        f"-i '{INPUT_FRAMES}' "
        f"-o '{OUTPUT_FRAMES}' "
        f"-n {target_frames} "
        "-m rife-v4.6"
    )

    run(
        command,
        "RIFE interpolation"
    )

    output_frames = sorted(
        Path(OUTPUT_FRAMES).glob("*.png")
    )

    if not output_frames:

        raise RuntimeError(
            "RIFE не создал выходные кадры"
        )

    print(
        f"Получено выходных кадров: "
        f"{len(output_frames)}"
    )


def encode_video(
    target_fps,
    audio
):

    print("7. Создаём итоговый MP4...")

    if os.path.exists(OUTPUT_FILE):
        os.remove(OUTPUT_FILE)

    if audio:

        command = (
            "ffmpeg -y "
            f"-framerate {target_fps} "
            f"-i '{OUTPUT_FRAMES}/%08d.png' "
            f"-i '{AUDIO_FILE}' "
            "-map 0:v:0 "
            "-map 1:a:0 "
            "-c:v libx264 "
            "-preset veryfast "
            "-crf 18 "
            "-pix_fmt yuv420p "
            "-c:a aac "
            "-b:a 192k "
            "-shortest "
            f"'{OUTPUT_FILE}'"
        )

    else:

        command = (
            "ffmpeg -y "
            f"-framerate {target_fps} "
            f"-i '{OUTPUT_FRAMES}/%08d.png' "
            "-c:v libx264 "
            "-preset veryfast "
            "-crf 18 "
            "-pix_fmt yuv420p "
            f"'{OUTPUT_FILE}'"
        )

    run(
        command,
        "Создание MP4"
    )


def send_result(
    bot_token,
    chat_id
):

    print("8. Отправляем видео в Telegram...")

    url = (
        f"https://api.telegram.org/"
        f"bot{bot_token}/sendVideo"
    )

    with open(
        OUTPUT_FILE,
        "rb"
    ) as video:

        response = requests.post(
            url,
            data={
                "chat_id": chat_id
            },
            files={
                "video": (
                    "rife_output.mp4",
                    video,
                    "video/mp4"
                )
            },
            timeout=600
        )

    print(response.text)

    if not response.ok:

        raise RuntimeError(
            "Telegram не смог принять видео"
        )


def send_error(
    bot_token,
    chat_id,
    error
):

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{bot_token}/sendMessage"
        )

        requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": (
                    "❌ Ошибка обработки:\n\n"
                    f"{str(error)[:3500]}"
                )
            },
            timeout=30
        )

    except Exception:
        pass


def cleanup():

    for folder in [
        INPUT_FRAMES,
        OUTPUT_FRAMES
    ]:

        if os.path.exists(folder):
            shutil.rmtree(folder)

    for file in [
        VIDEO_FILE,
        AUDIO_FILE,
        OUTPUT_FILE,
        RIFE_ZIP
    ]:

        if os.path.exists(file):
            os.remove(file)


def main():

    config = load_config()

    video_url = config["VIDEO_URL"]
    chat_id = config["CHAT_ID"]
    bot_token = config["BOT_TOKEN"]

    target_fps = int(
        config["TARGET_FPS"]
    )

    print("=" * 60)
    print("RIFE TELEGRAM WORKER")
    print("=" * 60)
    print(f"Target FPS: {target_fps}")
    print("=" * 60)

    try:

        download_video(video_url)

        install_rife()

        source_fps = get_video_fps()

        audio = extract_audio()

        source_frames = extract_frames()

        run_rife(
            source_fps,
            source_frames,
            target_fps
        )

        encode_video(
            target_fps,
            audio
        )

        send_result(
            bot_token,
            chat_id
        )

        print()
        print("=" * 60)
        print("ГОТОВО")
        print("=" * 60)

    except Exception as e:

        print(
            f"ОШИБКА: {e}"
        )

        send_error(
            bot_token,
            chat_id,
            e
        )

        raise

    finally:

        cleanup()


if __name__ == "__main__":
    main()
'''


# ============================================================
# СОЗДАЁМ KERNEL PROJECT
# ============================================================

def prepare_kernel(
    video_url,
    chat_id,
    target_fps
):

    if WORKER_DIR.exists():

        shutil.rmtree(WORKER_DIR)

    WORKER_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # --------------------------------------------------------
    # worker.py
    # --------------------------------------------------------

    worker_file = (
        WORKER_DIR / "rife-worker.py"
    )

    worker_file.write_text(
        WORKER_CODE,
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # job_config.json
    # --------------------------------------------------------

    config = {
        "VIDEO_URL": video_url,
        "CHAT_ID": str(chat_id),
        "BOT_TOKEN": BOT_TOKEN,
        "TARGET_FPS": str(target_fps)
    }

    config_file = (
        WORKER_DIR / "job_config.json"
    )

    config_file.write_text(
        json.dumps(
            config,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # requirements.txt
    # --------------------------------------------------------

    requirements = (
        "requests\n"
    )

    (
        WORKER_DIR / "requirements.txt"
    ).write_text(
        requirements,
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # kernel-metadata.json
    # --------------------------------------------------------

    metadata = {
        "id": KERNEL_ID,
        "title": "rife-worker",
        "code_file": "rife-worker.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": "true",
        "enable_gpu": "true",
        "enable_internet": "true"
    }

    (
        WORKER_DIR / "kernel-metadata.json"
    ).write_text(
        json.dumps(
            metadata,
            indent=2
        ),
        encoding="utf-8"
    )

    logger.info(
        "Kaggle Kernel project prepared"
    )


# ============================================================
# ОТПРАВЛЯЕМ JOB В KAGGLE
# ============================================================

def push_kaggle_job(
    video_url,
    chat_id,
    target_fps
):

    logger.info(
        f"Отправляем job в Kaggle: "
        f"{KERNEL_ID}"
    )

    prepare_kernel(
        video_url,
        chat_id,
        target_fps
    )

    # ВАЖНО:
    # НИКАКОГО kernels_pull() ЗДЕСЬ НЕТ.
    # Мы сразу отправляем нашу новую версию Kernel.

    api.kernels_push(str(WORKER_DIR))

    logger.info(
        "Kaggle job успешно отправлен"
    )


# ============================================================
# /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Отправь мне видео, которое нужно "
        "улучшить с помощью RIFE."
    )


# ============================================================
# ПОЛУЧЕНИЕ ВИДЕО
# ============================================================

async def video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    video = update.message.video

    if not video:

        return

    user_id = update.effective_user.id

    user_videos[user_id] = {
        "file_id": video.file_id
    }

    await update.message.reply_text(
        "🎬 Видео получено!\n\n"
        "До скольки FPS улучшить?\n\n"
        "Например: 60"
    )


# ============================================================
# ПОЛУЧЕНИЕ FPS
# ============================================================

async def fps_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if user_id not in user_videos:

        await update.message.reply_text(
            "Сначала отправь видео."
        )

        return

    text = update.message.text.strip()

    try:

        target_fps = int(text)

    except ValueError:

        await update.message.reply_text(
            "❌ FPS должен быть числом.\n"
            "Например: 60"
        )

        return

    if target_fps < 1 or target_fps > 240:

        await update.message.reply_text(
            "❌ FPS должен быть от 1 до 240."
        )

        return

    await update.message.reply_text(
        "⏳ Подготавливаю задачу для Kaggle..."
    )

    try:

        video_file_id = user_videos[
            user_id
        ]["file_id"]

        telegram_file = await context.bot.get_file(
            video_file_id
        )

        file_url = (
            f"https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/"
            f"{telegram_file.file_path}"
        )

        chat_id = update.effective_chat.id

        await asyncio.to_thread(
            push_kaggle_job,
            file_url,
            chat_id,
            target_fps
        )

        await update.message.reply_text(
            "✅ Задача отправлена в Kaggle!\n\n"
            f"🎞 Целевой FPS: {target_fps}\n\n"
            "Когда обработка закончится, "
            "я отправлю готовое видео сюда."
        )

        del user_videos[user_id]

    except Exception as e:

        logger.exception(
            "Ошибка отправки задачи в Kaggle"
        )

        await update.message.reply_text(
            "❌ Не удалось отправить задачу "
            "в Kaggle.\n\n"
            f"{type(e).__name__}: {e}"
        )


# ============================================================
# ЗАПУСК TELEGRAM
# ============================================================

def main():

    logger.info("=" * 40)
    logger.info("Запуск FlowFrames Bot...")
    logger.info(
        f"Kaggle Kernel: {KERNEL_ID}"
    )
    logger.info(
        f"HTTP Port: {PORT}"
    )
    logger.info("=" * 40)

    # HTTP server для Render
    threading.Thread(
        target=start_http_server,
        daemon=True
    ).start()

    # Telegram
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            video_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            fps_handler
        )
    )

    logger.info(
        "FlowFrames Bot запущен!"
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()
