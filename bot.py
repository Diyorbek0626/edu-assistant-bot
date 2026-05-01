import os
import io
import logging
import requests
import psycopg2
from psycopg2 import pool
from fpdf import FPDF
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# 1. LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 2. ENV O'ZGARUVCHILAR
# ─────────────────────────────────────────────
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise EnvironmentError("TELEGRAM_BOT_TOKEN .env faylda topilmadi!")
if not GROQ_API_KEY:
    raise EnvironmentError("GROQ_API_KEY .env faylda topilmadi!")
if not DATABASE_URL:
    raise EnvironmentError("DATABASE_URL .env faylda topilmadi!")

# ─────────────────────────────────────────────
# 3. CONNECTION POOL
# ─────────────────────────────────────────────
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1, maxconn=10, dsn=DATABASE_URL,
    )
    logger.info("PostgreSQL connection pool yaratildi.")
except Exception as e:
    logger.critical("DB ulanishida xatolik: %s", e)
    raise


def get_conn():
    return db_pool.getconn()


def release_conn(conn):
    db_pool.putconn(conn)


# ─────────────────────────────────────────────
# 4. DB JADVALNI BOSHLASH
# ─────────────────────────────────────────────
def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_messages (
                    id         SERIAL PRIMARY KEY,
                    user_id    BIGINT       NOT NULL,
                    username   TEXT,
                    message    TEXT,
                    topic      TEXT         DEFAULT 'umumiy',
                    created_at TIMESTAMPTZ  DEFAULT NOW()
                );
            """)
        conn.commit()
        logger.info("Jadval tayyor.")
    except Exception as e:
        conn.rollback()
        logger.error("init_db xatolik: %s", e)
    finally:
        release_conn(conn)


# ─────────────────────────────────────────────
# 5. GROQ AI
# ─────────────────────────────────────────────
def ask_groq(topic: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama3-8b-8192",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sen tajribali prezentatsiya mutaxassisisan. "
                    "Foydalanuvchi bergan mavzu boyicha 5-7 ta slayddan "
                    "iborat aniq, lo'nda reja tuz. "
                    "Har bir slayd: 'Slayd N: <sarlavha>' formatida bolsin "
                    "va uning asosiy nuqtalari qisqacha keltirilsin."
                ),
            },
            {"role": "user", "content": f"Mavzu: {topic}"},
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "Groq API javob bermadi. Keyinroq urinib koring."
    except requests.exceptions.HTTPError as e:
        return f"Groq API xatolik: {e}"
    except Exception as e:
        return f"Kutilmagan xatolik: {e}"


# ─────────────────────────────────────────────
# 6. PDF YARATISH
# ─────────────────────────────────────────────
FONT_PATH      = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(os.path.dirname(__file__), "DejaVuSans-Bold.ttf")


class SlidesPDF(FPDF):
    def __init__(self, topic: str, style: str):
        super().__init__()
        self.topic = topic
        self.style = style
        if os.path.exists(FONT_PATH):
            self.add_font("DejaVu", "",  FONT_PATH,      uni=True)
            self.add_font("DejaVu", "B", FONT_BOLD_PATH, uni=True)
            self._font = "DejaVu"
        else:
            self._font = "Arial"

    def header(self):
        if self.style == "minimal":
            self.set_fill_color(245, 245, 250)
            self.set_text_color(25, 25, 112)
        else:
            self.set_fill_color(63, 84, 186)
            self.set_text_color(255, 255, 255)
        self.rect(0, 0, 210, 20, "F")
        self.set_font(self._font, "B", 11)
        self.cell(0, 20, f"Taqdimot: {self.topic}", align="C")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font(self._font, "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Sahifa {self.page_no()}", align="C")


def create_pdf_bytes(topic: str, slides_text: str, style: str = "minimal") -> bytes:
    pdf = SlidesPDF(topic=topic, style=style)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    if style == "minimal":
        pdf.set_fill_color(250, 250, 255)
        pdf.set_text_color(30, 30, 60)
    else:
        pdf.set_fill_color(240, 243, 255)
        pdf.set_text_color(20, 20, 80)
    pdf.rect(0, 0, 210, 297, "F")
    pdf.set_font(pdf._font, "", 12)
    pdf.set_x(15)
    pdf.multi_cell(180, 8, slides_text)
    return bytes(pdf.output())


# ─────────────────────────────────────────────
# 7. DB FUNKSIYALAR
# ─────────────────────────────────────────────
def save_message(user_id: int, username, text: str, topic: str = "umumiy"):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_messages (user_id, username, message, topic) VALUES (%s, %s, %s, %s)",
                (user_id, username or "noma'lum", text, topic),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("save_message xatolik: %s", e)
    finally:
        release_conn(conn)


def get_top_users(topic=None, limit: int = 10):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if topic:
                cur.execute(
                    "SELECT username, COUNT(*) FROM user_messages WHERE topic=%s GROUP BY username ORDER BY COUNT(*) DESC LIMIT %s",
                    (topic, limit),
                )
            else:
                cur.execute(
                    "SELECT username, COUNT(*) FROM user_messages GROUP BY username ORDER BY COUNT(*) DESC LIMIT %s",
                    (limit,),
                )
            return cur.fetchall()
    except Exception as e:
        logger.error("get_top_users xatolik: %s", e)
        return []
    finally:
        release_conn(conn)


# ─────────────────────────────────────────────
# 8. HANDLERLAR
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Salom! Men taqdimot botiman.\n\n"
        "Buyruqlar:\n"
        "/present <mavzu> — slayd reja va PDF yaratish\n"
        "/top — eng faol foydalanuvchilar\n"
        "/top <mavzu> — mavzu boyicha reyting\n"
        "/help — yordam"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def presentation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text("Mavzu kiriting:\n/present <mavzu>\n\nMisol: /present Sun'iy intellekt")
        return

    await update.message.reply_text(f"'{topic}' uchun slayd reja tayyorlanmoqda...")
    slides_text = ask_groq(topic)

    for style, caption in [("minimal", "Minimalistik dizayn"), ("bright", "Yorqin dizayn")]:
        try:
            pdf_bytes = create_pdf_bytes(topic, slides_text, style=style)
            await update.message.reply_document(
                document=io.BytesIO(pdf_bytes),
                filename=f"{topic[:40]}_{style}.pdf",
                caption=caption,
            )
        except Exception as e:
            logger.error("PDF xatolik (%s): %s", style, e)
            await update.message.reply_text(f"{caption} PDF yaratishda xatolik.")

    save_message(update.effective_user.id, update.effective_user.username, f"/present {topic}", topic)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip() if context.args else None
    rows = get_top_users(topic=topic)
    if not rows:
        await update.message.reply_text("Hozircha malumot yoq.")
        return
    header = f"{topic.capitalize()} boyicha eng faol:\n\n" if topic else "Umumiy eng faol:\n\n"
    lines = [f"{i}. @{row[0]} — {row[1]} ta xabar" for i, row in enumerate(rows, 1)]
    await update.message.reply_text(header + "\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_message(user.id, user.username, update.message.text or "")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Xatolik: %s", context.error, exc_info=True)


# ─────────────────────────────────────────────
# 9. MAIN
# ─────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("present", presentation))
    app.add_handler(CommandHandler("top",     top))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
