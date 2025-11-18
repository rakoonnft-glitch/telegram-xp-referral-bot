import os
import logging
import sqlite3
from datetime import datetime, timedelta
from math import sqrt

from telegram import (
    Update,
    ChatMemberUpdated,
    ChatInviteLink,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# -----------------------
# 환경 변수 / 설정
# -----------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "xp_bot.db")

# 레퍼럴 시스템을 적용할 "메인 그룹" ID
# 예: MAIN_CHAT_ID=-1001234567890
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))  # 0이면 모든 그룹에서 허용

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_main_chat(chat_id: int) -> bool:
    """
    레퍼럴/초대 관련 기능을 사용할 수 있는 채팅인지 확인.
    MAIN_CHAT_ID가 0이면 모든 그룹 허용,
    0이 아니면 해당 ID와 일치하는 그룹에서만 허용.
    """
    if MAIN_CHAT_ID == 0:
        return True
    return chat_id == MAIN_CHAT_ID


# -----------------------
# DB 유틸
# -----------------------


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # 유저별 + 채팅방별 XP 정보
    cur.execute(
        """
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
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )

    # 초대 링크
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY,
            chat_id INTEGER,
            inviter_id INTEGER,
            created_at TEXT,
            joined_count INTEGER DEFAULT 0
        )
        """
    )

    # 초대한 유저 목록 (어떤 링크로 들어왔는지)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invited_users (
            chat_id INTEGER,
            user_id INTEGER,
            inviter_id INTEGER,
            invite_link TEXT,
            joined_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )

    conn.commit()
    conn.close()


# -----------------------
# XP / 레벨 계산 로직
# -----------------------


def calc_level(xp: int) -> int:
    # 간단한 레벨 공식: xp가 커질수록 레벨업이 점점 어려워짐
    return int(sqrt(xp / 100)) + 1 if xp > 0 else 1


def xp_for_next_level(level: int) -> int:
    # 다음 레벨에 필요한 누적 XP
    next_level = level + 1
    return int((next_level - 1) ** 2 * 100)


def add_xp(chat_id: int, user, base_xp: int) -> tuple[int, int, int]:
    """
    XP를 추가하고 (xp, level, messages_count)를 반환.
    """
    user_id = user.id
    username = user.username
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT xp, level, messages_count FROM user_stats
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user_id),
    )
    row = cur.fetchone()

    if row is None:
        xp = max(0, base_xp)
        level = calc_level(xp)
        messages_count = 1
        cur.execute(
            """
            INSERT INTO user_stats
            (chat_id, user_id, username, first_name, last_name, xp, level, messages_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id,
                username,
                first_name,
                last_name,
                xp,
                level,
                messages_count,
            ),
        )
    else:
        xp = row["xp"] + max(0, base_xp)
        level = calc_level(xp)
        messages_count = row["messages_count"] + 1
        cur.execute(
            """
            UPDATE user_stats
            SET username = ?, first_name = ?, last_name = ?,
                xp = ?, level = ?, messages_count = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (
                username,
                first_name,
                last_name,
                xp,
                level,
                messages_count,
                chat_id,
                user_id,
            ),
        )

    conn.commit()
    conn.close()
    return xp, level, messages_count


# -----------------------
# 메시지 핸들러
# -----------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 그룹 / 수퍼그룹 메시지에만 반응
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat is None or user is None or message is None:
        return

    if chat.type not in ("group", "supergroup"):
        return

    # 텍스트 길이에 비례해서 XP 부여 (최소 3)
    text = message.text or message.caption or ""
    length = len(text)
    base_xp = 3 + length // 20

    xp, level, messages_count = add_xp(chat.id, user, base_xp)

    # 레벨업 알림
    old_xp = xp - base_xp
    old_level = calc_level(old_xp)
    if level > old_level:
        await message.reply_text(
            f"🎉 {user.mention_html()} 님이 레벨업 했습니다!\n"
            f"➡️ 현재 레벨: {level}",
            parse_mode="HTML",
        )


# -----------------------
# 명령어 핸들러
# -----------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "안녕하세요! 저는 Terminal.Fi XP 봇입니다.\n"
        "이 채팅방에서 메시지를 보내면 XP를 얻고 레벨이 올라가요.\n\n"
        "주요 명령어:\n"
        "/stats   - 내 레벨/XP 확인\n"
        "/ranking - 상위 10명 랭킹\n"
        "/daily   - 하루 한 번 보너스 XP\n"
        "/mylink  - 나만의 초대 링크 생성 (메인 그룹 전용)\n"
        "/refstats- 초대 랭킹 보기 (메인 그룹 전용)\n"
        "/chatid  - 이 채팅의 ID 확인"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return
    await update.message.reply_text(
        f"이 채팅의 ID는 `{chat.id}` 입니다.", parse_mode="Markdown"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT xp, level, messages_count, last_daily, invites_count
        FROM user_stats
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat.id, user.id),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        await update.message.reply_text(
            "아직 기록된 경험치가 없습니다.\n메시지를 보내면 XP가 쌓입니다!"
        )
        return

    xp = row["xp"]
    level = row["level"]
    messages_count = row["messages_count"]
    invites_count = row["invites_count"]
    next_xp = xp_for_next_level(level)
    remain = max(0, next_xp - xp)

    text = (
        f"📊 {user.full_name} 님의 통계\n\n"
        f"🎯 레벨: {level}\n"
        f"⭐ 경험치: {xp} XP\n"
        f"📈 다음 레벨까지: {remain} XP\n"
        f"💬 총 메시지 수: {messages_count}\n"
        f"👥 초대 인원 수: {invites_count}"
    )

    await update.message.reply_text(text)


async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name, xp, level
        FROM user_stats
        WHERE chat_id = ?
        ORDER BY xp DESC
        LIMIT 10
        """,
        (chat.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "아직 이 채팅방에는 경험치 기록이 없습니다."
        )
        return

    lines = ["🏆 경험치 랭킹 TOP 10\n"]
    medals = ["🥇", "🥈", "🥉"]
    for idx, row in enumerate(rows, start=1):
        username = row["username"]
        if username:
            name = f"@{username}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"

        xp = row["xp"]
        level = row["level"]

        prefix = medals[idx - 1] if idx <= len(medals) else f"{idx}."
        lines.append(f"{prefix} {name} - Lv.{level} ({xp} XP)")

    await update.message.reply_text("\n".join(lines))


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT xp, level, messages_count, last_daily
        FROM user_stats
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat.id, user.id),
    )
    row = cur.fetchone()

    now = datetime.utcnow()
    bonus_xp = 50

    if row is None:
        # 처음 사용하는 유저
        xp = bonus_xp
        level = calc_level(xp)
        messages_count = 0
        last_daily_str = now.isoformat()
        cur.execute(
            """
            INSERT INTO user_stats
            (chat_id, user_id, username, first_name, last_name, xp, level, messages_count, last_daily)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat.id,
                user.id,
                user.username,
                user.first_name or "",
                user.last_name or "",
                xp,
                level,
                messages_count,
                last_daily_str,
            ),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"🎁 첫 일일 보상으로 {bonus_xp} XP를 받았습니다!\n"
            f"현재 레벨: {level}, 총 XP: {xp}"
        )
        return

    last_daily = row["last_daily"]
    if last_daily:
        last_dt = datetime.fromisoformat(last_daily)
        if now - last_dt < timedelta(hours=24):
            remain = timedelta(hours=24) - (now - last_dt)
            hours = remain.seconds // 3600
            minutes = (remain.seconds % 3600) // 60
            await update.message.reply_text(
                f"⏰ 이미 오늘의 보상을 받았습니다.\n"
                f"{hours}시간 {minutes}분 후에 다시 시도해 주세요."
            )
            conn.close()
            return

    xp = row["xp"] + bonus_xp
    level = calc_level(xp)
    messages_count = row["messages_count"]
    last_daily_str = now.isoformat()

    cur.execute(
        """
        UPDATE user_stats
        SET xp = ?, level = ?, last_daily = ?
        WHERE chat_id = ? AND user_id = ?
        """,
        (xp, level, last_daily_str, chat.id, user.id),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🎁 일일 보상으로 {bonus_xp} XP를 받았습니다!\n"
        f"현재 레벨: {level}, 총 XP: {xp}"
    )


# -----------------------
# 리퍼럴 / 초대 링크
# -----------------------


async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat is None or user is None:
        return

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("이 명령어는 그룹 채팅에서만 사용할 수 있습니다.")
        return

    # 레퍼럴 전용 메인 그룹이 지정되어 있으면, 해당 그룹에서만 허용
    if not is_main_chat(chat.id):
        await update.message.reply_text(
            "이 봇의 레퍼럴 시스템은 지정된 메인 그룹에서만 사용할 수 있습니다."
        )
        return

    # Bot이 관리자이며 초대 링크 생성 권한이 있다고 가정
    try:
        invite: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=chat.id,
            name=f"referral:{user.id}",
            creates_join_request=False,
        )
    except Exception:
        logger.exception("초대 링크 생성 실패")
        await update.message.reply_text(
            "초대 링크를 생성할 수 없습니다.\n"
            "봇이 관리자이며 초대 링크 생성 권한이 있는지 확인해 주세요."
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO invite_links
        (invite_link, chat_id, inviter_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (invite.invite_link, chat.id, user.id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "👥 나만의 초대 링크를 생성했습니다!\n"
        "이 링크로 들어온 인원은 모두 내 초대로 집계됩니다.\n\n"
        f"{invite.invite_link}"
    )


async def cmd_refstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text(
            "초대 랭킹은 메인 그룹에서만 확인할 수 있습니다."
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.username, u.first_name, u.last_name, u.invites_count
        FROM user_stats u
        WHERE u.chat_id = ?
        AND u.invites_count > 0
        ORDER BY u.invites_count DESC
        LIMIT 10
        """,
        (chat.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("아직 초대 기록이 없습니다.")
        return

    lines = ["👥 초대 랭킹 TOP 10\n"]
    for idx, row in enumerate(rows, start=1):
        username = row["username"]
        if username:
            name = f"@{username}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"

        count = row["invites_count"]
        lines.append(f"{idx}. {name} - {count}명 초대")

    await update.message.reply_text("\n".join(lines))


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return

    # 레퍼럴 전용 메인 그룹이 아니면 초대 추적 X
    if not is_main_chat(chat.id):
        return

    chat_member: ChatMemberUpdated = update.chat_member
    new = chat_member.new_chat_member
    old = chat_member.old_chat_member

    # 새로 들어온 경우만 처리
    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        user = new.user
        invite_link = chat_member.invite_link
        if invite_link is None:
            return

        link_url = invite_link.invite_link

        conn = get_conn()
        cur = conn.cursor()

        # 이미 초대 기록이 있는 유저인지 확인
        cur.execute(
            """
            SELECT inviter_id FROM invited_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat.id, user.id),
        )
        if cur.fetchone() is not None:
            conn.close()
            return

        # 초대 링크 테이블 업데이트
        cur.execute(
            """
            SELECT inviter_id, joined_count FROM invite_links
            WHERE invite_link = ? AND chat_id = ?
            """,
            (link_url, chat.id),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return

        inviter_id = row["inviter_id"]
        joined_count = row["joined_count"] + 1

        cur.execute(
            """
            UPDATE invite_links
            SET joined_count = ?
            WHERE invite_link = ? AND chat_id = ?
            """,
            (joined_count, link_url, chat.id),
        )

        # 초대한 사람의 invites_count +1
        cur.execute(
            """
            SELECT invites_count FROM user_stats
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat.id, inviter_id),
        )
        inviter_row = cur.fetchone()
        if inviter_row is None:
            # 아직 user_stats에 없으면 생성
            cur.execute(
                """
                INSERT INTO user_stats
                (chat_id, user_id, username, first_name, last_name, xp, level, messages_count, last_daily, invites_count)
                VALUES (?, ?, ?, ?, ?, 0, 1, 0, NULL, 1)
                """,
                (
                    chat.id,
                    inviter_id,
                    None,
                    "",
                    "",
                ),
            )
        else:
            invites_count = inviter_row["invites_count"] + 1
            cur.execute(
                """
                UPDATE user_stats
                SET invites_count = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (invites_count, chat.id, inviter_id),
            )

        # 어떤 링크로 들어왔는지 저장
        cur.execute(
            """
            INSERT OR REPLACE INTO invited_users
            (chat_id, user_id, inviter_id, invite_link, joined_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat.id,
                user.id,
                inviter_id,
                link_url,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"👋 {user.full_name} 님이 초대 링크를 통해 입장했습니다!\n"
                    f"초대한 유저 ID: {inviter_id}"
                ),
            )
        except Exception:
            logger.exception("welcome 메시지 전송 실패")


# -----------------------
# 메인 (동기 함수)
# -----------------------


def main():
    init_db()

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 메시지 핸들러 (텍스트/캡션, 명령어 제외)
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Caption) & (~filters.COMMAND),
            handle_message,
        )
    )

    # 명령어 핸들러
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler(["stats", "xp"], cmd_stats))
    application.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    application.add_handler(CommandHandler("daily", cmd_daily))
    application.add_handler(CommandHandler("mylink", cmd_mylink))
    application.add_handler(CommandHandler("refstats", cmd_refstats))

    # chat_member 업데이트 (초대 링크 추적)
    application.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    logger.info("XP Bot started")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
