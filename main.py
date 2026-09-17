import os
import json
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from kaggle.api.kaggle_api_extended import KaggleApi

BOT_TOKEN = os.getenv("BOT_TOKEN")
KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME")
KAGGLE_API_TOKEN = os.getenv("KAGGLE_API_TOKEN")

os.environ["KAGGLE_USERNAME"] = KAGGLE_USERNAME
os.environ["KAGGLE_KEY"] = KAGGLE_API_TOKEN

api = KaggleApi()
api.authenticate()

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or "60"
    parts = caption.split(" ", 1)
    target_fps = parts[0] if parts[0].isdigit() else "60"
    extra_args = parts[1] if len(parts) > 1 else ""

    await update.message.reply_text(f"Принято! FPS: {target_fps}. Флаги: '{extra_args}'. Отправляю в Kaggle...")

    video_file = await context.bot.get_file(update.message.video.file_id)
    file_url = video_file.file_path

    kernel_id = f"{KAGGLE_USERNAME}/rife-worker"
    try:
        api.kernels_pull(kernel_id, path="./kernel_temp")
        
        job_config = {
            "VIDEO_URL": file_url,
            "CHAT_ID": str(update.message.chat_id),
            "BOT_TOKEN": BOT_TOKEN,
            "TARGET_FPS": target_fps,
            "EXTRA_ARGS": extra_args
        }
        
        with open("./kernel_temp/job_config.json", "w") as f:
            json.dump(job_config, f)

        api.kernels_push("./kernel_temp")
        await update.message.reply_text("Задача в облаке! Ожидайте готовности...")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при запуске: {str(e)}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.run_polling()
  
