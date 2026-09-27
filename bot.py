from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import discord
import fitz
import pytesseract
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("dnd-bot")


# =========================================================
# CONSTANTS
# =========================================================

Slot = Literal["Main", "Alt"]

SLOTS = [
    app_commands.Choice(name="Main", value="Main"),
    app_commands.Choice(name="Alt", value="Alt"),
]

DOWNTIME_CHOICES = [
    app_commands.Choice(name="8", value=8),
    app_commands.Choice(name="16", value=16),
    app_commands.Choice(name="24", value=24),
    app_commands.Choice(name="32", value=32),
    app_commands.Choice(name="40", value=40),
]

LEVEL_THRESHOLDS = {
    1: 0,
    2: 2,
    3: 3,
    4: 7,
    5: 11,
    6: 13,
    7: 19,
    8: 25,
    9: 28,
    10: 36,
    11: 44,
    12: 48,
    13: 58,
    14: 68,
    15: 73,
    16: 85,
    17: 97,
    18: 103,
    19: 116,
    20: 130,
}

RANK_RANGES = {
    "F": range(1, 3),
    "E": range(3, 6),
    "D": range(6, 9),
    "C": range(9, 13),
    "B": range(13, 16),
    "A": range(16, 19),
    "S": range(19, 21),
}

RANK_ORDER = (
    "F",
    "E",
    "D",
    "C",
    "B",
    "A",
    "S",
)

RANK_ROLE_NAMES = [
    f"{rank} Rank"
    for rank in RANK_ORDER
]

QUEST_RANK_CHOICES = [
    app_commands.Choice(
        name=f"{rank} Rank",
        value=rank,
    )
    for rank in RANK_ORDER
]

STAT_NAMES = (
    "Strength",
    "Dexterity",
    "Constitution",
    "Intelligence",
    "Wisdom",
    "Charisma",
)

TIMEZONE_ALIASES = {
    "eastern": "America/New_York",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "central": "America/Chicago",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "mountain": "America/Denver",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pacific": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "alaska": "America/Anchorage",
    "akst": "America/Anchorage",
    "akdt": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "hst": "Pacific/Honolulu",
}


# =========================================================
# PROGRESSION
# =========================================================

def level_for_mp(mp: int) -> int:
    mp = max(0, mp)

    return max(
        level
        for level, threshold in LEVEL_THRESHOLDS.items()
        if threshold <= mp
    )


def rank_for_level(level: int) -> str:
    return next(
        rank
        for rank, levels in RANK_RANGES.items()
        if level in levels
    )


def mp_until_next(
    level: int,
    mp: int,
) -> int:
    if level >= 20:
        return 0

    return max(
        0,
        LEVEL_THRESHOLDS[level + 1] - max(0, mp),
    )


# =========================================================
# PERMISSIONS
# =========================================================

def staff_only(
    interaction: discord.Interaction,
) -> bool:
    return (
        isinstance(
            interaction.user,
            discord.Member,
        )
        and any(
            role.name.casefold() in {"dm", "gm"}
            for role in interaction.user.roles
        )
    )


async def staff_check(
    interaction: discord.Interaction,
) -> bool:
    if staff_only(interaction):
        return True

    raise app_commands.CheckFailure(
        "Only members with the DM or GM role can use this command."
    )


# =========================================================
# DISCORD RANK
# =========================================================

def discord_rank_for_member(
    member: discord.Member,
) -> str | None:
    role_names = {
        role.name
        for role in member.roles
    }

    for rank in reversed(RANK_ORDER):
        if f"{rank} Rank" in role_names:
            return rank

    return None


# =========================================================
# QUEST HELPERS
# =========================================================

def eligible_ranks(
    designated_rank: str,
) -> list[str]:
    index = RANK_ORDER.index(
        designated_rank
    )

    start = max(
        0,
        index - 1,
    )

    end = min(
        len(RANK_ORDER),
        index + 2,
    )

    return list(
        RANK_ORDER[start:end]
    )


def member_is_quest_eligible(
    member: discord.Member,
    designated_rank: str,
) -> bool:
    allowed = {
        f"{rank} Rank"
        for rank in eligible_ranks(
            designated_rank
        )
    }

    return any(
        role.name in allowed
        for role in member.roles
    )


def normalize_timezone_name(
    value: str,
) -> str:
    cleaned = value.strip()

    alias = TIMEZONE_ALIASES.get(
        cleaned.casefold()
    )

    return alias or cleaned


def validate_timezone(
    value: str,
) -> str:
    timezone_name = (
        normalize_timezone_name(
            value
        )
    )

    try:
        ZoneInfo(
            timezone_name
        )

    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            "That timezone was not recognized. "
            "Try something like `America/Chicago`, "
            "`America/New_York`, `Central`, or `Eastern`."
        ) from exc

    return timezone_name


def parse_quest_datetime(
    value: str,
    timezone_name: str,
) -> datetime:
    text = re.sub(
        r"\s+",
        " ",
        value.strip(),
    )

    formats = (
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%y %I:%M %p",
        "%m/%d/%y %H:%M",
    )

    parsed = None

    for date_format in formats:
        try:
            parsed = datetime.strptime(
                text,
                date_format,
            )
            break

        except ValueError:
            continue

    if parsed is None:
        raise ValueError(
            "Use a date and time like "
            "`10/03/2026 7:00 PM` or "
            "`2026-10-03 19:00`."
        )

    local_zone = ZoneInfo(
        timezone_name
    )

    localized = parsed.replace(
        tzinfo=local_zone
    )

    return localized.astimezone(
        timezone.utc
    )


def parse_duration_minutes(
    value: str,
) -> int:
    text = value.strip().casefold()

    if text.isdigit():
        hours = int(text)

        if hours <= 0:
            raise ValueError(
                "Duration must be greater than zero."
            )

        return hours * 60

    match = re.fullmatch(
        r"\s*(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?"
        r"\s*(?:(\d+)\s*(?:m|min|mins|minute|minutes))?\s*",
        text,
    )

    if not match:
        raise ValueError(
            "Use a duration like `4 hours`, `4h`, "
            "`90m`, or `2h 30m`."
        )

    hours = int(
        match.group(1) or 0
    )

    minutes = int(
        match.group(2) or 0
    )

    total = (
        hours * 60
        + minutes
    )

    if total <= 0:
        raise ValueError(
            "Duration must be greater than zero."
        )

    return total


def format_duration(
    minutes: int,
) -> str:
    hours, remaining = divmod(
        minutes,
        60,
    )

    parts: list[str] = []

    if hours:
        parts.append(
            f"{hours}h"
        )

    if remaining:
        parts.append(
            f"{remaining}m"
        )

    return " ".join(parts) or "0m"


def valid_image_url(
    value: str | None,
) -> str | None:
    if not value:
        return None

    cleaned = value.strip()

    if not cleaned:
        return None

    parsed = urlparse(
        cleaned
    )

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        raise ValueError(
            "Image must be a normal `http://` or `https://` URL."
        )

    return cleaned


def signup_names(
    user_ids: list[int],
) -> str:
    if not user_ids:
        return "None"

    text = ", ".join(
        f"<@{user_id}>"
        for user_id in user_ids
    )

    if len(text) <= 1000:
        return text

    return (
        text[:995]
        + "..."
    )


def chunk_mentions(
    user_ids: list[int],
    max_length: int = 1800,
) -> list[str]:
    chunks: list[str] = []
    current = ""

    for user_id in user_ids:
        mention = f"<@{user_id}>"

        candidate = (
            f"{current} {mention}".strip()
        )

        if (
            current
            and len(candidate) > max_length
        ):
            chunks.append(
                current
            )

            current = mention

        else:
            current = candidate

    if current:
        chunks.append(
            current
        )

    return chunks


# =========================================================
# SHEET DATA
# =========================================================

@dataclass
class SheetData:
    name: str | None
    class_name: str | None
    subclass_name: str | None
    stats: dict[str, int]
    proficiencies: list[str]


def ability_modifier(
    score: int,
) -> int:
    return (score - 10) // 2


def normalize_field_name(
    value: str,
) -> str:
    return re.sub(
        r"[^a-z0-9]",
        "",
        value.casefold(),
    )


def find_form_field(
    fields: dict[str, str],
    *possible_names: str,
) -> str:
    normalized_fields = {
        normalize_field_name(key): value
        for key, value in fields.items()
    }

    for possible_name in possible_names:
        match = normalized_fields.get(
            normalize_field_name(
                possible_name
            )
        )

        if match:
            return match.strip()

    return ""


# =========================================================
# FALLBACK SHEET PARSER
# =========================================================

def parse_sheet_text(
    text: str,
) -> SheetData:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    stats: dict[str, int] = {}

    stat_aliases = {
        "Strength": ("STRENGTH", "STR"),
        "Dexterity": ("DEXTERITY", "DEX"),
        "Constitution": ("CONSTITUTION", "CON"),
        "Intelligence": ("INTELLIGENCE", "INT"),
        "Wisdom": ("WISDOM", "WIS"),
        "Charisma": ("CHARISMA", "CHA"),
    }

    for full_name, aliases in stat_aliases.items():
        for i, line in enumerate(lines):
            upper = line.upper()

            if any(
                alias == upper
                or upper.startswith(alias + " ")
                for alias in aliases
            ):
                numbers = re.findall(
                    r"\b([1-9]|[12][0-9]|30)\b",
                    line,
                )

                if not numbers:
                    for next_line in lines[
                        i + 1:i + 4
                    ]:
                        numbers = re.findall(
                            r"\b([1-9]|[12][0-9]|30)\b",
                            next_line,
                        )

                        if numbers:
                            break

                if numbers:
                    stats[
                        full_name
                    ] = int(
                        numbers[0]
                    )

                    break

    name = None

    for i, line in enumerate(lines):
        if re.search(
            r"CHARACTER\s+NAME",
            line,
            re.I,
        ):
            candidate = re.sub(
                r"CHARACTER\s+NAME\s*[:\-]?",
                "",
                line,
                flags=re.I,
            ).strip()

            if candidate:
                name = candidate

            elif i + 1 < len(lines):
                name = lines[
                    i + 1
                ]

            break

    class_name = None
    subclass_name = None

    for line in lines:
        match = re.search(
            r"(?:CLASS\s*&\s*LEVEL|CLASS\s+LEVEL)"
            r"\s*[:\-]?\s*"
            r"([A-Za-z][A-Za-z '\-]+?)"
            r"\s+\d{1,2}\b",
            line,
            re.I,
        )

        if match:
            class_name = (
                match.group(1).strip()
            )
            break

    for line in lines:
        subclass_match = re.search(
            r"\b([A-Za-z][A-Za-z '\-]*)"
            r"\s+Subclass\b"
            r".*?\|\s*([^*|\n]+)",
            line,
            re.I,
        )

        if subclass_match:
            if not class_name:
                class_name = (
                    subclass_match
                    .group(1)
                    .strip()
                )

            subclass_name = (
                subclass_match
                .group(2)
                .strip()
            )

            break

    proficiencies: list[str] = []

    start_index = None

    for i, line in enumerate(lines):
        if re.search(
            r"PROFICIENCIES"
            r"(?:\s*&\s*(?:LANGUAGES|TRAINING))?",
            line,
            re.I,
        ):
            start_index = i + 1
            break

    if start_index is not None:
        stop_headers = {
            "FEATURES",
            "FEATURES & TRAITS",
            "EQUIPMENT",
            "ATTACKS",
            "ATTACKS & SPELLCASTING",
            "SPELLS",
            "PERSONALITY TRAITS",
            "IDEALS",
            "BONDS",
            "FLAWS",
            "CLASS FEATURES",
        }

        ignored_terms = (
            "HIT POINT",
            "TEMP HP",
            "DEATH SAVE",
            "ARMOR CLASS",
            "INITIATIVE",
            "SPEED",
            "PROFICIENCY BONUS",
            "PASSIVE WISDOM",
            "CLASS",
            "LEVEL",
            "HIT DICE",
            "DEFENSE",
        )

        for line in lines[
            start_index:
        ]:
            upper = line.upper().strip()

            if upper in stop_headers:
                break

            if any(
                term in upper
                for term in ignored_terms
            ):
                continue

            entries = [
                item.strip(
                    " •\t"
                )
                for item in re.split(
                    r"[,;•]",
                    line,
                )
                if item.strip(
                    " •\t"
                )
            ]

            for entry in entries:
                if (
                    1 < len(entry) < 100
                    and entry not in proficiencies
                ):
                    proficiencies.append(
                        entry
                    )

    return SheetData(
        name=name,
        class_name=class_name,
        subclass_name=subclass_name,
        stats=stats,
        proficiencies=proficiencies,
    )


# =========================================================
# D&D BEYOND PDF PARSER
# =========================================================

def extract_sheet(
    attachment: discord.Attachment,
    payload: bytes,
) -> SheetData:
    filename = (
        attachment.filename.casefold()
    )

    content_type = (
        attachment.content_type or ""
    ).casefold()

    if (
        filename.endswith(".pdf")
        or content_type == "application/pdf"
    ):
        document = fitz.open(
            stream=payload,
            filetype="pdf",
        )

        fields: dict[str, str] = {}

        for page in document:
            widgets = page.widgets()

            if widgets:
                for widget in widgets:
                    if (
                        widget.field_name
                        and widget.field_value is not None
                    ):
                        fields[
                            widget.field_name
                        ] = str(
                            widget.field_value
                        ).strip()

        if fields:
            log.info(
                "D&D Beyond PDF form fields detected: %s",
                list(
                    fields.keys()
                ),
            )

            stats: dict[str, int] = {}

            field_to_stat = {
                "STR": "Strength",
                "DEX": "Dexterity",
                "CON": "Constitution",
                "INT": "Intelligence",
                "WIS": "Wisdom",
                "CHA": "Charisma",
            }

            for (
                field_name,
                stat_name,
            ) in field_to_stat.items():
                value = find_form_field(
                    fields,
                    field_name,
                )

                if value:
                    try:
                        score = int(
                            value
                        )

                        if 1 <= score <= 30:
                            stats[
                                stat_name
                            ] = score

                    except ValueError:
                        pass

            name = find_form_field(
                fields,
                "CharacterName",
                "Character Name",
            ) or None

            class_name = None

            class_level = find_form_field(
                fields,
                "CLASS LEVEL",
                "Class Level",
                "ClassLevel",
                "Class & Level",
            )

            if class_level:
                class_name = re.sub(
                    r"\s+\d{1,2}\s*$",
                    "",
                    class_level,
                ).strip() or None

            subclass_name = None

            features_text = " ".join(
                value
                for key, value in fields.items()
                if (
                    "feature"
                    in key.casefold()
                    or "trait"
                    in key.casefold()
                )
            )

            subclass_match = re.search(
                r"\b([A-Za-z][A-Za-z '\-]*)"
                r"\s+Subclass\b"
                r"[^|]{0,150}\|"
                r"\s*([^*|\n]+)",
                features_text,
                re.I,
            )

            if subclass_match:
                if not class_name:
                    class_name = (
                        subclass_match
                        .group(1)
                        .strip()
                    )

                subclass_name = (
                    subclass_match
                    .group(2)
                    .strip()
                )

            proficiencies: list[str] = []

            training = find_form_field(
                fields,
                "ProficienciesLang",
                "Proficiencies Lang",
                "Proficiencies & Languages",
                "Proficiencies & Training",
            )

            if training:
                sections = re.split(
                    r"===\s*"
                    r"(?:WEAPONS|TOOLS|LANGUAGES|ARMOR)"
                    r"\s*===",
                    training,
                    flags=re.I,
                )

                for section in sections:
                    section = (
                        section.strip()
                    )

                    if not section:
                        continue

                    entries = [
                        item.strip()
                        for item in re.split(
                            r"[,;\n]",
                            section,
                        )
                        if item.strip()
                    ]

                    for entry in entries:
                        if (
                            entry
                            not in proficiencies
                        ):
                            proficiencies.append(
                                entry
                            )

            log.info(
                "Parsed sheet class=%r subclass=%r",
                class_name,
                subclass_name,
            )

            return SheetData(
                name=name,
                class_name=class_name,
                subclass_name=subclass_name,
                stats=stats,
                proficiencies=proficiencies,
            )

        text = "\n".join(
            page.get_text(
                "text"
            )
            for page in document
        )

        if len(
            text.strip()
        ) < 100:
            pages: list[str] = []

            for page in document:
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(
                        2,
                        2,
                    ),
                    alpha=False,
                )

                image = Image.open(
                    io.BytesIO(
                        pix.tobytes(
                            "png"
                        )
                    )
                )

                pages.append(
                    pytesseract.image_to_string(
                        image
                    )
                )

            text = "\n".join(
                pages
            )

        return parse_sheet_text(
            text
        )

    if (
        content_type.startswith(
            "image/"
        )
        or filename.endswith(
            (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            )
        )
    ):
        image = Image.open(
            io.BytesIO(
                payload
            )
        )

        text = (
            pytesseract
            .image_to_string(
                image
            )
        )

        return parse_sheet_text(
            text
        )

    raise ValueError(
        "Upload a PDF, PNG, JPG, or WebP file."
    )


# =========================================================
# BOT
# =========================================================

class DndBot(commands.Bot):
    def __init__(
        self,
    ) -> None:
        intents = (
            discord.Intents.default()
        )

        super().__init__(
            command_prefix=(
                commands.when_mentioned
            ),
            intents=intents,
        )

        self.pool: (
            asyncpg.Pool | None
        ) = None

    async def setup_hook(
        self,
    ) -> None:
        database_url = os.environ[
            "DATABASE_URL"
        ]

        async def configure_connection(
            connection: asyncpg.Connection,
        ) -> None:
            for type_name in (
                "json",
                "jsonb",
            ):
                await connection.set_type_codec(
                    type_name,
                    schema="pg_catalog",
                    encoder=json.dumps,
                    decoder=json.loads,
                    format="text",
                )

        self.pool = (
            await asyncpg.create_pool(
                database_url,
                min_size=1,
                max_size=5,
                command_timeout=30,
                init=configure_connection,
            )
        )

        async with (
            self.pool.acquire()
        ) as connection:
            await connection.execute(
                SCHEMA
            )

        open_quests = (
            await self.pool.fetch(
                """
                SELECT quest_id, message_id
                FROM quests
                WHERE status='open'
                AND message_id IS NOT NULL
                """
            )
        )

        for quest_row in open_quests:
            self.add_view(
                QuestView(
                    int(
                        quest_row[
                            "quest_id"
                        ]
                    )
                ),
                message_id=int(
                    quest_row[
                        "message_id"
                    ]
                ),
            )

        if not (
            quest_reminder_loop
            .is_running()
        ):
            quest_reminder_loop.start()

        await self.tree.sync()

        log.info(
            "Synced global commands"
        )

    async def close(
        self,
    ) -> None:
        if (
            quest_reminder_loop
            .is_running()
        ):
            quest_reminder_loop.cancel()

        if self.pool:
            await self.pool.close()

        await super().close()


# =========================================================
# DATABASE
# =========================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,

    slot TEXT NOT NULL CHECK (
        slot IN ('Main', 'Alt')
    ),

    character_name TEXT NOT NULL,

    class_name TEXT,
    subclass_name TEXT,

    mp INTEGER NOT NULL DEFAULT 0 CHECK (
        mp >= 0
    ),

    level SMALLINT NOT NULL DEFAULT 1 CHECK (
        level BETWEEN 1 AND 20
    ),

    rank CHAR(1) NOT NULL DEFAULT 'F' CHECK (
        rank IN ('F','E','D','C','B','A','S')
    ),

    gold INTEGER NOT NULL DEFAULT 0 CHECK (
        gold >= 0
    ),

    materials JSONB NOT NULL DEFAULT '{}'::jsonb,
    proficiencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,

    downtime_hours SMALLINT NOT NULL DEFAULT 0 CHECK (
        downtime_hours BETWEEN 0 AND 40
    ),

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (
        guild_id,
        user_id,
        slot
    )
);

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS class_name TEXT;

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS subclass_name TEXT;

CREATE TABLE IF NOT EXISTS user_timezones (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    timezone_name TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (
        guild_id,
        user_id
    )
);

CREATE TABLE IF NOT EXISTS quests (
    quest_id BIGSERIAL PRIMARY KEY,

    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,

    creator_id BIGINT NOT NULL,

    title TEXT NOT NULL,
    description TEXT NOT NULL,

    designated_rank CHAR(1) NOT NULL CHECK (
        designated_rank IN ('F','E','D','C','B','A','S')
    ),

    timezone_name TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (
        duration_minutes > 0
    ),

    expected_rewards TEXT NOT NULL,

    max_players INTEGER NOT NULL CHECK (
        max_players > 0
    ),

    image_url TEXT,

    status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('open','deleted')
    ),

    reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quest_signups (
    quest_id BIGINT NOT NULL
        REFERENCES quests(quest_id)
        ON DELETE CASCADE,

    user_id BIGINT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN (
            'accepted',
            'tentative',
            'declined',
            'waitlist'
        )
    ),

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (
        quest_id,
        user_id
    )
);

CREATE INDEX IF NOT EXISTS quest_signups_waitlist_idx
ON quest_signups (
    quest_id,
    status,
    updated_at
);

CREATE INDEX IF NOT EXISTS quests_reminder_idx
ON quests (
    status,
    reminder_sent,
    start_at
);
"""


bot = DndBot()


def db() -> asyncpg.Pool:
    if bot.pool is None:
        raise RuntimeError(
            "Database is not connected"
        )

    return bot.pool


async def get_character(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: str,
):
    row = await db().fetchrow(
        """
        SELECT *
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        interaction.guild_id,
        member.id,
        slot,
    )

    if not row:
        await interaction.followup.send(
            (
                f"{member.display_name} "
                f"has no {slot} character yet. "
                "Use `/character create` first."
            ),
            ephemeral=True,
        )

    return row


# =========================================================
# RANK ROLE SYNC
# =========================================================

async def sync_rank_role(
    member: discord.Member,
) -> str | None:
    guild = member.guild

    highest_level = (
        await db().fetchval(
            """
            SELECT COALESCE(MAX(level), 1)
            FROM characters
            WHERE guild_id=$1
            AND user_id=$2
            """,
            guild.id,
            member.id,
        )
    )

    target_name = (
        f"{rank_for_level(int(highest_level))} Rank"
    )

    target = discord.utils.get(
        guild.roles,
        name=target_name,
    )

    removable = [
        role
        for role in member.roles
        if (
            role.name
            in RANK_ROLE_NAMES
            and role.name
            != target_name
        )
    ]

    try:
        if removable:
            await member.remove_roles(
                *removable,
                reason=(
                    "D&D rank updated "
                    f"to level {highest_level}"
                ),
            )

        if (
            target
            and target
            not in member.roles
        ):
            await member.add_roles(
                target,
                reason=(
                    "D&D rank updated "
                    f"to level {highest_level}"
                ),
            )

        if target:
            return None

        return (
            f"The `{target_name}` role "
            "does not exist yet."
        )

    except discord.Forbidden:
        return (
            "I could not update the rank role; "
            "move my bot role above all rank roles."
        )


async def finish_staff_change_silently(
    interaction: discord.Interaction,
) -> None:
    try:
        await (
            interaction
            .delete_original_response()
        )

    except (
        discord.NotFound,
        discord.HTTPException,
    ):
        pass


# =========================================================
# NUMERIC MUTATIONS
# =========================================================

async def mutate_number(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: str,
    field: str,
    amount: int,
    mode: str,
):
    await interaction.response.defer(
        ephemeral=True
    )

    row = await get_character(
        interaction,
        member,
        slot,
    )

    if not row:
        return

    current = int(
        row[field]
    )

    if mode == "set":
        new_value = amount

    elif mode == "add":
        new_value = (
            current + amount
        )

    else:
        new_value = (
            current - amount
        )

    if field == "mp":
        new_value = max(
            0,
            new_value,
        )

    elif field == "gold":
        if new_value < 0:
            await interaction.followup.send(
                "That would make gold negative.",
                ephemeral=True,
            )

            return

    elif field == "downtime_hours":
        if mode == "add":
            new_value = min(
                40,
                new_value,
            )

        elif mode == "remove":
            new_value = max(
                0,
                new_value,
            )

        else:
            new_value = max(
                0,
                min(
                    40,
                    new_value,
                ),
            )

    elif field == "level":
        if not (
            1 <= new_value <= 20
        ):
            await interaction.followup.send(
                "Level must stay between 1 and 20.",
                ephemeral=True,
            )

            return

    role_note = ""

    if field == "mp":
        new_level = (
            level_for_mp(
                new_value
            )
        )

        await db().execute(
            """
            UPDATE characters
            SET mp=$1,
                level=$2,
                rank=$3,
                updated_at=now()
            WHERE guild_id=$4
            AND user_id=$5
            AND slot=$6
            """,
            new_value,
            new_level,
            rank_for_level(
                new_level
            ),
            interaction.guild_id,
            member.id,
            slot,
        )

        role_note = (
            await sync_rank_role(
                member
            )
            or ""
        )

    elif field == "level":
        new_mp = (
            LEVEL_THRESHOLDS[
                new_value
            ]
        )

        await db().execute(
            """
            UPDATE characters
            SET level=$1,
                mp=$2,
                rank=$3,
                updated_at=now()
            WHERE guild_id=$4
            AND user_id=$5
            AND slot=$6
            """,
            new_value,
            new_mp,
            rank_for_level(
                new_value
            ),
            interaction.guild_id,
            member.id,
            slot,
        )

        role_note = (
            await sync_rank_role(
                member
            )
            or ""
        )

    else:
        allowed_fields = {
            "gold",
            "downtime_hours",
        }

        if field not in allowed_fields:
            raise ValueError(
                "Invalid numeric field."
            )

        await db().execute(
            f"""
            UPDATE characters
            SET {field}=$1,
                updated_at=now()
            WHERE guild_id=$2
            AND user_id=$3
            AND slot=$4
            """,
            new_value,
            interaction.guild_id,
            member.id,
            slot,
        )

    if role_note:
        await interaction.followup.send(
            role_note,
            ephemeral=True,
        )

    else:
        await finish_staff_change_silently(
            interaction
        )


# =========================================================
# COMMAND GROUPS
# =========================================================

character = app_commands.Group(
    name="character",
    description="Manage your two character slots",
)

sheet = app_commands.Group(
    name="sheet",
    description="Import a D&D Beyond character sheet",
)

downtime = app_commands.Group(
    name="downtime",
    description="Manage downtime hours",
)

mp_group = app_commands.Group(
    name="mp",
    description="DM/GM MP management",
)

level_group = app_commands.Group(
    name="level",
    description="DM/GM level management",
)

gold_group = app_commands.Group(
    name="gold",
    description="DM/GM gold management",
)

materials_group = app_commands.Group(
    name="materials",
    description="DM/GM material management",
)

timezone_group = app_commands.Group(
    name="timezone",
    description="Set your timezone for quest creation",
)


# =========================================================
# CHARACTER COMMANDS
# =========================================================

@character.command(
    name="create",
    description="Create or rename your Main or Alt character",
)
@app_commands.choices(
    slot=SLOTS
)
async def character_create(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    name: app_commands.Range[
        str,
        1,
        60,
    ],
):
    await db().execute(
        """
        INSERT INTO characters (
            guild_id,
            user_id,
            slot,
            character_name
        )
        VALUES ($1,$2,$3,$4)

        ON CONFLICT (
            guild_id,
            user_id,
            slot
        )
        DO UPDATE SET
            character_name=EXCLUDED.character_name,
            updated_at=now()
        """,
        interaction.guild_id,
        interaction.user.id,
        slot.value,
        name.strip(),
    )

    await interaction.response.send_message(
        (
            f"Your {slot.value} character "
            f"is now **{name.strip()}**."
        ),
        ephemeral=True,
    )


@character.command(
    name="delete",
    description="Delete one of your character records",
)
@app_commands.choices(
    slot=SLOTS
)
async def character_delete(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    confirmation: str,
):
    if confirmation != "DELETE":
        await interaction.response.send_message(
            (
                "Nothing was deleted. "
                "Type `DELETE` exactly to confirm."
            ),
            ephemeral=True,
        )

        return

    result = await db().execute(
        """
        DELETE FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        interaction.guild_id,
        interaction.user.id,
        slot.value,
    )

    if result.endswith("1"):
        await interaction.response.send_message(
            "Character deleted.",
            ephemeral=True,
        )

    else:
        await interaction.response.send_message(
            "That character did not exist.",
            ephemeral=True,
        )


# =========================================================
# INVENTORY
# =========================================================

@bot.tree.command(
    name="inventory",
    description="View your Main or Alt character inventory",
)
@app_commands.choices(
    slot=SLOTS
)
async def inventory(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
):
    await interaction.response.defer(
        ephemeral=True
    )

    row = await get_character(
        interaction,
        interaction.user,
        slot.value,
    )

    if not row:
        return

    materials = dict(
        row["materials"]
    )

    stats = dict(
        row["stats"]
    )

    profs = list(
        row["proficiencies"]
    )

    server_mp = max(
        0,
        int(
            row["mp"]
        ),
    )

    server_level = (
        level_for_mp(
            server_mp
        )
    )

    calculated_rank = (
        rank_for_level(
            server_level
        )
    )

    if (
        int(
            row["level"]
        )
        != server_level
        or str(
            row["rank"]
        )
        != calculated_rank
    ):
        await db().execute(
            """
            UPDATE characters
            SET level=$1,
                rank=$2,
                mp=$3,
                updated_at=now()
            WHERE guild_id=$4
            AND user_id=$5
            AND slot=$6
            """,
            server_level,
            calculated_rank,
            server_mp,
            interaction.guild_id,
            interaction.user.id,
            slot.value,
        )

    discord_rank = None

    if isinstance(
        interaction.user,
        discord.Member,
    ):
        discord_rank = (
            discord_rank_for_member(
                interaction.user
            )
        )

    display_rank = (
        discord_rank
        or calculated_rank
    )

    class_name = row[
        "class_name"
    ]

    subclass_name = row[
        "subclass_name"
    ]

    class_lines: list[str] = []

    if class_name:
        class_lines.append(
            f"Class: {class_name}"
        )

    if subclass_name:
        class_lines.append(
            f"Subclass: {subclass_name}"
        )

    class_display = (
        "\n".join(
            class_lines
        )
        if class_lines
        else None
    )

    embed = discord.Embed(
        title=(
            f"{interaction.user.display_name} "
            f"({slot.value})"
        ),
        description=class_display,
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="Level / Rank",
        value=(
            f"{server_level} / "
            f"{display_rank}"
        ),
    )

    embed.add_field(
        name="MP",
        value=(
            f"{server_mp} total\n"
            f"{mp_until_next(server_level, server_mp)} "
            "until next level"
        ),
    )

    embed.add_field(
        name="Gold",
        value=str(
            row["gold"]
        ),
    )

    embed.add_field(
        name="Downtime Hours",
        value=(
            f"{row['downtime_hours']} / 40"
        ),
    )

    stat_abbreviations = {
        "Strength": "STR",
        "Dexterity": "DEX",
        "Constitution": "CON",
        "Intelligence": "INT",
        "Wisdom": "WIS",
        "Charisma": "CHA",
    }

    stat_lines: list[str] = []

    for stat_name in STAT_NAMES:
        if stat_name in stats:
            score = int(
                stats[
                    stat_name
                ]
            )

            modifier = (
                ability_modifier(
                    score
                )
            )

            stat_lines.append(
                (
                    f"{stat_abbreviations[stat_name]} "
                    f"{score} "
                    f"({modifier:+d})"
                )
            )

    embed.add_field(
        name="Stats",
        value=(
            "\n".join(
                stat_lines
            )
            or "Not imported"
        ),
        inline=False,
    )

    embed.add_field(
        name="Proficiencies",
        value=(
            "\n".join(
                f"• {prof}"
                for prof in profs
            )[:1024]
            or "Not imported"
        ),
        inline=False,
    )

    embed.add_field(
        name="Materials",
        value=(
            "\n".join(
                (
                    f"{material}: "
                    f"{quantity}"
                )
                for (
                    material,
                    quantity,
                )
                in sorted(
                    materials.items()
                )
            )[:1024]
            or "None"
        ),
        inline=False,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


# =========================================================
# DOWNTIME
# =========================================================

@downtime.command(
    name="add",
    description="Add downtime hours",
)
@app_commands.choices(
    slot=SLOTS,
    hours=DOWNTIME_CHOICES,
)
@app_commands.check(
    staff_check
)
async def downtime_add(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    hours: app_commands.Choice[int],
):
    await mutate_number(
        interaction,
        member,
        slot.value,
        "downtime_hours",
        hours.value,
        "add",
    )


@downtime.command(
    name="remove",
    description="Remove downtime hours",
)
@app_commands.choices(
    slot=SLOTS,
    hours=DOWNTIME_CHOICES,
)
@app_commands.check(
    staff_check
)
async def downtime_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    hours: app_commands.Choice[int],
):
    await mutate_number(
        interaction,
        member,
        slot.value,
        "downtime_hours",
        hours.value,
        "remove",
    )


@downtime.command(
    name="set",
    description="Set downtime hours",
)
@app_commands.choices(
    slot=SLOTS,
    hours=DOWNTIME_CHOICES,
)
@app_commands.check(
    staff_check
)
async def downtime_set(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    hours: app_commands.Choice[int],
):
    await mutate_number(
        interaction,
        member,
        slot.value,
        "downtime_hours",
        hours.value,
        "set",
    )


# =========================================================
# SHEET IMPORT
# =========================================================

@sheet.command(
    name="import",
    description=(
        "Import a D&D Beyond PDF "
        "or character-sheet image"
    ),
)
@app_commands.choices(
    slot=SLOTS
)
async def sheet_import(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    file: discord.Attachment,
):
    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    if file.size > 12_000_000:
        await interaction.followup.send(
            "Please upload a file smaller than 12 MB.",
            ephemeral=True,
        )

        return

    existing = await get_character(
        interaction,
        interaction.user,
        slot.value,
    )

    if not existing:
        return

    try:
        parsed = extract_sheet(
            file,
            await file.read(),
        )

    except Exception as exc:
        log.warning(
            "Sheet import failed: %s",
            exc,
        )

        await interaction.followup.send(
            f"I couldn't read that sheet: {exc}",
            ephemeral=True,
        )

        return

    if (
        not parsed.stats
        and not parsed.proficiencies
        and not parsed.name
        and not parsed.class_name
        and not parsed.subclass_name
    ):
        await interaction.followup.send(
            (
                "I could not recognize character details. "
                "Try the exported D&D Beyond PDF."
            ),
            ephemeral=True,
        )

        return

    await db().execute(
        """
        UPDATE characters
        SET character_name=$1,
            class_name=$2,
            subclass_name=$3,
            stats=$4::jsonb,
            proficiencies=$5::jsonb,
            updated_at=now()
        WHERE guild_id=$6
        AND user_id=$7
        AND slot=$8
        """,
        (
            parsed.name
            or existing[
                "character_name"
            ]
        ),
        (
            parsed.class_name
            or existing[
                "class_name"
            ]
        ),
        (
            parsed.subclass_name
            or existing[
                "subclass_name"
            ]
        ),
        parsed.stats,
        parsed.proficiencies,
        interaction.guild_id,
        interaction.user.id,
        slot.value,
    )

    await interaction.followup.send(
        (
            "Sheet imported. "
            "Class, subclass, stats, and proficiencies updated. "
            "Server progression was preserved."
        ),
        ephemeral=True,
    )


# =========================================================
# GENERIC STAFF NUMERIC COMMANDS
# =========================================================

def install_numeric_commands(
    group: app_commands.Group,
    field: str,
    allow_set: bool = True,
):
    async def add(
        interaction: discord.Interaction,
        member: discord.Member,
        slot: app_commands.Choice[str],
        amount: app_commands.Range[
            int,
            1,
            1_000_000,
        ],
    ):
        await mutate_number(
            interaction,
            member,
            slot.value,
            field,
            amount,
            "add",
        )

    async def remove(
        interaction: discord.Interaction,
        member: discord.Member,
        slot: app_commands.Choice[str],
        amount: app_commands.Range[
            int,
            1,
            1_000_000,
        ],
    ):
        await mutate_number(
            interaction,
            member,
            slot.value,
            field,
            amount,
            "remove",
        )

    app_commands.choices(
        slot=SLOTS
    )(add)

    app_commands.choices(
        slot=SLOTS
    )(remove)

    add_command = (
        group.command(
            name="add",
            description=(
                f"Add {field.replace('_', ' ')}"
            ),
        )(add)
    )

    remove_command = (
        group.command(
            name="remove",
            description=(
                f"Remove {field.replace('_', ' ')}"
            ),
        )(remove)
    )

    add_command.add_check(
        staff_check
    )

    remove_command.add_check(
        staff_check
    )

    if allow_set:

        async def set_value(
            interaction: discord.Interaction,
            member: discord.Member,
            slot: app_commands.Choice[str],
            amount: app_commands.Range[
                int,
                0,
                1_000_000,
            ],
        ):
            await mutate_number(
                interaction,
                member,
                slot.value,
                field,
                amount,
                "set",
            )

        app_commands.choices(
            slot=SLOTS
        )(set_value)

        set_command = (
            group.command(
                name="set",
                description=(
                    f"Set {field.replace('_', ' ')}"
                ),
            )(set_value)
        )

        set_command.add_check(
            staff_check
        )


install_numeric_commands(
    mp_group,
    "mp",
)

install_numeric_commands(
    gold_group,
    "gold",
)


# =========================================================
# LEVEL
# =========================================================

@level_group.command(
    name="set",
    description=(
        "Set a character's level and "
        "MP to that level's threshold"
    ),
)
@app_commands.choices(
    slot=SLOTS
)
@app_commands.check(
    staff_check
)
async def level_set(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    level: app_commands.Range[
        int,
        1,
        20,
    ],
):
    await mutate_number(
        interaction,
        member,
        slot.value,
        "level",
        level,
        "set",
    )


# =========================================================
# MATERIALS
# =========================================================

@materials_group.command(
    name="add",
    description="Add a material to a character",
)
@app_commands.choices(
    slot=SLOTS
)
@app_commands.check(
    staff_check
)
async def materials_add(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    material: app_commands.Range[
        str,
        1,
        80,
    ],
    quantity: app_commands.Range[
        int,
        1,
        1_000_000,
    ],
):
    await interaction.response.defer(
        ephemeral=True
    )

    row = await get_character(
        interaction,
        member,
        slot.value,
    )

    if not row:
        return

    items = dict(
        row["materials"]
    )

    key = material.strip()

    items[key] = (
        int(
            items.get(
                key,
                0,
            )
        )
        + quantity
    )

    await db().execute(
        """
        UPDATE characters
        SET materials=$1::jsonb,
            updated_at=now()
        WHERE guild_id=$2
        AND user_id=$3
        AND slot=$4
        """,
        items,
        interaction.guild_id,
        member.id,
        slot.value,
    )

    await finish_staff_change_silently(
        interaction
    )


@materials_group.command(
    name="remove",
    description="Remove a material from a character",
)
@app_commands.choices(
    slot=SLOTS
)
@app_commands.check(
    staff_check
)
async def materials_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    material: app_commands.Range[
        str,
        1,
        80,
    ],
    quantity: app_commands.Range[
        int,
        1,
        1_000_000,
    ],
):
    await interaction.response.defer(
        ephemeral=True
    )

    row = await get_character(
        interaction,
        member,
        slot.value,
    )

    if not row:
        return

    items = dict(
        row["materials"]
    )

    key = material.strip()

    current = int(
        items.get(
            key,
            0,
        )
    )

    if current < quantity:
        await interaction.followup.send(
            (
                f"Only {current} × "
                f"{key} is available."
            ),
            ephemeral=True,
        )

        return

    remaining = (
        current - quantity
    )

    if remaining:
        items[
            key
        ] = remaining

    else:
        items.pop(
            key,
            None,
        )

    await db().execute(
        """
        UPDATE characters
        SET materials=$1::jsonb,
            updated_at=now()
        WHERE guild_id=$2
        AND user_id=$3
        AND slot=$4
        """,
        items,
        interaction.guild_id,
        member.id,
        slot.value,
    )

    await finish_staff_change_silently(
        interaction
    )


# =========================================================
# TIMEZONE
# =========================================================

@timezone_group.command(
    name="set",
    description="Set your timezone for creating quests",
)
async def timezone_set(
    interaction: discord.Interaction,
    timezone_name: str,
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=True,
        )
        return

    try:
        normalized = validate_timezone(
            timezone_name
        )

    except ValueError as exc:
        await interaction.response.send_message(
            str(exc),
            ephemeral=True,
        )
        return

    await db().execute(
        """
        INSERT INTO user_timezones (
            guild_id,
            user_id,
            timezone_name
        )
        VALUES ($1,$2,$3)

        ON CONFLICT (
            guild_id,
            user_id
        )
        DO UPDATE SET
            timezone_name=EXCLUDED.timezone_name,
            updated_at=now()
        """,
        interaction.guild_id,
        interaction.user.id,
        normalized,
    )

    await interaction.response.send_message(
        (
            "Quest timezone set to "
            f"`{normalized}`."
        ),
        ephemeral=True,
    )


# =========================================================
# QUEST DATABASE / EMBED HELPERS
# =========================================================

async def get_quest(
    quest_id: int,
):
    return await db().fetchrow(
        """
        SELECT *
        FROM quests
        WHERE quest_id=$1
        AND status='open'
        """,
        quest_id,
    )


async def get_quest_signup_map(
    quest_id: int,
) -> dict[str, list[int]]:
    rows = await db().fetch(
        """
        SELECT user_id, status
        FROM quest_signups
        WHERE quest_id=$1
        ORDER BY updated_at ASC, user_id ASC
        """,
        quest_id,
    )

    result: dict[
        str,
        list[int],
    ] = {
        "accepted": [],
        "waitlist": [],
        "tentative": [],
        "declined": [],
    }

    for row in rows:
        result[
            str(
                row["status"]
            )
        ].append(
            int(
                row["user_id"]
            )
        )

    return result


async def build_quest_embed(
    quest,
) -> discord.Embed:
    signup_map = (
        await get_quest_signup_map(
            int(
                quest["quest_id"]
            )
        )
    )

    accepted = signup_map[
        "accepted"
    ]

    waitlist = signup_map[
        "waitlist"
    ]

    tentative = signup_map[
        "tentative"
    ]

    declined = signup_map[
        "declined"
    ]

    ranks = eligible_ranks(
        str(
            quest[
                "designated_rank"
            ]
        )
    )

    start_at = quest[
        "start_at"
    ]

    if (
        start_at.tzinfo
        is None
    ):
        start_at = (
            start_at.replace(
                tzinfo=timezone.utc
            )
        )

    embed = discord.Embed(
        title=(
            f"⚔ {quest['title']}"
        ),
        description=str(
            quest[
                "description"
            ]
        ),
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="Rank",
        value=(
            f"{quest['designated_rank']} Rank\n"
            f"Eligible: {' • '.join(ranks)}"
        ),
        inline=True,
    )

    embed.add_field(
        name="Players",
        value=(
            f"{len(accepted)} / "
            f"{quest['max_players']}"
        ),
        inline=True,
    )

    embed.add_field(
        name="Duration",
        value=format_duration(
            int(
                quest[
                    "duration_minutes"
                ]
            )
        ),
        inline=True,
    )

    embed.add_field(
        name="Starts",
        value=(
            f"{discord.utils.format_dt(start_at, style='F')}\n"
            f"{discord.utils.format_dt(start_at, style='R')}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Expected Rewards",
        value=str(
            quest[
                "expected_rewards"
            ]
        )[:1024],
        inline=False,
    )

    signup_text = (
        f"**✅ Accepted ({len(accepted)})**\n"
        f"{signup_names(accepted)}\n\n"
        f"**⏳ Waitlist ({len(waitlist)})**\n"
        f"{signup_names(waitlist)}\n\n"
        f"**❓ Tentative ({len(tentative)})**\n"
        f"{signup_names(tentative)}\n\n"
        f"**❌ Declined ({len(declined)})**\n"
        f"{signup_names(declined)}"
    )

    embed.add_field(
        name="Signups",
        value=signup_text[:1024],
        inline=False,
    )

    embed.set_footer(
        text=(
            f"Quest #{quest['quest_id']} • "
            "Times display in your local timezone"
        )
    )

    image_url = quest[
        "image_url"
    ]

    if image_url:
        embed.set_image(
            url=str(
                image_url
            )
        )

    return embed


async def refresh_quest_message(
    quest_id: int,
) -> None:
    quest = await get_quest(
        quest_id
    )

    if not quest:
        return

    guild = bot.get_guild(
        int(
            quest[
                "guild_id"
            ]
        )
    )

    if guild is None:
        return

    channel = guild.get_channel(
        int(
            quest[
                "channel_id"
            ]
        )
    )

    if (
        channel is None
        or not isinstance(
            channel,
            (
                discord.TextChannel,
                discord.Thread,
            ),
        )
    ):
        return

    try:
        message = await channel.fetch_message(
            int(
                quest[
                    "message_id"
                ]
            )
        )

        embed = await build_quest_embed(
            quest
        )

        await message.edit(
            embed=embed,
            view=QuestView(
                quest_id
            ),
        )

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
    ) as exc:
        log.warning(
            "Could not refresh quest %s: %s",
            quest_id,
            exc,
        )


async def upsert_signup(
    connection: asyncpg.Connection,
    quest_id: int,
    user_id: int,
    status: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO quest_signups (
            quest_id,
            user_id,
            status
        )
        VALUES ($1,$2,$3)

        ON CONFLICT (
            quest_id,
            user_id
        )
        DO UPDATE SET
            status=EXCLUDED.status,
            updated_at=CASE
                WHEN quest_signups.status=EXCLUDED.status
                THEN quest_signups.updated_at
                ELSE now()
            END
        """,
        quest_id,
        user_id,
        status,
    )


async def promote_first_waitlisted(
    connection: asyncpg.Connection,
    quest_id: int,
) -> int | None:
    candidate = await connection.fetchrow(
        """
        SELECT user_id
        FROM quest_signups
        WHERE quest_id=$1
        AND status='waitlist'
        ORDER BY updated_at ASC, user_id ASC
        LIMIT 1
        FOR UPDATE
        """,
        quest_id,
    )

    if not candidate:
        return None

    user_id = int(
        candidate[
            "user_id"
        ]
    )

    await connection.execute(
        """
        UPDATE quest_signups
        SET status='accepted',
            updated_at=now()
        WHERE quest_id=$1
        AND user_id=$2
        """,
        quest_id,
        user_id,
    )

    return user_id


async def fill_open_quest_slots(
    connection: asyncpg.Connection,
    quest_id: int,
    max_players: int,
) -> list[int]:
    promoted: list[int] = []

    while True:
        accepted_count = int(
            await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM quest_signups
                WHERE quest_id=$1
                AND status='accepted'
                """,
                quest_id,
            )
        )

        if (
            accepted_count
            >= max_players
        ):
            break

        promoted_user = (
            await promote_first_waitlisted(
                connection,
                quest_id,
            )
        )

        if promoted_user is None:
            break

        promoted.append(
            promoted_user
        )

    return promoted


async def set_quest_signup(
    interaction: discord.Interaction,
    quest_id: int,
    requested_status: str,
) -> tuple[str, int | None]:
    if not isinstance(
        interaction.user,
        discord.Member,
    ):
        raise ValueError(
            "Use quest signups inside the server."
        )

    promoted_user: (
        int | None
    ) = None

    async with (
        db().acquire()
    ) as connection:
        async with (
            connection.transaction()
        ):
            quest = await connection.fetchrow(
                """
                SELECT *
                FROM quests
                WHERE quest_id=$1
                AND status='open'
                FOR UPDATE
                """,
                quest_id,
            )

            if not quest:
                raise ValueError(
                    "That quest no longer exists."
                )

            start_at = quest[
                "start_at"
            ]

            if (
                start_at
                <= datetime.now(
                    timezone.utc
                )
            ):
                raise ValueError(
                    "That quest has already started."
                )

            if not (
                member_is_quest_eligible(
                    interaction.user,
                    str(
                        quest[
                            "designated_rank"
                        ]
                    ),
                )
            ):
                allowed = ", ".join(
                    f"{rank} Rank"
                    for rank in eligible_ranks(
                        str(
                            quest[
                                "designated_rank"
                            ]
                        )
                    )
                )

                raise ValueError(
                    "This quest is for "
                    f"{allowed}."
                )

            previous_status = (
                await connection.fetchval(
                    """
                    SELECT status
                    FROM quest_signups
                    WHERE quest_id=$1
                    AND user_id=$2
                    FOR UPDATE
                    """,
                    quest_id,
                    interaction.user.id,
                )
            )

            actual_status = (
                requested_status
            )

            if (
                requested_status
                == "accepted"
            ):
                if (
                    previous_status
                    == "accepted"
                ):
                    actual_status = (
                        "accepted"
                    )

                else:
                    accepted_count = int(
                        await connection.fetchval(
                            """
                            SELECT COUNT(*)
                            FROM quest_signups
                            WHERE quest_id=$1
                            AND status='accepted'
                            """,
                            quest_id,
                        )
                    )

                    if (
                        accepted_count
                        >= int(
                            quest[
                                "max_players"
                            ]
                        )
                    ):
                        actual_status = (
                            "waitlist"
                        )

                await upsert_signup(
                    connection,
                    quest_id,
                    interaction.user.id,
                    actual_status,
                )

            else:
                await upsert_signup(
                    connection,
                    quest_id,
                    interaction.user.id,
                    requested_status,
                )

                if (
                    previous_status
                    == "accepted"
                ):
                    promoted_user = (
                        await promote_first_waitlisted(
                            connection,
                            quest_id,
                        )
                    )

            return (
                actual_status,
                promoted_user,
            )


# =========================================================
# QUEST MODALS
# =========================================================

class QuestCreateModal(
    discord.ui.Modal,
    title="Create Quest",
):
    quest_title = discord.ui.TextInput(
        label="Title",
        placeholder="The Shattered Crypt",
        max_length=100,
    )

    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="A short description of the quest.",
        max_length=1000,
    )

    start_time = discord.ui.TextInput(
        label="Start Date & Time",
        placeholder="10/03/2026 7:00 PM",
        max_length=40,
    )

    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="4 hours",
        max_length=30,
    )

    rewards = discord.ui.TextInput(
        label="Expected Rewards",
        placeholder="4 MP • 500 Gold • Rare materials",
        max_length=500,
    )

    def __init__(
        self,
        designated_rank: str,
        max_players: int,
        image_url: str | None,
        timezone_name: str,
    ) -> None:
        super().__init__()

        self.designated_rank = (
            designated_rank
        )

        self.max_players = (
            max_players
        )

        self.image_url = (
            image_url
        )

        self.timezone_name = (
            timezone_name
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if (
            interaction.guild
            is None
            or interaction.channel
            is None
        ):
            await interaction.response.send_message(
                "Create quests inside the server.",
                ephemeral=True,
            )
            return

        try:
            start_at = (
                parse_quest_datetime(
                    str(
                        self.start_time
                    ),
                    self.timezone_name,
                )
            )

            duration_minutes = (
                parse_duration_minutes(
                    str(
                        self.duration
                    )
                )
            )

        except ValueError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
            )
            return

        if (
            start_at
            <= datetime.now(
                timezone.utc
            )
        ):
            await interaction.response.send_message(
                "Quest start time must be in the future.",
                ephemeral=True,
            )
            return

        quest = await db().fetchrow(
            """
            INSERT INTO quests (
                guild_id,
                channel_id,
                creator_id,
                title,
                description,
                designated_rank,
                timezone_name,
                start_at,
                duration_minutes,
                expected_rewards,
                max_players,
                image_url
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,
                $7,$8,$9,$10,$11,$12
            )
            RETURNING *
            """,
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
            str(
                self.quest_title
            ).strip(),
            str(
                self.description
            ).strip(),
            self.designated_rank,
            self.timezone_name,
            start_at,
            duration_minutes,
            str(
                self.rewards
            ).strip(),
            self.max_players,
            self.image_url,
        )

        quest_id = int(
            quest[
                "quest_id"
            ]
        )

        embed = await build_quest_embed(
            quest
        )

        ping_roles: list[
            discord.Role
        ] = []

        for rank in eligible_ranks(
            self.designated_rank
        ):
            role = discord.utils.get(
                interaction.guild.roles,
                name=f"{rank} Rank",
            )

            if role:
                ping_roles.append(
                    role
                )

        content = " ".join(
            role.mention
            for role in ping_roles
        )

        await interaction.response.send_message(
            content=(
                content
                or None
            ),
            embed=embed,
            view=QuestView(
                quest_id
            ),
            allowed_mentions=discord.AllowedMentions(
                roles=True,
                users=False,
                everyone=False,
            ),
        )

        message = (
            await interaction.original_response()
        )

        await db().execute(
            """
            UPDATE quests
            SET message_id=$1,
                updated_at=now()
            WHERE quest_id=$2
            """,
            message.id,
            quest_id,
        )


class QuestEditDetailsModal(
    discord.ui.Modal,
    title="Edit Quest Details",
):
    def __init__(
        self,
        quest,
    ) -> None:
        super().__init__()

        timezone_name = str(
            quest[
                "timezone_name"
            ]
        )

        local_start = (
            quest[
                "start_at"
            ]
            .astimezone(
                ZoneInfo(
                    timezone_name
                )
            )
        )

        self.quest_id = int(
            quest[
                "quest_id"
            ]
        )

        self.timezone_name = (
            timezone_name
        )

        self.title_input = discord.ui.TextInput(
            label="Title",
            default=str(
                quest[
                    "title"
                ]
            )[:100],
            max_length=100,
        )

        self.description_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=str(
                quest[
                    "description"
                ]
            )[:1000],
            max_length=1000,
        )

        self.start_input = discord.ui.TextInput(
            label="Start Date & Time",
            default=local_start.strftime(
                "%m/%d/%Y %I:%M %p"
            ),
            max_length=40,
        )

        self.duration_input = discord.ui.TextInput(
            label="Duration",
            default=format_duration(
                int(
                    quest[
                        "duration_minutes"
                    ]
                )
            ),
            max_length=30,
        )

        self.rewards_input = discord.ui.TextInput(
            label="Expected Rewards",
            default=str(
                quest[
                    "expected_rewards"
                ]
            )[:500],
            max_length=500,
        )

        self.add_item(
            self.title_input
        )
        self.add_item(
            self.description_input
        )
        self.add_item(
            self.start_input
        )
        self.add_item(
            self.duration_input
        )
        self.add_item(
            self.rewards_input
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can edit quests.",
                ephemeral=True,
            )
            return

        try:
            start_at = (
                parse_quest_datetime(
                    str(
                        self.start_input
                    ),
                    self.timezone_name,
                )
            )

            duration_minutes = (
                parse_duration_minutes(
                    str(
                        self.duration_input
                    )
                )
            )

        except ValueError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
            )
            return

        if (
            start_at
            <= datetime.now(
                timezone.utc
            )
        ):
            await interaction.response.send_message(
                "Quest start time must be in the future.",
                ephemeral=True,
            )
            return

        await db().execute(
            """
            UPDATE quests
            SET title=$1,
                description=$2,
                start_at=$3,
                duration_minutes=$4,
                expected_rewards=$5,
                reminder_sent=FALSE,
                updated_at=now()
            WHERE quest_id=$6
            AND status='open'
            """,
            str(
                self.title_input
            ).strip(),
            str(
                self.description_input
            ).strip(),
            start_at,
            duration_minutes,
            str(
                self.rewards_input
            ).strip(),
            self.quest_id,
        )

        await refresh_quest_message(
            self.quest_id
        )

        await interaction.response.send_message(
            "Quest updated.",
            ephemeral=True,
        )


class QuestEditSettingsModal(
    discord.ui.Modal,
    title="Edit Quest Settings",
):
    def __init__(
        self,
        quest,
    ) -> None:
        super().__init__()

        self.quest_id = int(
            quest[
                "quest_id"
            ]
        )

        self.rank_input = discord.ui.TextInput(
            label="Designated Rank",
            placeholder="F, E, D, C, B, A, or S",
            default=str(
                quest[
                    "designated_rank"
                ]
            ),
            max_length=1,
        )

        self.max_players_input = (
            discord.ui.TextInput(
                label="Max Players",
                default=str(
                    quest[
                        "max_players"
                    ]
                ),
                max_length=10,
            )
        )

        self.image_input = discord.ui.TextInput(
            label="Image URL (optional)",
            default=str(
                quest[
                    "image_url"
                ]
                or ""
            )[:500],
            required=False,
            max_length=500,
        )

        self.add_item(
            self.rank_input
        )
        self.add_item(
            self.max_players_input
        )
        self.add_item(
            self.image_input
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can edit quests.",
                ephemeral=True,
            )
            return

        rank = (
            str(
                self.rank_input
            )
            .strip()
            .upper()
        )

        if rank not in RANK_ORDER:
            await interaction.response.send_message(
                "Rank must be F, E, D, C, B, A, or S.",
                ephemeral=True,
            )
            return

        try:
            max_players = int(
                str(
                    self.max_players_input
                ).strip()
            )

        except ValueError:
            await interaction.response.send_message(
                "Max Players must be a whole number.",
                ephemeral=True,
            )
            return

        if max_players <= 0:
            await interaction.response.send_message(
                "Max Players must be at least 1.",
                ephemeral=True,
            )
            return

        try:
            image_url = valid_image_url(
                str(
                    self.image_input
                )
            )

        except ValueError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
            )
            return

        async with (
            db().acquire()
        ) as connection:
            async with (
                connection.transaction()
            ):
                quest = (
                    await connection.fetchrow(
                        """
                        SELECT *
                        FROM quests
                        WHERE quest_id=$1
                        AND status='open'
                        FOR UPDATE
                        """,
                        self.quest_id,
                    )
                )

                if not quest:
                    await interaction.response.send_message(
                        "That quest no longer exists.",
                        ephemeral=True,
                    )
                    return

                accepted_count = int(
                    await connection.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM quest_signups
                        WHERE quest_id=$1
                        AND status='accepted'
                        """,
                        self.quest_id,
                    )
                )

                if (
                    max_players
                    < accepted_count
                ):
                    await interaction.response.send_message(
                        (
                            "Max Players cannot be lower than "
                            f"the current {accepted_count} accepted players."
                        ),
                        ephemeral=True,
                    )
                    return

                await connection.execute(
                    """
                    UPDATE quests
                    SET designated_rank=$1,
                        max_players=$2,
                        image_url=$3,
                        updated_at=now()
                    WHERE quest_id=$4
                    """,
                    rank,
                    max_players,
                    image_url,
                    self.quest_id,
                )

                await fill_open_quest_slots(
                    connection,
                    self.quest_id,
                    max_players,
                )

        await refresh_quest_message(
            self.quest_id
        )

        await interaction.response.send_message(
            "Quest settings updated.",
            ephemeral=True,
        )


# =========================================================
# QUEST VIEWS
# =========================================================

class QuestEditChooser(
    discord.ui.View,
):
    def __init__(
        self,
        quest_id: int,
    ) -> None:
        super().__init__(
            timeout=120
        )

        self.quest_id = (
            quest_id
        )

    @discord.ui.button(
        label="Details",
        style=discord.ButtonStyle.primary,
    )
    async def details(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can edit quests.",
                ephemeral=True,
            )
            return

        quest = await get_quest(
            self.quest_id
        )

        if not quest:
            await interaction.response.send_message(
                "That quest no longer exists.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            QuestEditDetailsModal(
                quest
            )
        )

    @discord.ui.button(
        label="Rank / Players / Image",
        style=discord.ButtonStyle.secondary,
    )
    async def settings(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can edit quests.",
                ephemeral=True,
            )
            return

        quest = await get_quest(
            self.quest_id
        )

        if not quest:
            await interaction.response.send_message(
                "That quest no longer exists.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            QuestEditSettingsModal(
                quest
            )
        )


class QuestDeleteConfirmView(
    discord.ui.View,
):
    def __init__(
        self,
        quest_id: int,
    ) -> None:
        super().__init__(
            timeout=60
        )

        self.quest_id = (
            quest_id
        )

    @discord.ui.button(
        label="Confirm Delete",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can delete quests.",
                ephemeral=True,
            )
            return

        quest = await get_quest(
            self.quest_id
        )

        if not quest:
            await interaction.response.send_message(
                "That quest no longer exists.",
                ephemeral=True,
            )
            return

        await db().execute(
            """
            DELETE FROM quests
            WHERE quest_id=$1
            """,
            self.quest_id,
        )

        guild = bot.get_guild(
            int(
                quest[
                    "guild_id"
                ]
            )
        )

        if guild:
            channel = guild.get_channel(
                int(
                    quest[
                        "channel_id"
                    ]
                )
            )

            if (
                channel
                and isinstance(
                    channel,
                    (
                        discord.TextChannel,
                        discord.Thread,
                    ),
                )
            ):
                try:
                    message = (
                        await channel.fetch_message(
                            int(
                                quest[
                                    "message_id"
                                ]
                            )
                        )
                    )

                    await message.delete()

                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    pass

        await interaction.response.edit_message(
            content="Quest deleted.",
            view=None,
        )


class QuestView(
    discord.ui.View,
):
    def __init__(
        self,
        quest_id: int,
    ) -> None:
        super().__init__(
            timeout=None
        )

        self.quest_id = (
            quest_id
        )

        accept = discord.ui.Button(
            label="Accept",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"quest:{quest_id}:accept"
            ),
        )

        tentative = discord.ui.Button(
            label="Tentative",
            emoji="❓",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"quest:{quest_id}:tentative"
            ),
        )

        decline = discord.ui.Button(
            label="Decline",
            emoji="❌",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"quest:{quest_id}:decline"
            ),
        )

        edit = discord.ui.Button(
            label="Edit",
            emoji="✏️",
            style=discord.ButtonStyle.primary,
            custom_id=(
                f"quest:{quest_id}:edit"
            ),
        )

        delete = discord.ui.Button(
            label="Delete",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"quest:{quest_id}:delete"
            ),
        )

        accept.callback = (
            self.accept_callback
        )

        tentative.callback = (
            self.tentative_callback
        )

        decline.callback = (
            self.decline_callback
        )

        edit.callback = (
            self.edit_callback
        )

        delete.callback = (
            self.delete_callback
        )

        self.add_item(
            accept
        )
        self.add_item(
            tentative
        )
        self.add_item(
            decline
        )
        self.add_item(
            edit
        )
        self.add_item(
            delete
        )

    async def handle_signup(
        self,
        interaction: discord.Interaction,
        status: str,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True
        )

        try:
            actual_status, promoted = (
                await set_quest_signup(
                    interaction,
                    self.quest_id,
                    status,
                )
            )

        except ValueError as exc:
            await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
            return

        await refresh_quest_message(
            self.quest_id
        )

        if (
            status == "accepted"
            and actual_status
            == "waitlist"
        ):
            message = (
                "The quest is full, so you were "
                "added to the waitlist."
            )

        elif actual_status == "accepted":
            message = (
                "You are accepted for this quest."
            )

        elif actual_status == "tentative":
            message = (
                "You are marked as tentative."
            )

        else:
            message = (
                "You are marked as declined."
            )

        if promoted is not None:
            message += (
                f"\n<@{promoted}> was automatically "
                "promoted from the waitlist."
            )

        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def accept_callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.handle_signup(
            interaction,
            "accepted",
        )

    async def tentative_callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.handle_signup(
            interaction,
            "tentative",
        )

    async def decline_callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.handle_signup(
            interaction,
            "declined",
        )

    async def edit_callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can edit quests.",
                ephemeral=True,
            )
            return

        quest = await get_quest(
            self.quest_id
        )

        if not quest:
            await interaction.response.send_message(
                "That quest no longer exists.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "What do you want to edit?",
            view=QuestEditChooser(
                self.quest_id
            ),
            ephemeral=True,
        )

    async def delete_callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not staff_only(
            interaction
        ):
            await interaction.response.send_message(
                "Only DM or GM roles can delete quests.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Delete this quest?",
            view=QuestDeleteConfirmView(
                self.quest_id
            ),
            ephemeral=True,
        )


# =========================================================
# QUEST COMMAND
# =========================================================

@bot.tree.command(
    name="quest",
    description="Create a quest for the server",
)
@app_commands.choices(
    rank=QUEST_RANK_CHOICES
)
@app_commands.check(
    staff_check
)
async def quest_command(
    interaction: discord.Interaction,
    rank: app_commands.Choice[str],
    max_players: int,
    image_url: str | None = None,
):
    if (
        interaction.guild_id
        is None
    ):
        await interaction.response.send_message(
            "Create quests inside the server.",
            ephemeral=True,
        )
        return

    if max_players <= 0:
        await interaction.response.send_message(
            "Max players must be at least 1.",
            ephemeral=True,
        )
        return

    try:
        cleaned_image = valid_image_url(
            image_url
        )

    except ValueError as exc:
        await interaction.response.send_message(
            str(exc),
            ephemeral=True,
        )
        return

    timezone_name = (
        await db().fetchval(
            """
            SELECT timezone_name
            FROM user_timezones
            WHERE guild_id=$1
            AND user_id=$2
            """,
            interaction.guild_id,
            interaction.user.id,
        )
    )

    if not timezone_name:
        await interaction.response.send_message(
            (
                "Set your timezone first with "
                "`/timezone set`. "
                "For Central Time you can use "
                "`America/Chicago` or `Central`."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        QuestCreateModal(
            designated_rank=rank.value,
            max_players=max_players,
            image_url=cleaned_image,
            timezone_name=str(
                timezone_name
            ),
        )
    )


# =========================================================
# QUEST REMINDERS
# =========================================================

@tasks.loop(
    seconds=30
)
async def quest_reminder_loop():
    rows = await db().fetch(
        """
        SELECT *
        FROM quests
        WHERE status='open'
        AND reminder_sent=FALSE
        AND start_at > now()
        AND start_at <= (
            now() + interval '5 minutes'
        )
        ORDER BY start_at ASC
        """
    )

    for quest in rows:
        quest_id = int(
            quest[
                "quest_id"
            ]
        )

        updated = (
            await db().execute(
                """
                UPDATE quests
                SET reminder_sent=TRUE,
                    updated_at=now()
                WHERE quest_id=$1
                AND reminder_sent=FALSE
                """,
                quest_id,
            )
        )

        if not updated.endswith(
            "1"
        ):
            continue

        signup_rows = (
            await db().fetch(
                """
                SELECT user_id
                FROM quest_signups
                WHERE quest_id=$1
                AND status IN (
                    'accepted',
                    'waitlist'
                )
                ORDER BY
                    CASE
                        WHEN status='accepted'
                        THEN 0
                        ELSE 1
                    END,
                    updated_at ASC
                """,
                quest_id,
            )
        )

        user_ids = [
            int(
                row[
                    "user_id"
                ]
            )
            for row in signup_rows
        ]

        if not user_ids:
            continue

        guild = bot.get_guild(
            int(
                quest[
                    "guild_id"
                ]
            )
        )

        if guild is None:
            continue

        channel = guild.get_channel(
            int(
                quest[
                    "channel_id"
                ]
            )
        )

        if (
            channel is None
            or not isinstance(
                channel,
                (
                    discord.TextChannel,
                    discord.Thread,
                ),
            )
        ):
            continue

        mention_chunks = (
            chunk_mentions(
                user_ids
            )
        )

        for index, mentions in enumerate(
            mention_chunks
        ):
            if index == 0:
                content = (
                    "⏰ **Quest starts in 5 minutes:** "
                    f"**{quest['title']}**\n"
                    f"{mentions}"
                )

            else:
                content = mentions

            try:
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        roles=False,
                        everyone=False,
                    ),
                )

            except (
                discord.Forbidden,
                discord.HTTPException,
            ) as exc:
                log.warning(
                    "Could not send reminder for quest %s: %s",
                    quest_id,
                    exc,
                )
                break


@quest_reminder_loop.before_loop
async def before_quest_reminder_loop():
    await bot.wait_until_ready()


# =========================================================
# REGISTER GROUPS
# =========================================================

for group in (
    character,
    sheet,
    downtime,
    mp_group,
    level_group,
    gold_group,
    materials_group,
    timezone_group,
):
    bot.tree.add_command(
        group
    )


# =========================================================
# ERRORS
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(
        error,
        app_commands.CheckFailure,
    ):
        message = (
            "Only members with the DM or GM role "
            "can use that command."
        )

    else:
        message = str(
            error
        )

    log.warning(
        "Command error: %s",
        error,
    )

    if (
        interaction
        .response
        .is_done()
    ):
        await interaction.followup.send(
            message,
            ephemeral=True,
        )

    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    log.info(
        "Connected as %s (%s)",
        bot.user,
        (
            bot.user.id
            if bot.user
            else "unknown"
        ),
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    token = os.getenv(
        "DISCORD_TOKEN"
    )

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is required"
        )

    bot.run(
        token,
        log_handler=None,
    )
