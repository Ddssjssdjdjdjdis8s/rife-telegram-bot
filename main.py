import os
import json
import logging

# Получаем Kaggle credentials ДО импорта библиотеки Kaggle
os.environ["KAGGLE_USERNAME"] = os.getenv("KAGGLE_USERNAME", "")
os.environ["KAGGLE_KEY"] = os.getenv("KAGGLE_KEY", "")

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes
)

from kaggle.api.kaggle_api_extended import KaggleApi


# =========================
# НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME")


# =========================
# ПРОВЕРКА ПЕРЕМЕННЫХ
# =========================

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в Render Environment Variables")

if not KAGGLE_USERNAME:
    raise RuntimeError("Не задан KAGGLE_USERNAME в Render Environment Variables")

if not os.getenv("KAGGLE_KEY"):
    raise RuntimeError("Не задан KAGGLE_KEY в Render Environment Variables")


# =========================
# ЛОГИ
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


# =========================
# KAGGLE API
# =========================

api = KaggleApi()
api.authenticate()


# =========================
# ОБРАБОТКА ВИДЕО
# =========================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.message.video:
        return

    # Получаем подпись к видео
    caption = update.message.caption or "60"

    parts = caption.split(" ", 1)

    # FPS
    if parts[0].isdigit():
        target_fps = parts[0]
    else:
        target_fps = "60"

    # Дополнительные параметры
    if len(parts) > 1:
        extra_args = parts[1]
    else:
        extra_args = ""

    await update.message.reply_text(
        f"🎬 Видео получено!\n\n"
        f"FPS: {target_fps}\n"
        f"Дополнительные параметры: "
        f"{extra_args or 'нет'}\n\n"
        f"☁️ Отправляю задачу в Kaggle..."
    )

    try:
        # Получаем файл Telegram
        video_file = await context.bot.get_file(
            update.message.video.file_id
        )

        # Ссылка на файл Telegram
        file_path = video_file.file_path

        file_url = (
            f"https://api.telegram.org/file/bot"
            f"{BOT_TOKEN}/{file_path}"
        )

        # ID Kaggle Kernel
        kernel_id = f"{KAGGLE_USERNAME}/rife-worker"

        # Папка временного Kernel
        kernel_path = "./kernel_temp"

        os.makedirs(kernel_path, exist_ok=True)

        # Загружаем существующий Kernel
        api.kernels_pull(
            kernel_id,
            path=kernel_path
        )

        # Создаём конфигурацию задания
        job_config = {
            "VIDEO_URL": file_url,
            "CHAT_ID": str(update.message.chat_id),
            "BOT_TOKEN": BOT_TOKEN,
            "TARGET_FPS": target_fps,
            "EXTRA_ARGS": extra_args
        }

        # Сохраняем конфигурацию
        with open(
            os.path.join(kernel_path, "job_config.json"),
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                job_config,
                f,
                ensure_ascii=False,
                indent=2
            )

        # Отправляем Kernel обратно в Kaggle
        api.kernels_push(kernel_path)

        await update.message.reply_text(
            "✅ Задача успешно отправлена в Kaggle!\n\n"
            "⏳ Ожидайте обработки видео."
        )

    except Exception as e:

        logging.exception(
            "Ошибка при запуске Kaggle Kernel"
        )

        await update.message.reply_text(
            "❌ Произошла ошибка при запуске Kaggle:\n\n"
            f"{type(e).__name__}: {e}"
        )


# =========================
# ЗАПУСК БОТА
# =========================

def main():

    logging.info("Запуск FlowFrames Bot...")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Обрабатываем видео
    app.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video
        )
    )

    logging.info("FlowFrames Bot запущен!")

    # Запускаем Telegram polling
    app.run_polling()


# =========================
# START
# =========================

if __name__ == "__main__":
    main()
