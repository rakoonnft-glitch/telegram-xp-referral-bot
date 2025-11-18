import os
import logging
import sqlite3
from datetime import datetime, timedelta, time, timezone
from math import sqrt

from telegram import (
    Update,
    ChatMemberUpdated,
    ChatInviteLink,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# -----------------------
# 환경 변수 / 기본 설정
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "xp_bot.db")

MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

_admin_env = os.getenv("ADMIN_USER_IDS", "")
INITIAL_ADMIN_IDS = set()

for part in _admin_env.split(","):
    part = part.strip()
    if part:
        try:
            INITIAL_ADMIN_IDS.add(int(part))
        except ValueError:
            pass

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 메모리 관리자 목록
ADMIN_USER_IDS: set[int] = set()


def is_owner(uid: int) -> bool:
    return OWNER_ID != 0 and uid == OWNER_ID


def is_admin(uid: int) -> bool:
    return uid in ADMIN_USER_IDS or is_owner(uid)


def all_admin_targets() -> set[int]:
    t = set(ADMIN_USER_IDS)
    if OWNER_ID:
        t.add(OWNER_ID)
    return t


def is_main_chat(chat_id: int) -> bool:
    return MAIN_CHAT_ID == 0 or chat_id == MAIN_CHAT_ID


# -----------------------
# DB UTIL
# -----------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def reload_admins():
    global ADMIN_USER_IDS
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT admin_id FROM admin_users")
    rows = cur.fetchall()
    conn.close()
    ADMIN_USER_IDS = {int(r["admin_id"]) for r in rows}
    logger.info("Loaded admins: %s", ADMIN_USER_IDS)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            messages_count INTEGER DEFAULT 0,
            last_daily TEXT,
            invites_count INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY,
            chat_id INTEGER,
            inviter_id INTEGER,
            created_at TEXT,
            joined_count INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            admin_id INTEGER PRIMARY KEY
        )
    """)

    for aid in INITIAL_ADMIN_IDS:
        cur.execute(
            "INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)", (aid,)
        )

    conn.commit()
    conn.close()
    reload_admins()


# -----------------------
# XP 계산
# -----------------------

def calc_level(xp: int) -> int:
    return int(sqrt(xp / 100)) + 1 if xp > 0 else 1


def xp_for_next_level(level: int) -> int:
    return int((level ** 2) * 100)


def add_xp(chat_id: int, user, base_xp: int):
    user_id = user.id
    username = user.username
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT xp, level, messages_count FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    row = cur.fetchone()

    if not row:
        xp = base_xp
        level = calc_level(xp)
        msg_count = 1

        cur.execute("""
            INSERT INTO user_stats
            (chat_id, user_id, username, first_name, last_name, xp, level, messages_count)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            chat_id,
            user_id,
            username,
            first_name,
            last_name,
            xp,
            level,
            msg_count,
        ))
    else:
        xp = row["xp"] + base_xp
        level = calc_level(xp)
        msg_count = row["messages_count"] + 1

        cur.execute("""
            UPDATE user_stats
            SET username=?,first_name=?,last_name=?,xp=?,level=?,messages_count=?
            WHERE chat_id=? AND user_id=?
        """, (
            username,
            first_name,
            last_name,
            xp,
            level,
            msg_count,
            chat_id,
            user_id,
        ))

    conn.commit()
    conn.close()

    return xp, level, msg_count


# -----------------------
# 일반 메시지 → XP
# -----------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user

    if not chat or not user or not message:
        return
    if chat.type not in ("group", "supergroup"):
        return

    text = message.text or ""
    base_xp = 3 + len(text) // 20

    xp, level, _ = add_xp(chat.id, user, base_xp)

    if level > calc_level(xp - base_xp):
        await message.reply_text(
            f"🎉 {user.mention_html()} 님이 레벨업 했습니다! (Lv {level})",
            parse_mode="HTML",
        )


# -----------------------
# /start (help 통합)
# -----------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    text = (
        "안녕하세요! Terminal.Fi XP Bot입니다.\n"
        "메시지를 보내면 XP를 얻고 레벨이 올라갑니다.\n\n"
        "📌 일반 명령어\n"
        "/stats - 내 스탯\n"
        "/ranking - 경험치 TOP 10\n"
        "/daily - 일일보상\n"
        "/mylink - 초대 링크 생성 (메인 그룹)\n"
        "/myref - 내 초대 인원\n"
        "/refstats - 초대 랭킹\n"
    )

    if is_admin(user.id):
        text += (
            "\n🔧 관리자 명령어\n"
            "/chatid - 채팅방 ID 확인\n"
            "/listadmins - 관리자 목록\n"
            "/refuser <user> - 특정 유저 초대수\n"
        )

    if is_owner(user.id):
        text += "\n👑 OWNER 명령어\n/resetxp - XP 초기화"

    await update.message.reply_text(text)


# -----------------------
# /chatid
# -----------------------

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("관리자만 사용 가능합니다.")
        return

    await update.message.reply_text(f"Chat ID = `{update.effective_chat.id}`", parse_mode="Markdown")


# -----------------------
# /stats
# -----------------------

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT xp, level, messages_count, invites_count FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("아직 경험치 기록이 없습니다.")
        return

    xp = row["xp"]
    level = row["level"]
    msgs = row["messages_count"]
    invites = row["invites_count"]
    next_xp = xp_for_next_level(level) - xp

    text = (
        f"📊 {user.full_name}님의 통계\n\n"
        f"레벨: {level}\n"
        f"경험치: {xp}\n"
        f"다음 레벨까지: {next_xp} XP\n"
        f"메시지 수: {msgs}\n"
        f"초대 인원: {invites}\n"
    )

    await update.message.reply_text(text)


# -----------------------
# /ranking
# -----------------------

async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, first_name, last_name, xp, level
        FROM user_stats
        WHERE chat_id=?
        ORDER BY xp DESC
        LIMIT 10
    """, (chat.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("랭킹 데이터 없음.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 경험치 TOP 10\n"]

    for i, r in enumerate(rows, 1):
        name = f"@{r['username']}" if r['username'] else r["first_name"]
        medal = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} {name} - Lv.{r['level']} ({r['xp']} XP)")

    await update.message.reply_text("\n".join(lines))


# -----------------------
# /daily
# -----------------------

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat = update.effective_chat
    user = update.effective_user
    now = datetime.utcnow()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT xp, level, last_daily FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    row = cur.fetchone()

    if not row:
        xp = 50
        cur.execute("""
            INSERT INTO user_stats
            (chat_id,user_id,username,first_name,last_name,xp,level,last_daily)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            chat.id,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            xp,
            calc_level(xp),
            now.isoformat(),
        ))
        conn.commit()
        conn.close()
        await update.message.reply_text("🎁 첫 일일보상 50XP 지급!")
        return

    last = row["last_daily"]
    if last and (now - datetime.fromisoformat(last)) < timedelta(hours=24):
        remain = timedelta(hours=24) - (now - datetime.fromisoformat(last))
        h = remain.seconds // 3600
        m = (remain.seconds % 3600) // 60
        await update.message.reply_text(f"⏳ 이미 받았습니다. {h}시간 {m}분 후 재사용 가능.")
        conn.close()
        return

    xp = row["xp"] + 50
    cur.execute(
        "UPDATE user_stats SET xp=?,level=?,last_daily=? WHERE chat_id=? AND user_id=?",
        (xp, calc_level(xp), now.isoformat(), chat.id, user.id),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text("🎁 일일보상 50XP 지급!")


# -----------------------
# /mylink
# -----------------------

async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("그룹에서만 사용 가능합니다.")
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text("메인 그룹에서만 사용 가능합니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT invite_link FROM invite_links WHERE chat_id=? AND inviter_id=?",
        (chat.id, user.id),
    )
    row = cur.fetchone()

    # 이미 존재 → 재사용
    if row:
        await update.message.reply_text(f"이미 생성된 링크입니다:\n{row['invite_link']}")
        conn.close()
        return

    invite: ChatInviteLink = await context.bot.create_chat_invite_link(
        chat_id=chat.id,
        name=f"ref:{user.id}",
        creates_join_request=False,
    )

    cur.execute("""
        INSERT INTO invite_links (invite_link,chat_id,inviter_id,created_at)
        VALUES (?,?,?,?)
    """, (invite.invite_link, chat.id, user.id, datetime.utcnow().isoformat()))

    conn.commit()
    conn.close()

    await update.message.reply_text(f"초대 링크가 생성되었습니다:\n{invite.invite_link}")


# -----------------------
# /myref
# -----------------------

async def cmd_myref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(joined_count) AS c
        FROM invite_links
        WHERE inviter_id=?
    """, (user.id,))
    row = cur.fetchone()
    conn.close()

    cnt = row["c"] if row["c"] else 0
    await update.message.reply_text(f"👥 내 초대 인원: {cnt}명")


# -----------------------
# 초대 Tracking
# -----------------------

async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if not is_main_chat(chat.id):
        return

    cm: ChatMemberUpdated = update.chat_member
    new = cm.new_chat_member
    old = cm.old_chat_member

    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        if not cm.invite_link:
            return

        link = cm.invite_link.invite_link
        user = new.user

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            "SELECT inviter_id,joined_count FROM invite_links WHERE invite_link=?",
            (link,),
        )
        row = cur.fetchone()

        if not row:
            conn.close()
            return

        inviter_id = row["inviter_id"]
        joined = row["joined_count"] + 1

        cur.execute(
            "UPDATE invite_links SET joined_count=? WHERE invite_link=?",
            (joined, link),
        )

        cur.execute(
            "SELECT invites_count FROM user_stats WHERE chat_id=? AND user_id=?",
            (chat.id, inviter_id),
        )
        invrow = cur.fetchone()

        if not invrow:
            cur.execute("""
                INSERT INTO user_stats
                (chat_id,user_id,xp,level,messages_count,last_daily,invites_count)
                VALUES (?,?,?,?,?,?,?)
            """, (chat.id, inviter_id, 0, 1, 0, None, 1))
        else:
            cur.execute(
                "UPDATE user_stats SET invites_count=? WHERE chat_id=? AND user_id=?",
                (invrow["invites_count"] + 1, chat.id, inviter_id),
            )

        conn.commit()
        conn.close()

        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👋 {user.full_name} 님이 초대로 입장했습니다! (초대한 유저: {inviter_id})",
        )


# -----------------------
# 관리자 명령어
# -----------------------

async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("관리자만 사용 가능합니다.")
        return

    lines = ["📌 관리자 목록"]
    if OWNER_ID:
        lines.append(f"- OWNER: {OWNER_ID}")
    for aid in sorted(ADMIN_USER_IDS):
        lines.append(f"- {aid}")

    await update.message.reply_text("\n".join(lines))


async def cmd_refuser(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("관리자만 가능")
        return

    args = context.args
    if not args:
        await update.message.reply_text("사용법: /refuser @username 또는 user_id")
        return

    q = args[0]
    if q.startswith("@"):
        q = q[1:]

    target_id = None

    if q.isdigit():
        target_id = int(q)
    else:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM user_stats WHERE username=? LIMIT 1", (q,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            await update.message.reply_text("해당 유저 없음")
            return
        target_id = int(row["user_id"])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(joined_count) AS c
        FROM invite_links
        WHERE inviter_id=?
    """, (target_id,))
    row = cur.fetchone()
    conn.close()

    cnt = row["c"] if row["c"] else 0
    await update.message.reply_text(f"해당 유저의 총 초대 인원: {cnt}명")


async def cmd_resetxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("OWNER만 가능합니다.")
        return

    if MAIN_CHAT_ID == 0:
        await update.message.reply_text("MAIN_CHAT_ID 미설정.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_stats SET xp=0,level=1,messages_count=0,last_daily=NULL,invites_count=0 WHERE chat_id=?",
        (MAIN_CHAT_ID,),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()

    await update.message.reply_text(f"XP 초기화 완료 ({affected}명)")


# -----------------------
# Daily Summary
# -----------------------

async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if MAIN_CHAT_ID == 0:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username,first_name,last_name,xp,level
        FROM user_stats
        WHERE chat_id=?
        ORDER BY xp DESC
        LIMIT 10
    """, (MAIN_CHAT_ID,))
    rows = cur.fetchall()

    cur.execute(
        "SELECT COUNT(*) AS c FROM user_stats WHERE chat_id=?", (MAIN_CHAT_ID,)
    )
    total = cur.fetchone()["c"]
    conn.close()

    now_kst = datetime.utcnow() + timedelta(hours=9)

    lines = ["오늘 XP TOP 10\n"]
    for i, r in enumerate(rows, 1):
        name = f"@{r['username']}" if r["username"] else r["first_name"]
        lines.append(f"{i}. {name} - Lv.{r['level']} ({r['xp']}XP)")
    lines.append(f"\n총 유저 수: {total}명")

    msg = f"📊 Daily Summary (KST)\n{now_kst.strftime('%Y-%m-%d %H:%M')}\n\n" + "\n".join(lines)

    for admin in all_admin_targets():
        try:
            await context.bot.send_message(chat_id=admin, text=msg)
        except Exception:
            pass


# -----------------------
# MAIN
# -----------------------

def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 메시지 → XP
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Caption) & (~filters.COMMAND),
            handle_message,
        )
    )

    # 명령어
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler(["stats", "xp"], cmd_stats))
    app.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("mylink", cmd_mylink))
    app.add_handler(CommandHandler(["myref", "myinv"], cmd_myref))
    app.add_handler(CommandHandler("refstats", cmd_ranking))
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("refuser", cmd_refuser))
    app.add_handler(CommandHandler("resetxp", cmd_resetxp))

    # 초대 트래킹
    app.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # Daily summary — KST 23:59 → UTC 14:59
    app.job_queue.run_daily(
        send_daily_summary,
        time=time(hour=14, minute=59, tzinfo=timezone.utc),
        name="daily_summary",
    )

    logger.info("XP Bot Started.")
    app.run_polling()


if __name__ == "__main__":
    main()
