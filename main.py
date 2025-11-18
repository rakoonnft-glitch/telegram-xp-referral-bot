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
    Application,
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

# 메인 그룹 (랭킹·요약 기준)
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))  # 0이면 메인 그룹 미지정

# BotFather 로 만든 오너(너) user id
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# 초기 관리자 (쉼표 구분)
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

# 런타임 관리자 목록 (DB 에서 읽어옴)
ADMIN_USER_IDS: set[int] = set()


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in ADMIN_USER_IDS


def all_admin_targets() -> set[int]:
    targets = set(ADMIN_USER_IDS)
    if OWNER_ID:
        targets.add(OWNER_ID)
    return targets


def is_main_chat(chat_id: int) -> bool:
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

    # 관리자 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            admin_id INTEGER PRIMARY KEY
        )
        """
    )

    # 초기 관리자 등록
    for aid in INITIAL_ADMIN_IDS:
        cur.execute(
            "INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)",
            (aid,),
        )

    conn.commit()
    conn.close()

    reload_admins()


def reload_admins():
    """DB 기준으로 ADMIN_USER_IDS 세트 갱신"""
    global ADMIN_USER_IDS
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT admin_id FROM admin_users")
    rows = cur.fetchall()
    conn.close()

    ADMIN_USER_IDS = {int(r["admin_id"]) for r in rows}
    logger.info("Loaded admins from DB: %s", ADMIN_USER_IDS)


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
# 초대 카운트 유틸
# -----------------------


def get_invite_count_for_user(user_id: int) -> int:
    """
    invite_links.joined_count 합산해서 초대 인원 수 계산
    MAIN_CHAT_ID 가 설정되어 있으면 그 채팅 기준, 아니면 전체.
    """
    conn = get_conn()
    cur = conn.cursor()
    if MAIN_CHAT_ID != 0:
        cur.execute(
            """
            SELECT COALESCE(SUM(joined_count), 0) AS c
            FROM invite_links
            WHERE inviter_id = ? AND chat_id = ?
            """,
            (user_id, MAIN_CHAT_ID),
        )
    else:
        cur.execute(
            """
            SELECT COALESCE(SUM(joined_count), 0) AS c
            FROM invite_links
            WHERE inviter_id = ?
            """,
            (user_id,),
        )
    row = cur.fetchone()
    conn.close()
    if row is None or row["c"] is None:
        return 0
    return int(row["c"])


# -----------------------
# 일반 메시지 → XP
# -----------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat is None or user is None or message is None:
        return

    if chat.type not in ("group", "supergroup"):
        return

    text = message.text or message.caption or ""
    length = len(text)
    base_xp = 3 + length // 20

    xp, level, _ = add_xp(chat.id, user, base_xp)

    old_xp = xp - base_xp
    old_level = calc_level(old_xp)
    if level > old_level:
        await message.reply_text(
            f"🎉 {user.mention_html()} 님이 레벨업 했습니다!\n"
            f"➡️ 현재 레벨: {level}",
            parse_mode="HTML",
        )


# -----------------------
# 공용 명령어
# -----------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help
    - 그룹: 일반 유저용 도움말
    - DM: 일반 도움말 + 관리자면 관리자 섹션 추가
    """
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return

    try:
        base_text = (
            "안녕하세요! 저는 Terminal.Fi XP 봇입니다.\n"
            "이 채팅방에서 메시지를 보내면 XP를 얻고 레벨이 올라가요.\n\n"
            "일반 명령어:\n"
            "/stats - 내 레벨/XP 확인\n"
            "/ranking - 상위 10명 랭킹\n"
            "/daily - 하루 한 번 보너스 XP\n"
            "/mylink - 나만의 초대 링크 생성 (메인 그룹 전용)\n"
            "/myref - 내 초대 링크로 들어온 인원 수 확인\n"
            "/refstats - 초대 랭킹 보기 (메인 그룹 전용)\n"
        )

        # 그룹 / 슈퍼그룹이면 그냥 이것만
        if chat.type in ("group", "supergroup"):
            await message.reply_text(base_text)
            return

        # DM 인 경우
        text = base_text
        if is_admin(user.id):
            text += (
                "\n[관리자 전용 명령어]  (DM 에서만 사용 권장)\n"
                "/chatid - 이 채팅의 ID 확인\n"
                "/listadmins - 관리자 ID 목록 보기\n"
                "/refuser <@handle 또는 user_id> - 해당 유저 초대 인원 조회\n"
                "/resetxp - 메인 그룹 XP 초기화 (OWNER 전용)\n"
            )

        await message.reply_text(text)
    except Exception:
        logger.exception("/help 처리 중 오류")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if chat is None or user is None or msg is None:
        return

    if not is_admin(user.id):
        await msg.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    await msg.reply_text(f"이 채팅의 ID는 `{chat.id}` 입니다.", parse_mode="Markdown")


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
        f"👥 초대 인원 수(별도 시스템): {invites_count}\n"
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
    """
    /mylink
    - 메인 그룹에서만 사용 가능
    - 같은 유저가 여러 번 써도, 기존에 만든 초대 링크를 계속 재사용
    """
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat is None or user is None:
        return

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("이 명령어는 그룹 채팅에서만 사용할 수 있습니다.")
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text(
            "이 봇의 레퍼럴 시스템은 지정된 메인 그룹에서만 사용할 수 있습니다."
        )
        return

    conn = get_conn()
    cur = conn.cursor()

    # 1) 이미 이 유저가 이 채팅에서 쓴 초대링크가 있는지 먼저 확인
    cur.execute(
        """
        SELECT invite_link FROM invite_links
        WHERE chat_id = ? AND inviter_id = ?
        LIMIT 1
        """,
        (chat.id, user.id),
    )
    row = cur.fetchone()

    if row:
        # 있다 → 그 링크 그대로 재사용
        link_url = row["invite_link"]
        conn.close()
        await update.message.reply_text(
            "👥 이미 생성된 나만의 초대 링크가 있습니다!\n"
            "이 링크를 계속 사용해 주세요.\n\n"
            f"{link_url}"
        )
        return

    # 2) 없으면 새로 생성
    try:
        invite: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=chat.id,
            name=f"referral:{user.id}",
            creates_join_request=False,
        )
    except Exception:
        conn.close()
        logger.exception("초대 링크 생성 실패")
        await update.message.reply_text(
            "초대 링크를 생성할 수 없습니다.\n"
            "봇이 관리자이며 초대 링크 생성 권한이 있는지 확인해 주세요."
        )
        return

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


async def cmd_myref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /myref, /myinvites
    → 내 초대 링크로 들어온 인원 수 확인
    """
    user = update.effective_user
    msg = update.message
    if user is None or msg is None:
        return

    count = get_invite_count_for_user(user.id)
    await msg.reply_text(
        f"👥 현재까지 내 초대 링크를 통해 들어온 인원은 총 {count}명입니다."
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

    if not is_main_chat(chat.id):
        return

    chat_member: ChatMemberUpdated = update.chat_member
    new = chat_member.new_chat_member
    old = chat_member.old_chat_member

    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        user = new.user
        invite_link = chat_member.invite_link
        if invite_link is None:
            return

        link_url = invite_link.invite_link

        conn = get_conn()
        cur = conn.cursor()

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

        cur.execute(
            """
            SELECT invites_count, username, first_name, last_name FROM user_stats
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat.id, inviter_id),
        )
        inviter_row = cur.fetchone()
        if inviter_row is None:
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
# 관리자용 명령어
# -----------------------


async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if chat is None or user is None or msg is None:
        return

    if not is_admin(user.id):
        await msg.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    lines = ["현재 관리자 ID 목록:"]
    if OWNER_ID:
        lines.append(f"- OWNER_ID: {OWNER_ID}")
    for aid in sorted(ADMIN_USER_IDS):
        lines.append(f"- {aid}")

    await msg.reply_text("\n".join(lines))


async def cmd_refuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /refuser <@handle 또는 user_id>
    → 관리자 전용: 특정 유저의 초대 인원 수 조회
    """
    user = update.effective_user
    msg = update.message
    args = context.args

    if user is None or msg is None:
        return

    if not is_admin(user.id):
        await msg.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /refuser @username 또는 /refuser 123456789")
        return

    query = args[0].strip()
    if query.startswith("@"):
        query = query[1:]

    target_user_id = None
    target_name = None

    # 숫자면 바로 user_id 로 사용
    if query.isdigit():
        target_user_id = int(query)
        target_name = f"user_id {target_user_id}"
    else:
        # username 으로 user_stats 에서 찾기
        conn = get_conn()
        cur = conn.cursor()
        if MAIN_CHAT_ID != 0:
            cur.execute(
                """
                SELECT user_id, username, first_name, last_name
                FROM user_stats
                WHERE chat_id = ? AND username = ?
                LIMIT 1
                """,
                (MAIN_CHAT_ID, query),
            )
        else:
            cur.execute(
                """
                SELECT user_id, username, first_name, last_name
                FROM user_stats
                WHERE username = ?
                LIMIT 1
                """,
                (query,),
            )
        row = cur.fetchone()
        conn.close()

        if row is None:
            await msg.reply_text("해당 username 을 user_stats 에서 찾을 수 없습니다.")
            return

        target_user_id = int(row["user_id"])
        if row["username"]:
            target_name = f"@{row['username']}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            target_name = (fn + " " + ln).strip() or f"user_id {target_user_id}"

    count = get_invite_count_for_user(target_user_id)
    await msg.reply_text(
        f"👥 {target_name} 님의 초대 링크를 통해 들어온 인원은 총 {count}명입니다."
    )


async def cmd_resetxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if chat is None or user is None or msg is None:
        return

    if not is_owner(user.id):
        await msg.reply_text("이 명령어는 봇 소유자(OWNER_ID)만 사용할 수 있습니다.")
        return

    if MAIN_CHAT_ID == 0:
        await msg.reply_text("MAIN_CHAT_ID가 설정되어 있지 않아 XP를 리셋할 수 없습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE user_stats
        SET xp = 0, level = 1, messages_count = 0,
            last_daily = NULL, invites_count = 0
        WHERE chat_id = ?
        """,
        (MAIN_CHAT_ID,),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()

    await msg.reply_text(
        f"✅ MAIN_CHAT_ID={MAIN_CHAT_ID} 에 대한 XP/레벨/메시지/초대 기록을 초기화했습니다.\n"
        f"(영향 받은 레코드 수: {affected}명)"
    )


# -----------------------
# 매일 23:59 KST 요약 DM
# -----------------------


async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if MAIN_CHAT_ID == 0:
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
        (MAIN_CHAT_ID,),
    )
    rows = cur.fetchall()

    cur.execute(
        "SELECT COUNT(*) AS c FROM user_stats WHERE chat_id = ?",
        (MAIN_CHAT_ID,),
    )
    total_users = cur.fetchone()["c"]

    conn.close()

    now_kst = datetime.utcnow() + timedelta(hours=9)

    if not rows:
        body = "오늘 기록된 활동/XP 데이터가 없습니다."
    else:
        lines = ["오늘 기준 메인 그룹 XP 상위 10명:\n"]
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
            lines.append(f"{idx}. {name} - Lv.{level} ({xp} XP)")
        lines.append(f"\n총 기록된 유저 수: {total_users}명")
        body = "\n".join(lines)

    text = (
        f"📊 Daily XP 요약 (KST 기준)\n"
        f"{now_kst.strftime('%Y-%m-%d %H:%M')}\n"
        f"MAIN_CHAT_ID = {MAIN_CHAT_ID}\n\n"
        f"{body}"
    )

    for uid in all_admin_targets():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            logger.exception("daily summary DM 실패 (user_id=%s)", uid)


# -----------------------
# 메인
# -----------------------


async def main():
    init_db()

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 일반 메시지 핸들러 (XP)
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Caption) & (~filters.COMMAND),
            handle_message,
        )
    )

    # 공용 명령어
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler(["stats", "xp"], cmd_stats))
    application.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    application.add_handler(CommandHandler("daily", cmd_daily))
    application.add_handler(CommandHandler("mylink", cmd_mylink))
    application.add_handler(CommandHandler(["myref", "myinvites"], cmd_myref))
    application.add_handler(CommandHandler("refstats", cmd_refstats))

    # 관리자용
    application.add_handler(CommandHandler("listadmins", cmd_listadmins))
    application.add_handler(CommandHandler("refuser", cmd_refuser))
    application.add_handler(CommandHandler("resetxp", cmd_resetxp))

    # chat_member 업데이트 (초대 추적)
    application.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # 매일 23:59 KST (UTC 14:59)에 요약 전송
    kst_daily_time_utc = time(hour=14, minute=59, tzinfo=timezone.utc)
    application.job_queue.run_daily(
        send_daily_summary,
        time=kst_daily_time_utc,
        name="daily_summary",
    )

    logger.info("XP Bot started")
    await application.run_polling(close_loop=False)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
