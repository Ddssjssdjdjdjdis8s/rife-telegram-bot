import os
import json
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# KAGGLE CREDENTIALS
# ============================================================

os.environ["KAGGLE_USERNAME"] = os.getenv("KAGGLE_USERNAME", "")
os.environ["KAGGLE_KEY"] = os.getenv("KAGGLE_KEY", "")

# ============================================================
# TELEGRAM
# ============================================================

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ============================================================
# KAGGLE
# ============================================================

from kaggle.api.kaggle_api_extended import KaggleApi


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME")
KAGGLE_KEY = os.getenv("KAGGLE_KEY")

PORT = int(os.getenv("PORT", "10000"))

KERNEL_ID = f"{KAGGLE_USERNAME}/rife-worker"
KERNEL_PATH = "./kernel_temp"


# ============================================================
# ПРОВЕРКА ENVIRONMENT VARIABLES
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN в Render Environment Variables"
    )

if not KAGGLE_USERNAME:
    raise RuntimeError(
        "Не задан KAGGLE_USERNAME в Render Environment Variables"
    )

if not KAGGLE_KEY:
    raise RuntimeError(
        "Не задан KAGGLE_KEY в Render Environment Variables"
    )


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# ============================================================
# KAGGLE API
# ============================================================

api = KaggleApi()
api.authenticate()


# ============================================================
# СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ
#
# chat_id -> {
#     "video_file_id": "...",
#     "video_file_path": "..."
# }
# ============================================================

user_states = {}


# ============================================================
# HTTP SERVER ДЛЯ RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"FlowFrames Bot is running!"
        )

    def do_HEAD(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

    def log_message(self, format, *args):
        return


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
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    chat_id = update.message.chat_id

    user_states[chat_id] = {
        "waiting_for_video": True,
        "waiting_for_fps": False,
        "video_file_id": None
    }

    await update.message.reply_text(
        "👋 Привет!\n\n"
        "🎬 Пришли сюда видео, которое хочешь "
        "обработать через RIFE.\n\n"
        "После этого я спрошу, до скольки FPS "
        "нужно увеличить видео."
    )


# ============================================================
# ПОЛУЧЕНИЕ ВИДЕО
# ============================================================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.video:
        return

    chat_id = update.message.chat_id

    video = update.message.video

    # Создаём состояние, если пользователь сразу отправил видео
    if chat_id not in user_states:

        user_states[chat_id] = {
            "waiting_for_video": False,
            "waiting_for_fps": False,
            "video_file_id": None
        }

    # Сохраняем ID видео
    user_states[chat_id]["video_file_id"] = video.file_id

    user_states[chat_id]["waiting_for_video"] = False
    user_states[chat_id]["waiting_for_fps"] = True

    # Информация о видео
    size_mb = 0

    if video.file_size:
        size_mb = video.file_size / 1024 / 1024

    await update.message.reply_text(
        "🎬 Видео получено!\n\n"
        f"📦 Размер: {size_mb:.1f} MB\n\n"
        "🎯 До скольки FPS улучшить видео?\n\n"
        "Например:\n"
        "60\n\n"
        "Можно написать 60, 120, 144, 240 и т. д."
    )


# ============================================================
# ПОЛУЧЕНИЕ FPS
# ============================================================

async def handle_fps(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id

    text = update.message.text.strip()

    # Проверяем, действительно ли пользователь ждёт FPS
    state = user_states.get(chat_id)

    if not state or not state.get("waiting_for_fps"):

        await update.message.reply_text(
            "ℹ️ Сначала отправь /start, "
            "а затем видео."
        )

        return

    # Проверяем число
    if not text.isdigit():

        await update.message.reply_text(
            "❌ FPS должен быть числом.\n\n"
            "Например: 60"
        )

        return

    target_fps = int(text)

    # Ограничиваем допустимый диапазон
    if target_fps < 1 or target_fps > 240:

        await update.message.reply_text(
            "❌ Укажи FPS от 1 до 240.\n\n"
            "Например: 60"
        )

        return

    video_file_id = state.get("video_file_id")

    if not video_file_id:

        await update.message.reply_text(
            "❌ Я не нашёл сохранённое видео.\n\n"
            "Отправь /start и попробуй ещё раз."
        )

        user_states.pop(chat_id, None)

        return

    # Удаляем состояние ожидания
    user_states[chat_id]["waiting_for_fps"] = False

    await update.message.reply_text(
        f"🎯 Целевой FPS: {target_fps}\n\n"
        "☁️ Подготавливаю задачу для Kaggle...\n"
        "⏳ Это может занять некоторое время."
    )

    # Передаём работу в отдельный поток,
    # чтобы Telegram polling не зависал
    asyncio.create_task(
        submit_kaggle_job(
            update,
            context,
            video_file_id,
            target_fps
        )
    )


# ============================================================
# ОТПРАВКА ЗАДАЧИ В KAGGLE
# ============================================================

async def submit_kaggle_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    video_file_id: str,
    target_fps: int
):

    chat_id = update.effective_chat.id

    try:

        logger.info(
            f"Получаем Telegram file для chat {chat_id}"
        )

        # Получаем Telegram File
        video_file = await context.bot.get_file(
            video_file_id
        )

        file_path = video_file.file_path

        # Telegram Bot API URL
        file_url = (
            "https://api.telegram.org/file/bot"
            f"{BOT_TOKEN}/{file_path}"
        )

        logger.info(
            f"Telegram file path: {file_path}"
        )

        # Работа с Kaggle API является синхронной,
        # поэтому переносим её в отдельный поток
        await asyncio.to_thread(
            create_kaggle_job,
            file_url,
            chat_id,
            target_fps
        )

        await update.message.reply_text(
            "✅ Задача отправлена в Kaggle!\n\n"
            f"🎞 Целевой FPS: {target_fps}\n\n"
            "⚙️ RIFE сейчас обрабатывает видео.\n"
            "Когда обработка закончится, готовое "
            "видео придёт сюда автоматически."
        )

    except Exception as e:

        logger.exception(
            "Ошибка при отправке задачи в Kaggle"
        )

        await update.message.reply_text(
            "❌ Не удалось отправить задачу в Kaggle.\n\n"
            f"{type(e).__name__}: {e}"
        )


# ============================================================
# СОЗДАНИЕ KAGGLE JOB
# ============================================================

def create_kaggle_job(
    file_url: str,
    chat_id: int,
    target_fps: int
):

    logger.info(
        f"Подготавливаем Kaggle Kernel: {KERNEL_ID}"
    )

    os.makedirs(
        KERNEL_PATH,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Загружаем существующий kernel
    # --------------------------------------------------------

    api.kernels_pull(
        KERNEL_ID,
        path=KERNEL_PATH
    )

    # --------------------------------------------------------
    # Конфигурация
    # --------------------------------------------------------

    job_config = {

        "VIDEO_URL": file_url,

        "CHAT_ID": str(chat_id),

        "BOT_TOKEN": BOT_TOKEN,

        "TARGET_FPS": str(target_fps),

        "EXTRA_ARGS": ""
    }

    config_path = os.path.join(
        KERNEL_PATH,
        "job_config.json"
    )

    with open(
        config_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            job_config,
            f,
            ensure_ascii=False,
            indent=2
        )

    logger.info(
        "job_config.json создан"
    )

    # --------------------------------------------------------
    # Отправляем Kernel обратно в Kaggle
    # --------------------------------------------------------

    api.kernels_push(
        KERNEL_PATH
    )

    logger.info(
        "Kaggle Kernel успешно отправлен"
    )


# ============================================================
# ОБРАБОТКА ОШИБОК
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Telegram error:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "========================================"
    )

    logger.info(
        "Запуск FlowFrames Bot..."
    )

    logger.info(
        f"Kaggle Kernel: {KERNEL_ID}"
    )

    logger.info(
        f"HTTP Port: {PORT}"
    )

    logger.info(
        "========================================"
    )

    # --------------------------------------------------------
    # HTTP server для Render
    # --------------------------------------------------------

    http_thread = threading.Thread(
        target=start_http_server,
        daemon=True
    )

    http_thread.start()

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    # Видео
    app.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video
        )
    )

    # FPS
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_fps
        )
    )

    # Ошибки
    app.add_error_handler(
        error_handler
    )

    logger.info(
        "FlowFrames Bot запущен!"
    )

    # --------------------------------------------------------
    # Telegram polling
    # --------------------------------------------------------

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
