from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

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


QUEST_REWARD_MP_CHOICES = [
    app_commands.Choice(name="1 MP", value=1),
    app_commands.Choice(name="2 MP", value=2),
    app_commands.Choice(name="3 MP", value=3),
]


JOB_CHOICES = [
    app_commands.Choice(name="Soldier (Strength)", value="Soldier"),
    app_commands.Choice(name="Thief (Dexterity)", value="Thief"),
    app_commands.Choice(name="Farmer (Constitution)", value="Farmer"),
    app_commands.Choice(name="Teacher (Intelligence)", value="Teacher"),
    app_commands.Choice(name="Priest (Wisdom)", value="Priest"),
    app_commands.Choice(name="Performer (Charisma)", value="Performer"),
]

JOB_ABILITIES = {
    "Soldier": "Strength",
    "Thief": "Dexterity",
    "Farmer": "Constitution",
    "Teacher": "Intelligence",
    "Priest": "Wisdom",
    "Performer": "Charisma",
}

JOB_RANK_PAY = {
    "F": 25,
    "E": 50,
    "D": 100,
    "C": 250,
    "B": 400,
    "A": 600,
    "S": 1000,
}

JOB_DC = 16

TRAINING_TYPE_CHOICES = [
    app_commands.Choice(
        name="Language / Proficiency (50 Gold, 10 weeks)",
        value="Language / Proficiency",
    ),
    app_commands.Choice(
        name="Feat (200 Gold, 15 weeks)",
        value="Feat",
    ),
]

TRAINING_RULES = {
    "Language / Proficiency": {
        "cost": 50,
        "weeks": 10,
    },
    "Feat": {
        "cost": 200,
        "weeks": 15,
    },
}

TRAINING_DOWNTIME_COST = 40

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

STAT_NAMES = (
    "Strength",
    "Dexterity",
    "Constitution",
    "Intelligence",
    "Wisdom",
    "Charisma",
)

FACTION_ROLE_NAMES = (
    "Free Blade",
    "White Flag",
    "Blighted",
    "Saviors",
    "Hysteria",
)


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


def faction_for_member(
    member: discord.Member,
) -> str | None:
    role_names = {
        role.name
        for role in member.roles
    }

    factions = [
        faction
        for faction in FACTION_ROLE_NAMES
        if faction in role_names
    ]

    if not factions:
        return None

    return " / ".join(factions)


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
                    for next_line in lines[i + 1:i + 4]:
                        numbers = re.findall(
                            r"\b([1-9]|[12][0-9]|30)\b",
                            next_line,
                        )

                        if numbers:
                            break

                if numbers:
                    stats[full_name] = int(
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
                name = lines[i + 1]

            break

    class_name = None
    subclass_name = None

    # Try common class + level layouts.
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

    # Search for "... Wizard Subclass ... | Occultist"
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

        for line in lines[start_index:]:
            upper = line.upper().strip()

            if upper in stop_headers:
                break

            if any(
                term in upper
                for term in ignored_terms
            ):
                continue

            entries = [
                item.strip(" •\t")
                for item in re.split(
                    r"[,;•]",
                    line,
                )
                if item.strip(" •\t")
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
                list(fields.keys()),
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
                        score = int(value)

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

            # -------------------------------------------------
            # CLASS
            #
            # D&D Beyond PDFs can vary slightly in field naming.
            # We normalize the field names before looking them up.
            #
            # IMPORTANT: We strip the sheet level off and never
            # use it for server progression.
            # -------------------------------------------------

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

            # -------------------------------------------------
            # SUBCLASS
            # -------------------------------------------------

            subclass_name = None

            features_text = " ".join(
                value
                for key, value in fields.items()
                if "feature" in key.casefold()
                or "trait" in key.casefold()
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
                # If class field failed, recover the class
                # from "Wizard Subclass ..."
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

            # -------------------------------------------------
            # PROFICIENCIES
            # -------------------------------------------------

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
                    section = section.strip()

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
                        if entry not in proficiencies:
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

        # PDF without useful form fields
        text = "\n".join(
            page.get_text("text")
            for page in document
        )

        if len(text.strip()) < 100:
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
                        pix.tobytes("png")
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
        content_type.startswith("image/")
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
            io.BytesIO(payload)
        )

        text = (
            pytesseract.image_to_string(
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
    def __init__(self) -> None:
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

        async with self.pool.acquire() as connection:
            await connection.execute(
                SCHEMA
            )

        # Re-register persistent UI views after every restart.
        self.add_view(JobBoardView())
        self.add_view(TrainingBoardView())
        self.add_view(InventoryBoardView())

        active_auctions = await self.pool.fetch(
            """
            SELECT auction_id, message_id, buy_now_price
            FROM auctions
            WHERE status='active'
            AND message_id IS NOT NULL
            """
        )

        for auction in active_auctions:
            self.add_view(
                AuctionView(
                    auction_id=int(auction["auction_id"]),
                    has_buy_now=auction["buy_now_price"] is not None,
                ),
                message_id=int(auction["message_id"]),
            )

        if not auction_expiry_loop.is_running():
            auction_expiry_loop.start()

        await self.tree.sync()

        log.info(
            "Synced global commands"
        )

    async def close(
        self,
    ) -> None:
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

    training_name TEXT,
    training_type TEXT,
    training_week SMALLINT NOT NULL DEFAULT 0 CHECK (
        training_week BETWEEN 0 AND 15
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

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS training_name TEXT;

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS training_type TEXT;

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS training_week SMALLINT NOT NULL DEFAULT 0;

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS faction TEXT;

ALTER TABLE characters
ADD COLUMN IF NOT EXISTS faction_points INTEGER NOT NULL DEFAULT 0 CHECK (
    faction_points >= 0
);

CREATE TABLE IF NOT EXISTS auctions (
    auction_id BIGSERIAL PRIMARY KEY,

    guild_id BIGINT NOT NULL,
    channel_id BIGINT,
    message_id BIGINT,
    thread_id BIGINT,

    seller_user_id BIGINT NOT NULL,
    seller_slot TEXT NOT NULL CHECK (
        seller_slot IN ('Main', 'Alt')
    ),

    item_name TEXT NOT NULL,
    item_description TEXT NOT NULL,

    starting_price INTEGER NOT NULL CHECK (
        starting_price > 0
    ),

    buy_now_price INTEGER CHECK (
        buy_now_price IS NULL
        OR buy_now_price > 0
    ),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ends_at TIMESTAMPTZ NOT NULL,

    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN (
            'active',
            'completed',
            'ended_no_bids',
            'ended_no_valid_bid',
            'cancelled'
        )
    ),

    winner_user_id BIGINT,
    winner_slot TEXT CHECK (
        winner_slot IS NULL
        OR winner_slot IN ('Main', 'Alt')
    ),
    winning_amount INTEGER
);

CREATE TABLE IF NOT EXISTS auction_bids (
    auction_id BIGINT NOT NULL
        REFERENCES auctions(auction_id)
        ON DELETE CASCADE,

    guild_id BIGINT NOT NULL,
    bidder_user_id BIGINT NOT NULL,
    bidder_slot TEXT NOT NULL CHECK (
        bidder_slot IN ('Main', 'Alt')
    ),

    amount INTEGER NOT NULL CHECK (
        amount > 0
    ),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (
        auction_id,
        bidder_user_id,
        bidder_slot
    )
);

CREATE INDEX IF NOT EXISTS auctions_active_ends_idx
ON auctions (ends_at)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS auction_bids_bidder_idx
ON auction_bids (
    guild_id,
    bidder_user_id,
    bidder_slot
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
    *,
    ephemeral: bool = True,
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
            ephemeral=ephemeral,
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
            role.name in RANK_ROLE_NAMES
            and role.name != target_name
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
            and target not in member.roles
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

    # MP has a hard floor of zero.
    if field == "mp":
        new_value = max(
            0,
            new_value,
        )

    # Gold still cannot become negative.
    elif field == "gold":
        if new_value < 0:
            await interaction.followup.send(
                "That would make gold negative.",
                ephemeral=True,
            )

            return

    # Downtime remains between 0 and 40.
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

faction_points_group = app_commands.Group(
    name="factionpoints",
    description="DM/GM faction point management",
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
        ephemeral=False,
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
            ephemeral=False,
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
            ephemeral=False,
        )

    else:
        await interaction.response.send_message(
            "That character did not exist.",
            ephemeral=False,
        )


# =========================================================
# INVENTORY
# =========================================================

async def build_inventory_embed(
    *,
    guild_id: int,
    member: discord.Member | discord.User,
    slot: str,
) -> tuple[discord.Embed | None, str | None]:
    row = await db().fetchrow(
        """
        SELECT *
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        guild_id,
        member.id,
        slot,
    )

    if not row:
        return (
            None,
            (
                f"You do not have a {slot} character yet. "
                "Use `/character create` first."
            ),
        )

    materials = dict(row["materials"])
    stats = dict(row["stats"])
    profs = list(row["proficiencies"])

    server_mp = max(0, int(row["mp"]))
    server_level = level_for_mp(server_mp)
    calculated_rank = rank_for_level(server_level)

    if (
        int(row["level"]) != server_level
        or str(row["rank"]) != calculated_rank
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
            guild_id,
            member.id,
            slot,
        )

    discord_rank = None

    if isinstance(member, discord.Member):
        discord_rank = discord_rank_for_member(member)

    display_rank = discord_rank or calculated_rank

    display_faction = row["faction"]

    if isinstance(member, discord.Member):
        role_faction = faction_for_member(member)

        if role_faction != display_faction:
            await db().execute(
                """
                UPDATE characters
                SET faction=$1,
                    updated_at=now()
                WHERE guild_id=$2
                AND user_id=$3
                AND slot=$4
                """,
                role_faction,
                guild_id,
                member.id,
                slot,
            )
            display_faction = role_faction

    class_name = row["class_name"]
    subclass_name = row["subclass_name"]

    class_lines: list[str] = []

    if class_name:
        class_lines.append(f"Class: {class_name}")

    if subclass_name:
        class_lines.append(f"Subclass: {subclass_name}")

    class_display = (
        "\n".join(class_lines)
        if class_lines
        else None
    )

    embed = discord.Embed(
        title=f"{member.display_name} ({slot})",
        description=class_display,
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="Level / Rank",
        value=f"{server_level} / {display_rank}",
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
        value=str(row["gold"]),
    )

    embed.add_field(
        name="Downtime Hours",
        value=f"{row['downtime_hours']} / 40",
    )

    embed.add_field(
        name="Faction",
        value=(
            str(display_faction)
            if display_faction
            else "None"
        ),
    )

    embed.add_field(
        name="Faction Points",
        value=str(
            int(row["faction_points"])
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
            score = int(stats[stat_name])
            modifier = ability_modifier(score)

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
            "\n".join(stat_lines)
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
                for material, quantity
                in sorted(materials.items())
            )[:1024]
            or "None"
        ),
        inline=False,
    )

    return embed, None


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
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(
        ephemeral=False
    )

    embed, error = await build_inventory_embed(
        guild_id=interaction.guild_id,
        member=interaction.user,
        slot=slot.value,
    )

    if error:
        await interaction.followup.send(
            error,
            ephemeral=False,
        )
        return

    await interaction.followup.send(
        embed=embed,
        ephemeral=False,
    )


class InventorySessionView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=300)

        self.user_id = user_id
        self.guild_id = guild_id
        self.selected_slot: str | None = None

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=0,
        )

        self.view_button = discord.ui.Button(
            label="View Inventory",
            emoji="🎒",
            style=discord.ButtonStyle.success,
            row=1,
        )

        self.slot_select.callback = self.slot_changed
        self.view_button.callback = self.view_inventory

        self.add_item(self.slot_select)
        self.add_item(self.view_button)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
        ):
            return True

        await interaction.response.send_message(
            "This inventory menu belongs to another player.",
            ephemeral=True,
        )
        return False

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = self.slot_select.values[0]
        await interaction.response.defer()

    async def view_inventory(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        embed, error = await build_inventory_embed(
            guild_id=self.guild_id,
            member=interaction.user,
            slot=self.selected_slot,
        )

        if error:
            await interaction.edit_original_response(
                content=error,
                embed=None,
                view=self,
            )
            return

        if interaction.channel is None:
            await interaction.edit_original_response(
                content="I could not post the inventory in this channel.",
                embed=None,
                view=None,
            )
            return

        await interaction.channel.send(
            embed=embed,
        )

        await interaction.edit_original_response(
            content="Inventory posted.",
            embed=None,
            view=None,
        )


class InventoryBoardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="View Inventory",
        emoji="🎒",
        style=discord.ButtonStyle.primary,
        custom_id="astrelius:inventoryboard:view",
    )
    async def view_inventory(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Use this inside the server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎒 Inventory",
            description="Choose your character.",
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            view=InventorySessionView(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
            ),
            ephemeral=True,
        )


@bot.tree.command(
    name="inventoryboard",
    description="Post the Astrelius Inventory panel",
)
@app_commands.check(
    staff_check
)
async def inventoryboard_command(
    interaction: discord.Interaction,
):
    embed = discord.Embed(
        title="🎒 Astrelius Inventory",
        description="View your character information and resources.",
        color=discord.Color.gold(),
    )

    await interaction.response.send_message(
        embed=embed,
        view=InventoryBoardView(),
        ephemeral=False,
    )


# =========================================================
# DOWNTIME
#
# No /downtime spend command yet.
# Player hobbies will be added later.
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

async def import_character_sheet(
    *,
    guild_id: int,
    user_id: int,
    slot: str,
    file: discord.Attachment,
) -> tuple[bool, str]:
    if file.size > 12_000_000:
        return (
            False,
            "Please upload a file smaller than 12 MB.",
        )

    existing = await db().fetchrow(
        """
        SELECT *
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        guild_id,
        user_id,
        slot,
    )

    if not existing:
        return (
            False,
            (
                f"You do not have a {slot} character yet. "
                "Create that character first."
            ),
        )

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

        return (
            False,
            f"I couldn't read that sheet: {exc}",
        )

    if (
        not parsed.stats
        and not parsed.proficiencies
        and not parsed.name
        and not parsed.class_name
        and not parsed.subclass_name
    ):
        return (
            False,
            (
                "I could not recognize character details. "
                "Try the exported D&D Beyond PDF."
            ),
        )

    # D&D Beyond imports only sheet-derived information.
    # Server progression and economy values are preserved.
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
            or existing["character_name"]
        ),
        (
            parsed.class_name
            or existing["class_name"]
        ),
        (
            parsed.subclass_name
            or existing["subclass_name"]
        ),
        parsed.stats,
        parsed.proficiencies,
        guild_id,
        user_id,
        slot,
    )

    return (
        True,
        (
            "Sheet imported. "
            "Class, subclass, stats, and proficiencies updated. "
            "Server progression was preserved."
        ),
    )


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
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(
        ephemeral=False,
        thinking=True,
    )

    _, message = await import_character_sheet(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        slot=slot.value,
        file=file,
    )

    await interaction.followup.send(
        message,
        ephemeral=False,
    )


# =========================================================
# GENERIC STAFF NUMERIC COMMANDS
#
# Only MP and Gold use these.
# Downtime has its own dropdown commands.
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
        items[key] = remaining

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
# DOWNTIME JOBS
# =========================================================

async def run_job_for_character(
    *,
    guild_id: int,
    user_id: int,
    slot: str,
    job_name: str,
    hours: int,
) -> tuple[discord.Embed | None, str | None]:
    """
    Shared job engine used by both /job and the Job Board.

    Returns:
        (embed, None) on success
        (None, error_message) on failure
    """
    ability_name = JOB_ABILITIES[job_name]

    async with db().acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT *
                FROM characters
                WHERE guild_id=$1
                AND user_id=$2
                AND slot=$3
                FOR UPDATE
                """,
                guild_id,
                user_id,
                slot,
            )

            if not row:
                return (
                    None,
                    (
                        f"You do not have a {slot} character yet. "
                        "Use `/character create` first."
                    ),
                )

            current_downtime = int(row["downtime_hours"])

            if current_downtime < hours:
                return (
                    None,
                    (
                        f"You only have **{current_downtime}** downtime hours "
                        f"on your {slot} character, but this job needs "
                        f"**{hours}**."
                    ),
                )

            stats = dict(row["stats"])

            if ability_name not in stats:
                return (
                    None,
                    (
                        f"Your {slot} character does not have an imported "
                        f"**{ability_name}** score yet. Import the character sheet "
                        "before using this job."
                    ),
                )

            ability_score = int(stats[ability_name])
            modifier = ability_modifier(ability_score)

            server_mp = max(0, int(row["mp"]))
            server_level = level_for_mp(server_mp)
            rank = rank_for_level(server_level)
            gold_per_success = JOB_RANK_PAY[rank]

            roll_count = hours // 8
            roll_results = []
            total_gold = 0

            for _ in range(roll_count):
                natural_roll = secrets.randbelow(20) + 1
                total_roll = natural_roll + modifier
                natural_twenty = natural_roll == 20
                success = total_roll >= JOB_DC

                payout = 0
                if success:
                    payout = (
                        gold_per_success * 2
                        if natural_twenty
                        else gold_per_success
                    )

                total_gold += payout
                roll_results.append(
                    (
                        natural_roll,
                        total_roll,
                        success,
                        natural_twenty,
                        payout,
                    )
                )

            new_downtime = current_downtime - hours
            new_gold = int(row["gold"]) + total_gold

            await connection.execute(
                """
                UPDATE characters
                SET gold=$1,
                    downtime_hours=$2,
                    level=$3,
                    rank=$4,
                    updated_at=now()
                WHERE guild_id=$5
                AND user_id=$6
                AND slot=$7
                """,
                new_gold,
                new_downtime,
                server_level,
                rank,
                guild_id,
                user_id,
                slot,
            )

            character_name = str(row["character_name"])

    roll_lines = []

    for index, (
        natural_roll,
        total_roll,
        success,
        natural_twenty,
        payout,
    ) in enumerate(roll_results, start=1):
        if natural_twenty and success:
            result_text = (
                f"🌟 **NAT 20** → {total_roll} • "
                f"Success • **{payout} Gold**"
            )
        elif success:
            result_text = (
                f"✅ {natural_roll} {modifier:+d} = {total_roll} • "
                f"Success • **{payout} Gold**"
            )
        else:
            result_text = (
                f"❌ {natural_roll} {modifier:+d} = {total_roll} • "
                "Failed • **0 Gold**"
            )

        roll_lines.append(
            f"**Roll {index}:** {result_text}"
        )

    embed = discord.Embed(
        title=f"💼 {job_name} ({ability_name}) Work",
        description=(
            f"**{character_name}** worked as a "
            f"**{job_name} ({ability_name})**."
        ),
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="Job Check",
        value=(
            f"**Ability:** {ability_name} {ability_score} ({modifier:+d})\n"
            f"**DC:** {JOB_DC}\n"
            f"**Rank:** {rank}\n"
            f"**Pay per success:** {gold_per_success} Gold"
        ),
        inline=False,
    )

    embed.add_field(
        name=f"Work Rolls ({roll_count})",
        value="\n".join(roll_lines),
        inline=False,
    )

    embed.add_field(
        name="Results",
        value=(
            f"**Downtime spent:** {hours} hours\n"
            f"**Downtime remaining:** {new_downtime} / 40\n"
            f"**Gold earned:** {total_gold}\n"
            f"**New gold total:** {new_gold}"
        ),
        inline=False,
    )

    return embed, None


@bot.tree.command(
    name="job",
    description="Spend downtime working a job to earn gold",
)
@app_commands.choices(
    slot=SLOTS,
    job=JOB_CHOICES,
    hours=DOWNTIME_CHOICES,
)
async def job_command(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    job: app_commands.Choice[str],
    hours: app_commands.Choice[int],
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(
        ephemeral=False,
        thinking=True,
    )

    embed, error = await run_job_for_character(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        slot=slot.value,
        job_name=job.value,
        hours=hours.value,
    )

    if error:
        await interaction.followup.send(
            error,
            ephemeral=False,
        )
        return

    await interaction.followup.send(
        embed=embed,
        ephemeral=False,
    )


class JobSessionView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=300)

        self.user_id = user_id
        self.guild_id = guild_id
        self.selected_slot: str | None = None
        self.selected_job: str | None = None
        self.selected_hours: int | None = None
        self.processed = False

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=0,
        )

        self.job_select = discord.ui.Select(
            placeholder="Choose a job",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Soldier (Strength)",
                    value="Soldier",
                ),
                discord.SelectOption(
                    label="Thief (Dexterity)",
                    value="Thief",
                ),
                discord.SelectOption(
                    label="Farmer (Constitution)",
                    value="Farmer",
                ),
                discord.SelectOption(
                    label="Teacher (Intelligence)",
                    value="Teacher",
                ),
                discord.SelectOption(
                    label="Priest (Wisdom)",
                    value="Priest",
                ),
                discord.SelectOption(
                    label="Performer (Charisma)",
                    value="Performer",
                ),
            ],
            row=1,
        )

        self.hours_select = discord.ui.Select(
            placeholder="Choose downtime hours",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="8 hours", value="8"),
                discord.SelectOption(label="16 hours", value="16"),
                discord.SelectOption(label="24 hours", value="24"),
                discord.SelectOption(label="32 hours", value="32"),
                discord.SelectOption(label="40 hours", value="40"),
            ],
            row=2,
        )

        self.work_button = discord.ui.Button(
            label="Work Job",
            emoji="💼",
            style=discord.ButtonStyle.success,
            row=3,
        )

        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=3,
        )

        self.slot_select.callback = self.slot_changed
        self.job_select.callback = self.job_changed
        self.hours_select.callback = self.hours_changed
        self.work_button.callback = self.work_job
        self.cancel_button.callback = self.cancel

        self.add_item(self.slot_select)
        self.add_item(self.job_select)
        self.add_item(self.hours_select)
        self.add_item(self.work_button)
        self.add_item(self.cancel_button)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
        ):
            return True

        await interaction.response.send_message(
            "This private job session belongs to another player.",
            ephemeral=True,
        )
        return False

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = self.slot_select.values[0]
        await interaction.response.defer()

    async def job_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_job = self.job_select.values[0]
        await interaction.response.defer()

    async def hours_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_hours = int(
            self.hours_select.values[0]
        )
        await interaction.response.defer()

    async def work_job(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.processed:
            await interaction.response.send_message(
                "This job session has already been processed.",
                ephemeral=True,
            )
            return

        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        if self.selected_job is None:
            await interaction.response.send_message(
                "Choose a job first.",
                ephemeral=True,
            )
            return

        if self.selected_hours is None:
            await interaction.response.send_message(
                "Choose how many downtime hours to spend first.",
                ephemeral=True,
            )
            return

        self.processed = True

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        embed, error = await run_job_for_character(
            guild_id=self.guild_id,
            user_id=self.user_id,
            slot=self.selected_slot,
            job_name=self.selected_job,
            hours=self.selected_hours,
        )

        if error:
            self.processed = False
            await interaction.edit_original_response(
                content=error,
                embed=None,
                view=self,
            )
            return

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=None,
        )

    async def cancel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.processed = True
        await interaction.response.edit_message(
            content="Job session cancelled.",
            embed=None,
            view=None,
        )


class JobBoardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Work a Job",
        emoji="💼",
        style=discord.ButtonStyle.primary,
        custom_id="astrelius:jobboard:work",
    )
    async def work_a_job(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Use this inside the server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="💼 Work a Job",
            description=(
                "Choose your character slot, job, and how many "
                "downtime hours you want to spend."
            ),
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            view=JobSessionView(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
            ),
            ephemeral=True,
        )


@bot.tree.command(
    name="jobboard",
    description="Post the persistent Astrelius Job Board",
)
@app_commands.check(
    staff_check
)
async def jobboard_command(
    interaction: discord.Interaction,
):
    embed = discord.Embed(
        title="💼 Astrelius Job Board",
        description=(
            "Earn gold by spending downtime.\n\n"
            "Press **Work a Job** to open the job menu."
        ),
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="How it works",
        value=(
            "Choose your **Main or Alt**, select a job, "
            "then choose **8, 16, 24, 32, or 40** downtime hours."
        ),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        view=JobBoardView(),
        ephemeral=False,
    )


# =========================================================
# TRAINING
# =========================================================


def training_embed(
    *,
    character_name: str,
    slot: str,
    training_name: str,
    training_type: str,
    current_week: int,
    gold: int,
    downtime: int,
) -> discord.Embed:
    rules = TRAINING_RULES[training_type]
    cost = int(rules["cost"])
    total_weeks = int(rules["weeks"])
    next_week = current_week + 1

    embed = discord.Embed(
        title="📚 Confirm Training",
        description=(
            f"**{character_name}** ({slot}) is preparing to train "
            f"**{training_name}**."
        ),
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="Training",
        value=(
            f"**Type:** {training_type}\n"
            f"**Progress after this session:** Week {next_week} / {total_weeks}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Cost",
        value=(
            f"**Gold:** {cost}\n"
            f"**Downtime:** {TRAINING_DOWNTIME_COST} hours"
        ),
        inline=False,
    )

    embed.add_field(
        name="Current Resources",
        value=(
            f"**Gold:** {gold}\n"
            f"**Downtime:** {downtime} / 40"
        ),
        inline=False,
    )

    embed.set_footer(
        text="Nothing is spent until you confirm."
    )
    return embed


class TrainingConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
        slot: str,
        training_name: str,
        training_type: str,
        expected_week: int,
    ) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.slot = slot
        self.training_name = training_name
        self.training_type = training_type
        self.expected_week = expected_week
        self.processed = False

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.user_id:
            return True

        await interaction.response.send_message(
            "Only the player who started this training can use these buttons.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Confirm Training",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.processed:
            await interaction.response.send_message(
                "This training session has already been processed.",
                ephemeral=True,
            )
            return

        self.processed = True
        await interaction.response.defer(thinking=True)

        rules = TRAINING_RULES[self.training_type]
        cost = int(rules["cost"])
        total_weeks = int(rules["weeks"])

        async with db().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM characters
                    WHERE guild_id=$1
                    AND user_id=$2
                    AND slot=$3
                    FOR UPDATE
                    """,
                    self.guild_id,
                    self.user_id,
                    self.slot,
                )

                if not row:
                    await interaction.edit_original_response(
                        content=(
                            f"Your {self.slot} character no longer exists."
                        ),
                        embed=None,
                        view=None,
                    )
                    return

                active_name = row["training_name"]
                active_type = row["training_type"]
                current_week = int(row["training_week"] or 0)

                if active_type:
                    same_training_type = (
                        str(active_type) == self.training_type
                    )

                    if not same_training_type:
                        await interaction.edit_original_response(
                            content=(
                                "Your active training type changed before this was confirmed. "
                                "Run `/train` again."
                            ),
                            embed=None,
                            view=None,
                        )
                        return
                else:
                    current_week = 0

                if current_week != self.expected_week:
                    await interaction.edit_original_response(
                        content=(
                            "Your training progress changed before this was confirmed. "
                            "Run `/train` again."
                        ),
                        embed=None,
                        view=None,
                    )
                    return

                current_gold = int(row["gold"])
                reserved_gold = await reserved_auction_gold(
                    connection,
                    guild_id=self.guild_id,
                    user_id=self.user_id,
                    slot=self.slot,
                )
                available_gold = max(
                    0,
                    current_gold - reserved_gold,
                )
                current_downtime = int(row["downtime_hours"])

                if current_downtime < TRAINING_DOWNTIME_COST:
                    await interaction.edit_original_response(
                        content=(
                            "You need **40 downtime hours** to train for a week. "
                            f"You currently have **{current_downtime}**."
                        ),
                        embed=None,
                        view=None,
                    )
                    return

                if available_gold < cost:
                    await interaction.edit_original_response(
                        content=(
                            f"You need **{cost} Gold** for this training week. "
                            f"You currently have **{available_gold} Gold available** "
                            "after active auction bids."
                        ),
                        embed=None,
                        view=None,
                    )
                    return

                next_week = current_week + 1
                completed = next_week >= total_weeks
                new_gold = current_gold - cost
                new_downtime = current_downtime - TRAINING_DOWNTIME_COST

                if completed:
                    await connection.execute(
                        """
                        UPDATE characters
                        SET gold=$1,
                            downtime_hours=$2,
                            training_name=NULL,
                            training_type=NULL,
                            training_week=0,
                            updated_at=now()
                        WHERE guild_id=$3
                        AND user_id=$4
                        AND slot=$5
                        """,
                        new_gold,
                        new_downtime,
                        self.guild_id,
                        self.user_id,
                        self.slot,
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE characters
                        SET gold=$1,
                            downtime_hours=$2,
                            training_name=$3,
                            training_type=$4,
                            training_week=$5,
                            updated_at=now()
                        WHERE guild_id=$6
                        AND user_id=$7
                        AND slot=$8
                        """,
                        new_gold,
                        new_downtime,
                        self.training_name,
                        self.training_type,
                        next_week,
                        self.guild_id,
                        self.user_id,
                        self.slot,
                    )

                character_name = str(row["character_name"])

        result = discord.Embed(
            title=(
                "🎓 Training Complete!"
                if completed
                else "📚 Training Week Complete"
            ),
            description=(
                f"**{character_name}** trained **{self.training_name}**."
            ),
            color=discord.Color.gold(),
        )

        result.add_field(
            name="Progress",
            value=(
                f"**Type:** {self.training_type}\n"
                f"**Week:** {next_week} / {total_weeks}"
            ),
            inline=False,
        )

        result.add_field(
            name="Spent",
            value=(
                f"**Gold:** {cost}\n"
                f"**Downtime:** {TRAINING_DOWNTIME_COST} hours"
            ),
            inline=False,
        )

        result.add_field(
            name="Remaining",
            value=(
                f"**Gold:** {new_gold}\n"
                f"**Downtime:** {new_downtime} / 40"
            ),
            inline=False,
        )

        if completed:
            result.add_field(
                name="Finished",
                value=(
                    "The bot has cleared the active training record. "
                    "Add the completed language, proficiency, or feat to your "
                    "D&D Beyond sheet yourself."
                ),
                inline=False,
            )

        await interaction.edit_original_response(
            content=None,
            embed=result,
            view=None,
        )

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.secondary,
    )
    async def deny(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.processed:
            await interaction.response.send_message(
                "This training session has already been processed.",
                ephemeral=True,
            )
            return

        self.processed = True
        await interaction.response.edit_message(
            content="Training was not started. Nothing was spent.",
            embed=None,
            view=None,
        )


class CancelTrainingConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
        slot: str,
        training_name: str,
        training_type: str,
        training_week: int,
    ) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.slot = slot
        self.training_name = training_name
        self.training_type = training_type
        self.training_week = training_week
        self.processed = False

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.user_id:
            return True

        await interaction.response.send_message(
            "Only the player who owns this training can use these buttons.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Confirm Cancel",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.processed:
            await interaction.response.send_message(
                "This cancellation has already been processed.",
                ephemeral=True,
            )
            return

        self.processed = True
        await interaction.response.defer(thinking=True)

        async with db().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT training_name, training_type, training_week
                    FROM characters
                    WHERE guild_id=$1
                    AND user_id=$2
                    AND slot=$3
                    FOR UPDATE
                    """,
                    self.guild_id,
                    self.user_id,
                    self.slot,
                )

                if not row or not row["training_name"]:
                    await interaction.edit_original_response(
                        content="There is no active training to cancel.",
                        embed=None,
                        view=None,
                    )
                    return

                if (
                    str(row["training_name"]).casefold()
                    != self.training_name.casefold()
                    or str(row["training_type"]) != self.training_type
                    or int(row["training_week"] or 0) != self.training_week
                ):
                    await interaction.edit_original_response(
                        content=(
                            "Your training changed before this cancellation was confirmed. "
                            "Run `/canceltraining` again."
                        ),
                        embed=None,
                        view=None,
                    )
                    return

                await connection.execute(
                    """
                    UPDATE characters
                    SET training_name=NULL,
                        training_type=NULL,
                        training_week=0,
                        updated_at=now()
                    WHERE guild_id=$1
                    AND user_id=$2
                    AND slot=$3
                    """,
                    self.guild_id,
                    self.user_id,
                    self.slot,
                )

        await interaction.edit_original_response(
            content=(
                f"Training for **{self.training_name}** was cancelled. "
                "Previous gold and downtime were not refunded."
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(
        label="Keep Training",
        style=discord.ButtonStyle.secondary,
    )
    async def keep_training(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.processed:
            await interaction.response.send_message(
                "This cancellation has already been processed.",
                ephemeral=True,
            )
            return

        self.processed = True
        await interaction.response.edit_message(
            content="Training kept. No progress was changed.",
            embed=None,
            view=None,
        )


async def send_training_prompt(
    interaction: discord.Interaction,
    *,
    slot: str,
    training_name: str,
    training_type: str,
    ephemeral: bool,
) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=ephemeral,
        )
        return

    rules = TRAINING_RULES[training_type]
    cost = int(rules["cost"])
    total_weeks = int(rules["weeks"])

    row = await db().fetchrow(
        """
        SELECT *
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        interaction.guild_id,
        interaction.user.id,
        slot,
    )

    if not row:
        await interaction.response.send_message(
            (
                f"You do not have a {slot} character yet. "
                "Use `/character create` first."
            ),
            ephemeral=ephemeral,
        )
        return

    active_type = row["training_type"]
    current_week = int(row["training_week"] or 0)

    if active_type:
        same_training_type = (
            str(active_type) == training_type
        )

        if not same_training_type:
            await interaction.response.send_message(
                (
                    f"Your {slot} character is already training "
                    f"**{active_type}** at Week {current_week}.\n"
                    "Cancel the current training before switching types."
                ),
                ephemeral=ephemeral,
            )
            return
    else:
        current_week = 0

    if current_week >= total_weeks:
        await interaction.response.send_message(
            "That training is already complete.",
            ephemeral=ephemeral,
        )
        return

    current_gold = int(row["gold"])
    reserved_gold = await reserved_auction_gold(
        db(),
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        slot=slot,
    )
    available_gold = max(
        0,
        current_gold - reserved_gold,
    )
    current_downtime = int(row["downtime_hours"])

    if current_downtime < TRAINING_DOWNTIME_COST:
        await interaction.response.send_message(
            (
                "You need **40 downtime hours** to train for one week. "
                f"You currently have **{current_downtime}**."
            ),
            ephemeral=ephemeral,
        )
        return

    if available_gold < cost:
        await interaction.response.send_message(
            (
                f"You need **{cost} Gold** for this training week. "
                f"You currently have **{available_gold} Gold available** "
                "after active auction bids."
            ),
            ephemeral=ephemeral,
        )
        return

    await interaction.response.send_message(
        embed=training_embed(
            character_name=str(row["character_name"]),
            slot=slot,
            training_name=training_name,
            training_type=training_type,
            current_week=current_week,
            gold=current_gold,
            downtime=current_downtime,
        ),
        view=TrainingConfirmView(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            slot=slot,
            training_name=training_name,
            training_type=training_type,
            expected_week=current_week,
        ),
        ephemeral=ephemeral,
    )


@bot.tree.command(
    name="train",
    description="Spend 40 downtime hours to advance your current training",
)
@app_commands.choices(
    slot=SLOTS,
    training_type=TRAINING_TYPE_CHOICES,
)
async def train_command(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    training: app_commands.Range[str, 1, 100],
    training_type: app_commands.Choice[str],
):
    await send_training_prompt(
        interaction,
        slot=slot.value,
        training_name=training.strip(),
        training_type=training_type.value,
        ephemeral=False,
    )


@bot.tree.command(
    name="canceltraining",
    description="Erase your current training progress so you can train something else",
)
@app_commands.choices(
    slot=SLOTS,
)
async def cancel_training_command(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=False,
        )
        return

    row = await db().fetchrow(
        """
        SELECT character_name, training_name, training_type, training_week
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        interaction.guild_id,
        interaction.user.id,
        slot.value,
    )

    if not row:
        await interaction.response.send_message(
            (
                f"You do not have a {slot.value} character yet. "
                "Use `/character create` first."
            ),
            ephemeral=False,
        )
        return

    if not row["training_name"]:
        await interaction.response.send_message(
            f"Your {slot.value} character does not have active training.",
            ephemeral=False,
        )
        return

    training_name = str(row["training_name"])
    training_type = str(row["training_type"])
    training_week = int(row["training_week"] or 0)
    total_weeks = int(TRAINING_RULES[training_type]["weeks"])

    embed = discord.Embed(
        title="🗑️ Cancel Current Training?",
        description=(
            f"**{row['character_name']}** is currently training "
            f"**{training_name}**."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Current Progress",
        value=(
            f"**Type:** {training_type}\n"
            f"**Week:** {training_week} / {total_weeks}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Warning",
        value=(
            "All current training progress will be erased. "
            "Previously spent gold and downtime will not be refunded."
        ),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        view=CancelTrainingConfirmView(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            slot=slot.value,
            training_name=training_name,
            training_type=training_type,
            training_week=training_week,
        ),
        ephemeral=False,
    )


# =========================================================
# TRAINING BOARD
# =========================================================

class TrainingSubjectModal(
    discord.ui.Modal,
    title="Training",
):
    training_name = discord.ui.TextInput(
        label="What are you training?",
        placeholder="Example: Elvish or War Caster",
        min_length=1,
        max_length=100,
    )

    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
        slot: str,
        training_type: str,
    ) -> None:
        super().__init__()

        self.user_id = user_id
        self.guild_id = guild_id
        self.slot = slot
        self.training_type = training_type

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if (
            interaction.user.id != self.user_id
            or interaction.guild_id != self.guild_id
        ):
            await interaction.response.send_message(
                "This training menu belongs to another player.",
                ephemeral=True,
            )
            return

        await send_training_prompt(
            interaction,
            slot=self.slot,
            training_name=str(self.training_name).strip(),
            training_type=self.training_type,
            ephemeral=True,
        )


class TrainingStartView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=300)

        self.user_id = user_id
        self.guild_id = guild_id
        self.selected_slot: str | None = None
        self.selected_type: str | None = None

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=0,
        )

        self.type_select = discord.ui.Select(
            placeholder="Choose training type",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Language / Proficiency",
                    description="50 Gold • 10 weeks",
                    value="Language / Proficiency",
                ),
                discord.SelectOption(
                    label="Feat",
                    description="200 Gold • 15 weeks",
                    value="Feat",
                ),
            ],
            row=1,
        )

        self.continue_button = discord.ui.Button(
            label="Continue",
            emoji="📚",
            style=discord.ButtonStyle.success,
            row=2,
        )

        self.slot_select.callback = self.slot_changed
        self.type_select.callback = self.type_changed
        self.continue_button.callback = self.continue_training

        self.add_item(self.slot_select)
        self.add_item(self.type_select)
        self.add_item(self.continue_button)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
        ):
            return True

        await interaction.response.send_message(
            "This training menu belongs to another player.",
            ephemeral=True,
        )
        return False

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = self.slot_select.values[0]
        await interaction.response.defer()

    async def type_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_type = self.type_select.values[0]
        await interaction.response.defer()

    async def continue_training(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        if self.selected_type is None:
            await interaction.response.send_message(
                "Choose a training type first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            TrainingSubjectModal(
                user_id=self.user_id,
                guild_id=self.guild_id,
                slot=self.selected_slot,
                training_type=self.selected_type,
            )
        )


class CancelTrainingSlotView(discord.ui.View):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=300)

        self.user_id = user_id
        self.guild_id = guild_id
        self.selected_slot: str | None = None

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=0,
        )

        self.continue_button = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.danger,
            row=1,
        )

        self.slot_select.callback = self.slot_changed
        self.continue_button.callback = self.continue_cancel

        self.add_item(self.slot_select)
        self.add_item(self.continue_button)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
        ):
            return True

        await interaction.response.send_message(
            "This training menu belongs to another player.",
            ephemeral=True,
        )
        return False

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = self.slot_select.values[0]
        await interaction.response.defer()

    async def continue_cancel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        row = await db().fetchrow(
            """
            SELECT character_name, training_name, training_type, training_week
            FROM characters
            WHERE guild_id=$1
            AND user_id=$2
            AND slot=$3
            """,
            self.guild_id,
            self.user_id,
            self.selected_slot,
        )

        if not row:
            await interaction.response.edit_message(
                content=(
                    f"You do not have a {self.selected_slot} character yet."
                ),
                embed=None,
                view=None,
            )
            return

        if not row["training_name"]:
            await interaction.response.edit_message(
                content=(
                    f"Your {self.selected_slot} character "
                    "does not have active training."
                ),
                embed=None,
                view=None,
            )
            return

        training_name = str(row["training_name"])
        training_type = str(row["training_type"])
        training_week = int(row["training_week"] or 0)
        total_weeks = int(
            TRAINING_RULES[training_type]["weeks"]
        )

        embed = discord.Embed(
            title="🗑️ Cancel Current Training?",
            description=(
                f"**{row['character_name']}** is currently training "
                f"**{training_name}**."
            ),
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Current Progress",
            value=(
                f"**Type:** {training_type}\n"
                f"**Week:** {training_week} / {total_weeks}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Warning",
            value=(
                "Current training progress will be erased. "
                "Spent gold and downtime are not refunded."
            ),
            inline=False,
        )

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=CancelTrainingConfirmView(
                user_id=self.user_id,
                guild_id=self.guild_id,
                slot=self.selected_slot,
                training_name=training_name,
                training_type=training_type,
                training_week=training_week,
            ),
        )


class TrainingBoardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Train",
        emoji="📚",
        style=discord.ButtonStyle.primary,
        custom_id="astrelius:trainingboard:train",
    )
    async def train(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Use this inside the server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📚 Training",
            description="Choose a character and training type.",
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            view=TrainingStartView(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Cancel Training",
        emoji="🗑️",
        style=discord.ButtonStyle.secondary,
        custom_id="astrelius:trainingboard:cancel",
    )
    async def cancel_training(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Use this inside the server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🗑️ Cancel Training",
            description="Choose the character.",
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            view=CancelTrainingSlotView(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
            ),
            ephemeral=True,
        )


@bot.tree.command(
    name="trainingboard",
    description="Post the Astrelius Training panel",
)
@app_commands.check(
    staff_check
)
async def trainingboard_command(
    interaction: discord.Interaction,
):
    embed = discord.Embed(
        title="📚 Astrelius Training",
        description="Train languages, proficiencies, or replace a feat.",
        color=discord.Color.gold(),
    )

    await interaction.response.send_message(
        embed=embed,
        view=TrainingBoardView(),
        ephemeral=False,
    )


# =========================================================
# QUEST REWARDS
# =========================================================

@dataclass
class QuestRewardRecipient:
    user_id: int
    display_name: str
    slot: str


class QuestRewardSession:
    def __init__(
        self,
        guild: discord.Guild,
        staff_user_id: int,
        mp_reward: int,
        gold_reward: int,
        downtime_reward: int,
    ) -> None:
        self.guild = guild
        self.staff_user_id = staff_user_id
        self.mp_reward = mp_reward
        self.gold_reward = gold_reward
        self.downtime_reward = downtime_reward
        self.recipients: list[
            QuestRewardRecipient
        ] = []
        self.panel_message = None
        self.finished = False

    def can_use(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        return (
            interaction.user.id
            == self.staff_user_id
            and interaction.guild_id
            == self.guild.id
        )

    def summary_embed(
        self,
        confirmation: bool = False,
    ) -> discord.Embed:
        title = (
            "Confirm Quest Rewards"
            if confirmation
            else "Quest Rewards"
        )

        embed = discord.Embed(
            title=title,
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Rewards",
            value=(
                f"**MP:** +{self.mp_reward}\n"
                f"**Gold:** +{self.gold_reward}\n"
                f"**Downtime:** Set to {self.downtime_reward} / 40"
            ),
            inline=False,
        )

        if self.recipients:
            lines = [
                (
                    f"{index}. "
                    f"{recipient.display_name} "
                    f"— {recipient.slot}"
                )
                for index, recipient
                in enumerate(
                    self.recipients,
                    start=1,
                )
            ]

            recipient_text = "\n".join(
                lines
            )

            if len(recipient_text) > 1000:
                recipient_text = (
                    recipient_text[:995]
                    + "..."
                )

        else:
            recipient_text = (
                "No players added yet."
            )

        embed.add_field(
            name=(
                f"Recipients "
                f"({len(self.recipients)})"
            ),
            value=recipient_text,
            inline=False,
        )

        if confirmation:
            embed.set_footer(
                text=(
                    "Confirm to apply these rewards "
                    "to every selected character."
                )
            )

        else:
            embed.set_footer(
                text=(
                    "Players were selected in the slash command. "
                    "Each player keeps their chosen Main or Alt."
                )
            )

        return embed

    async def refresh_panel(
        self,
    ) -> None:
        if (
            self.panel_message is None
            or self.finished
        ):
            return

        try:
            await self.panel_message.edit(
                embed=self.summary_embed(),
                view=QuestRewardPanelView(
                    self
                ),
            )

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            pass

    async def add_recipient(
        self,
        member: discord.Member,
        slot: str,
    ) -> tuple[bool, str]:
        if self.finished:
            return (
                False,
                "This reward session is already finished.",
            )

        for recipient in self.recipients:
            if (
                recipient.user_id
                == member.id
                and recipient.slot
                == slot
            ):
                return (
                    False,
                    (
                        f"{member.display_name} "
                        f"({slot}) is already added."
                    ),
                )

        self.recipients.append(
            QuestRewardRecipient(
                user_id=member.id,
                display_name=(
                    member.display_name
                ),
                slot=slot,
            )
        )

        await self.refresh_panel()

        return (
            True,
            (
                f"Added **{member.display_name}** "
                f"({slot})."
            ),
        )

    async def remove_recipient(
        self,
        number: int,
    ) -> tuple[bool, str]:
        if self.finished:
            return (
                False,
                "This reward session is already finished.",
            )

        if not (
            1
            <= number
            <= len(
                self.recipients
            )
        ):
            return (
                False,
                (
                    "That recipient number does not exist. "
                    "Use the number shown on the reward panel."
                ),
            )

        removed = self.recipients.pop(
            number - 1
        )

        await self.refresh_panel()

        return (
            True,
            (
                f"Removed **{removed.display_name}** "
                f"({removed.slot})."
            ),
        )

    async def apply_rewards(
        self,
    ) -> tuple[
        list[str],
        list[str],
        list[str],
    ]:
        applied: list[str] = []
        skipped: list[str] = []
        role_notes: list[str] = []

        if self.finished:
            return (
                applied,
                ["This reward session is already finished."],
                role_notes,
            )

        self.finished = True

        for recipient in list(
            self.recipients
        ):
            member = self.guild.get_member(
                recipient.user_id
            )

            if member is None:
                skipped.append(
                    (
                        f"{recipient.display_name} "
                        f"({recipient.slot}) "
                        "is no longer in the server."
                    )
                )
                continue

            async with (
                db().acquire()
            ) as connection:
                async with (
                    connection.transaction()
                ):
                    row = (
                        await connection.fetchrow(
                            """
                            SELECT *
                            FROM characters
                            WHERE guild_id=$1
                            AND user_id=$2
                            AND slot=$3
                            FOR UPDATE
                            """,
                            self.guild.id,
                            recipient.user_id,
                            recipient.slot,
                        )
                    )

                    if not row:
                        skipped.append(
                            (
                                f"{member.display_name} "
                                f"({recipient.slot}) "
                                "does not have that character slot."
                            )
                        )
                        continue

                    current_mp = max(
                        0,
                        int(
                            row["mp"]
                        ),
                    )

                    current_gold = max(
                        0,
                        int(
                            row["gold"]
                        ),
                    )

                    new_mp = (
                        current_mp
                        + self.mp_reward
                    )

                    new_level = (
                        level_for_mp(
                            new_mp
                        )
                    )

                    new_rank = (
                        rank_for_level(
                            new_level
                        )
                    )

                    new_gold = (
                        current_gold
                        + self.gold_reward
                    )

                    await connection.execute(
                        """
                        UPDATE characters
                        SET mp=$1,
                            level=$2,
                            rank=$3,
                            gold=$4,
                            downtime_hours=$5,
                            updated_at=now()
                        WHERE guild_id=$6
                        AND user_id=$7
                        AND slot=$8
                        """,
                        new_mp,
                        new_level,
                        new_rank,
                        new_gold,
                        self.downtime_reward,
                        self.guild.id,
                        recipient.user_id,
                        recipient.slot,
                    )

            role_note = (
                await sync_rank_role(
                    member
                )
                or ""
            )

            if role_note:
                role_notes.append(
                    (
                        f"{member.display_name}: "
                        f"{role_note}"
                    )
                )

            applied.append(
                (
                    f"{member.display_name} "
                    f"({recipient.slot})"
                )
            )

        return (
            applied,
            skipped,
            role_notes,
        )


class QuestRewardRecipientPicker(
    discord.ui.View,
):
    def __init__(
        self,
        session: QuestRewardSession,
    ) -> None:
        super().__init__(
            timeout=300
        )

        self.session = session
        self.selected_member: (
            discord.Member | None
        ) = None
        self.selected_slot: (
            str | None
        ) = None

        self.member_select = (
            discord.ui.UserSelect(
                placeholder="Choose a player",
                min_values=1,
                max_values=1,
                row=0,
            )
        )

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=1,
        )

        self.add_button = discord.ui.Button(
            label="Add Player",
            emoji="➕",
            style=discord.ButtonStyle.success,
            row=2,
        )

        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=2,
        )

        self.member_select.callback = (
            self.member_changed
        )

        self.slot_select.callback = (
            self.slot_changed
        )

        self.add_button.callback = (
            self.add_selected
        )

        self.cancel_button.callback = (
            self.cancel
        )

        self.add_item(
            self.member_select
        )

        self.add_item(
            self.slot_select
        )

        self.add_item(
            self.add_button
        )

        self.add_item(
            self.cancel_button
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if self.session.can_use(
            interaction
        ):
            return True

        await interaction.response.send_message(
            "Only the staff member who opened this reward panel can use it.",
            ephemeral=True,
        )

        return False

    async def member_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        selected = (
            self.member_select.values[0]
        )

        if isinstance(
            selected,
            discord.Member,
        ):
            self.selected_member = (
                selected
            )

        else:
            member = (
                interaction.guild.get_member(
                    selected.id
                )
                if interaction.guild
                else None
            )

            self.selected_member = (
                member
            )

        await interaction.response.defer()

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = (
            self.slot_select.values[0]
        )

        await interaction.response.defer()

    async def add_selected(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.selected_member is None:
            await interaction.response.send_message(
                "Choose a player first.",
                ephemeral=True,
            )
            return

        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        success, message = (
            await self.session.add_recipient(
                self.selected_member,
                self.selected_slot,
            )
        )

        if success:
            await interaction.response.edit_message(
                content=message,
                view=None,
            )

        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )

    async def cancel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.edit_message(
            content="Player selection cancelled.",
            view=None,
        )


class QuestRewardRemoveModal(
    discord.ui.Modal,
    title="Remove Reward Recipient",
):
    recipient_number = discord.ui.TextInput(
        label="Recipient number",
        placeholder="Example: 2",
        min_length=1,
        max_length=8,
    )

    def __init__(
        self,
        session: QuestRewardSession,
    ) -> None:
        super().__init__()

        self.session = session

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not self.session.can_use(
            interaction
        ):
            await interaction.response.send_message(
                "Only the staff member who opened this reward panel can use it.",
                ephemeral=True,
            )
            return

        try:
            number = int(
                str(
                    self.recipient_number
                ).strip()
            )

        except ValueError:
            await interaction.response.send_message(
                "Enter a whole recipient number.",
                ephemeral=True,
            )
            return

        success, message = (
            await self.session.remove_recipient(
                number
            )
        )

        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


class QuestRewardConfirmView(
    discord.ui.View,
):
    def __init__(
        self,
        session: QuestRewardSession,
    ) -> None:
        super().__init__(
            timeout=300
        )

        self.session = session

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if self.session.can_use(
            interaction
        ):
            return True

        await interaction.response.send_message(
            "Only the staff member who opened this reward panel can use it.",
            ephemeral=True,
        )

        return False

    @discord.ui.button(
        label="Confirm",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.session.finished:
            await interaction.response.send_message(
                "These rewards have already been processed.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        applied, skipped, role_notes = (
            await self.session.apply_rewards()
        )

        if (
            self.session.panel_message
            is not None
        ):
            try:
                finished_embed = (
                    self.session.summary_embed()
                )

                finished_embed.title = (
                    "Quest Rewards Complete"
                )

                finished_embed.set_footer(
                    text=(
                        "This reward batch has been processed."
                    )
                )

                await (
                    self.session
                    .panel_message
                    .edit(
                        embed=finished_embed,
                        view=None,
                    )
                )

            except (
                discord.NotFound,
                discord.HTTPException,
            ):
                pass

        lines = []

        if applied:
            lines.append(
                (
                    f"**Rewarded ({len(applied)}):**\n"
                    + "\n".join(
                        f"• {item}"
                        for item in applied
                    )
                )
            )

        if skipped:
            lines.append(
                (
                    f"**Skipped ({len(skipped)}):**\n"
                    + "\n".join(
                        f"• {item}"
                        for item in skipped
                    )
                )
            )

        if role_notes:
            lines.append(
                (
                    "**Rank role notes:**\n"
                    + "\n".join(
                        f"• {item}"
                        for item in role_notes
                    )
                )
            )

        if not lines:
            lines.append(
                "No rewards were applied."
            )

        summary = "\n\n".join(
            lines
        )

        if len(summary) > 1900:
            summary = (
                summary[:1895]
                + "..."
            )

        await interaction.followup.send(
            summary,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Reward confirmation cancelled.",
            embed=None,
            view=None,
        )


class QuestRewardPanelView(
    discord.ui.View,
):
    def __init__(
        self,
        session: QuestRewardSession,
    ) -> None:
        super().__init__(
            timeout=900
        )

        self.session = session

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if self.session.can_use(
            interaction
        ):
            return True

        await interaction.response.send_message(
            "Only the staff member who opened this reward panel can use it.",
            ephemeral=True,
        )

        return False

    @discord.ui.button(
        label="Add Player",
        emoji="➕",
        style=discord.ButtonStyle.success,
    )
    async def add_player(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.session.finished:
            await interaction.response.send_message(
                "This reward batch is already finished.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose a player and whether to reward their Main or Alt character.",
            view=QuestRewardRecipientPicker(
                self.session
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Remove Player",
        emoji="➖",
        style=discord.ButtonStyle.secondary,
    )
    async def remove_player(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not self.session.recipients:
            await interaction.response.send_message(
                "There are no recipients to remove.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            QuestRewardRemoveModal(
                self.session
            )
        )

    @discord.ui.button(
        label="Give Rewards",
        emoji="🎁",
        style=discord.ButtonStyle.primary,
    )
    async def give_rewards(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not self.session.recipients:
            await interaction.response.send_message(
                "Add at least one player before giving rewards.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=(
                self.session
                .summary_embed(
                    confirmation=True
                )
            ),
            view=QuestRewardConfirmView(
                self.session
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.session.finished = True

        await interaction.response.edit_message(
            content="Quest reward batch cancelled.",
            embed=None,
            view=None,
        )


@bot.tree.command(
    name="questreward",
    description="Give the same quest rewards to multiple characters",
)
@app_commands.choices(
    slot1=SLOTS,
    slot2=SLOTS,
    slot3=SLOTS,
    slot4=SLOTS,
    slot5=SLOTS,
    slot6=SLOTS,
    slot7=SLOTS,
    slot8=SLOTS,
    slot9=SLOTS,
    slot10=SLOTS,
    downtime=DOWNTIME_CHOICES,
    mp=QUEST_REWARD_MP_CHOICES,
)
@app_commands.check(
    staff_check
)
async def questreward(
    interaction: discord.Interaction,
    player1: discord.Member,
    slot1: app_commands.Choice[str],
    downtime: app_commands.Choice[int],
    mp: app_commands.Choice[int],
    gold: app_commands.Range[
        int,
        0,
        1_000_000,
    ],
    player2: discord.Member | None = None,
    slot2: app_commands.Choice[str] | None = None,
    player3: discord.Member | None = None,
    slot3: app_commands.Choice[str] | None = None,
    player4: discord.Member | None = None,
    slot4: app_commands.Choice[str] | None = None,
    player5: discord.Member | None = None,
    slot5: app_commands.Choice[str] | None = None,
    player6: discord.Member | None = None,
    slot6: app_commands.Choice[str] | None = None,
    player7: discord.Member | None = None,
    slot7: app_commands.Choice[str] | None = None,
    player8: discord.Member | None = None,
    slot8: app_commands.Choice[str] | None = None,
    player9: discord.Member | None = None,
    slot9: app_commands.Choice[str] | None = None,
    player10: discord.Member | None = None,
    slot10: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=True,
        )
        return

    pairs = [
        (player1, slot1),
        (player2, slot2),
        (player3, slot3),
        (player4, slot4),
        (player5, slot5),
        (player6, slot6),
        (player7, slot7),
        (player8, slot8),
        (player9, slot9),
        (player10, slot10),
    ]

    # If an optional player is chosen, require their Main/Alt slot too.
    missing_slots = [
        index
        for index, (member, slot)
        in enumerate(
            pairs,
            start=1,
        )
        if (
            member is not None
            and slot is None
        )
    ]

    if missing_slots:
        numbers = ", ".join(
            str(number)
            for number in missing_slots
        )

        await interaction.response.send_message(
            (
                "Choose Main or Alt for "
                f"player slot(s): {numbers}."
            ),
            ephemeral=True,
        )
        return

    # Ignore empty optional positions.
    selected_pairs = [
        (member, slot)
        for member, slot in pairs
        if (
            member is not None
            and slot is not None
        )
    ]

    # Stop accidental duplicate copies of the same exact character.
    seen: set[
        tuple[int, str]
    ] = set()

    duplicates: list[str] = []

    for member, slot in selected_pairs:
        key = (
            member.id,
            slot.value,
        )

        if key in seen:
            duplicates.append(
                (
                    f"{member.display_name} "
                    f"({slot.value})"
                )
            )

        seen.add(
            key
        )

    if duplicates:
        await interaction.response.send_message(
            (
                "The same character was selected more than once:\n"
                + "\n".join(
                    f"• {item}"
                    for item in duplicates
                )
            ),
            ephemeral=True,
        )
        return

    session = QuestRewardSession(
        guild=interaction.guild,
        staff_user_id=(
            interaction.user.id
        ),
        mp_reward=mp.value,
        gold_reward=gold,
        downtime_reward=(
            downtime.value
        ),
    )

    for member, slot in selected_pairs:
        session.recipients.append(
            QuestRewardRecipient(
                user_id=member.id,
                display_name=(
                    member.display_name
                ),
                slot=slot.value,
            )
        )

    await interaction.response.send_message(
        embed=session.summary_embed(
            confirmation=True
        ),
        view=QuestRewardConfirmView(
            session
        ),
        ephemeral=True,
    )



# =========================================================
# AUCTION HOUSE
# =========================================================

AUCTION_DURATION = timedelta(days=3)


async def reserved_auction_gold(
    connection,
    *,
    guild_id: int,
    user_id: int,
    slot: str,
    exclude_auction_id: int | None = None,
) -> int:
    if exclude_auction_id is None:
        value = await connection.fetchval(
            """
            SELECT COALESCE(SUM(b.amount), 0)
            FROM auction_bids b
            JOIN auctions a
              ON a.auction_id = b.auction_id
            WHERE a.status='active'
            AND b.guild_id=$1
            AND b.bidder_user_id=$2
            AND b.bidder_slot=$3
            """,
            guild_id,
            user_id,
            slot,
        )
    else:
        value = await connection.fetchval(
            """
            SELECT COALESCE(SUM(b.amount), 0)
            FROM auction_bids b
            JOIN auctions a
              ON a.auction_id = b.auction_id
            WHERE a.status='active'
            AND b.guild_id=$1
            AND b.bidder_user_id=$2
            AND b.bidder_slot=$3
            AND b.auction_id <> $4
            """,
            guild_id,
            user_id,
            slot,
            exclude_auction_id,
        )

    return int(value or 0)


async def available_gold_for_character(
    connection,
    *,
    guild_id: int,
    user_id: int,
    slot: str,
    exclude_auction_id: int | None = None,
    lock: bool = False,
):
    query = """
        SELECT *
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
    """

    if lock:
        query += " FOR UPDATE"

    row = await connection.fetchrow(
        query,
        guild_id,
        user_id,
        slot,
    )

    if not row:
        return None, 0, 0

    reserved = await reserved_auction_gold(
        connection,
        guild_id=guild_id,
        user_id=user_id,
        slot=slot,
        exclude_auction_id=exclude_auction_id,
    )

    total_gold = max(0, int(row["gold"]))
    available = max(0, total_gold - reserved)

    return row, reserved, available


def auction_embed(
    auction,
) -> discord.Embed:
    status = str(auction["status"])
    active = status == "active"

    embed = discord.Embed(
        title=f"🔨 Auction #{auction['auction_id']}: {auction['item_name']}",
        description=str(auction["item_description"]),
        color=(
            discord.Color.gold()
            if active
            else discord.Color.dark_grey()
        ),
    )

    embed.add_field(
        name="Starting Price",
        value=f"{int(auction['starting_price'])} Gold",
        inline=True,
    )

    if auction["buy_now_price"] is not None:
        embed.add_field(
            name="Buy Now",
            value=f"{int(auction['buy_now_price'])} Gold",
            inline=True,
        )

    if active:
        ends_at = auction["ends_at"]
        timestamp = int(ends_at.timestamp())

        embed.add_field(
            name="Ends",
            value=(
                f"<t:{timestamp}:F>\n"
                f"<t:{timestamp}:R>"
            ),
            inline=False,
        )

        embed.set_footer(
            text="Bids are silent. Bidder names and amounts stay hidden until the auction ends."
        )

    else:
        status_text = {
            "completed": "Ended",
            "ended_no_bids": "Ended • No bids",
            "ended_no_valid_bid": "Ended • No valid winner",
            "cancelled": "Cancelled",
        }.get(status, "Ended")

        embed.add_field(
            name="Status",
            value=status_text,
            inline=False,
        )

        if auction["winning_amount"] is not None:
            embed.add_field(
                name="Winning Price",
                value=f"{int(auction['winning_amount'])} Gold",
                inline=True,
            )

    return embed


async def get_auction_thread(
    auction,
):
    thread_id = auction["thread_id"]

    if thread_id is None:
        return None

    thread = bot.get_channel(int(thread_id))

    if thread is not None:
        return thread

    try:
        return await bot.fetch_channel(
            int(thread_id)
        )
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
    ):
        return None


async def post_auction_thread_message(
    auction,
    content: str,
) -> None:
    thread = await get_auction_thread(
        auction
    )

    if thread is None:
        return

    try:
        await thread.send(
            content
        )
    except (
        discord.Forbidden,
        discord.HTTPException,
    ):
        pass


async def refresh_auction_message(
    auction_id: int,
) -> None:
    auction = await db().fetchrow(
        """
        SELECT *
        FROM auctions
        WHERE auction_id=$1
        """,
        auction_id,
    )

    if not auction:
        return

    if (
        auction["channel_id"] is None
        or auction["message_id"] is None
    ):
        return

    channel = bot.get_channel(
        int(auction["channel_id"])
    )

    if channel is None:
        try:
            channel = await bot.fetch_channel(
                int(auction["channel_id"])
            )
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            return

    try:
        message = await channel.fetch_message(
            int(auction["message_id"])
        )

        if auction["status"] == "active":
            view = AuctionView(
                auction_id=int(auction["auction_id"]),
                has_buy_now=auction["buy_now_price"] is not None,
            )
        else:
            view = None

        await message.edit(
            embed=auction_embed(
                auction
            ),
            view=view,
        )

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
    ):
        pass


async def place_auction_bid(
    *,
    auction_id: int,
    bidder_user_id: int,
    bidder_slot: str,
    amount: int,
) -> tuple[bool, str]:
    if amount <= 0:
        return False, "Bid amount must be greater than 0."

    async with db().acquire() as connection:
        async with connection.transaction():
            auction = await connection.fetchrow(
                """
                SELECT *
                FROM auctions
                WHERE auction_id=$1
                FOR UPDATE
                """,
                auction_id,
            )

            if not auction:
                return False, "That auction no longer exists."

            if auction["status"] != "active":
                return False, "That auction has already ended."

            now = datetime.now(timezone.utc)

            if auction["ends_at"] <= now:
                return False, "That auction has already reached its ending time."

            if int(auction["seller_user_id"]) == bidder_user_id:
                return False, "You cannot bid on your own auction."

            starting_price = int(
                auction["starting_price"]
            )

            highest_bid = await connection.fetchval(
                """
                SELECT MAX(amount)
                FROM auction_bids
                WHERE auction_id=$1
                """,
                auction_id,
            )

            if highest_bid is None:
                if amount < starting_price:
                    return (
                        False,
                        f"The first bid must be at least **{starting_price} Gold**.",
                    )
            elif amount <= int(highest_bid):
                return (
                    False,
                    "Your bid was not high enough.",
                )

            buy_now_price = auction["buy_now_price"]

            if (
                buy_now_price is not None
                and amount >= int(buy_now_price)
            ):
                return (
                    False,
                    "That amount reaches the Buy Now price. Use **Buy Now** instead.",
                )

            character, reserved_elsewhere, available = (
                await available_gold_for_character(
                    connection,
                    guild_id=int(auction["guild_id"]),
                    user_id=bidder_user_id,
                    slot=bidder_slot,
                    exclude_auction_id=auction_id,
                    lock=True,
                )
            )

            if not character:
                return (
                    False,
                    f"You do not have a {bidder_slot} character yet.",
                )

            if available < amount:
                return (
                    False,
                    (
                        f"You only have **{available} Gold available** for bids "
                        f"after your other active auction bids."
                    ),
                )

            await connection.execute(
                """
                INSERT INTO auction_bids (
                    auction_id,
                    guild_id,
                    bidder_user_id,
                    bidder_slot,
                    amount
                )
                VALUES ($1,$2,$3,$4,$5)

                ON CONFLICT (
                    auction_id,
                    bidder_user_id,
                    bidder_slot
                )
                DO UPDATE SET
                    amount=EXCLUDED.amount,
                    updated_at=now()
                """,
                auction_id,
                int(auction["guild_id"]),
                bidder_user_id,
                bidder_slot,
                amount,
            )

    auction = await db().fetchrow(
        """
        SELECT *
        FROM auctions
        WHERE auction_id=$1
        """,
        auction_id,
    )

    if auction:
        await post_auction_thread_message(
            auction,
            "🔨 A new bid has been placed.",
        )

    return (
        True,
        f"Your silent bid of **{amount} Gold** was accepted.",
    )


async def complete_auction_buy_now(
    *,
    auction_id: int,
    buyer_user_id: int,
    buyer_slot: str,
) -> tuple[bool, str]:
    async with db().acquire() as connection:
        async with connection.transaction():
            auction = await connection.fetchrow(
                """
                SELECT *
                FROM auctions
                WHERE auction_id=$1
                FOR UPDATE
                """,
                auction_id,
            )

            if not auction:
                return False, "That auction no longer exists."

            if auction["status"] != "active":
                return False, "That auction has already ended."

            if auction["ends_at"] <= datetime.now(timezone.utc):
                return False, "That auction has already reached its ending time."

            if int(auction["seller_user_id"]) == buyer_user_id:
                return False, "You cannot buy your own auction."

            if auction["buy_now_price"] is None:
                return False, "This auction does not have a Buy Now price."

            price = int(
                auction["buy_now_price"]
            )

            buyer, _, available = (
                await available_gold_for_character(
                    connection,
                    guild_id=int(auction["guild_id"]),
                    user_id=buyer_user_id,
                    slot=buyer_slot,
                    exclude_auction_id=auction_id,
                    lock=True,
                )
            )

            if not buyer:
                return (
                    False,
                    f"You do not have a {buyer_slot} character yet.",
                )

            if available < price:
                return (
                    False,
                    (
                        f"You need **{price} Gold** to use Buy Now. "
                        f"You currently have **{available} Gold available**."
                    ),
                )

            seller = await connection.fetchrow(
                """
                SELECT *
                FROM characters
                WHERE guild_id=$1
                AND user_id=$2
                AND slot=$3
                FOR UPDATE
                """,
                int(auction["guild_id"]),
                int(auction["seller_user_id"]),
                str(auction["seller_slot"]),
            )

            if not seller:
                return (
                    False,
                    "The seller's character no longer exists, so this auction cannot be completed.",
                )

            await connection.execute(
                """
                UPDATE characters
                SET gold=gold-$1,
                    updated_at=now()
                WHERE guild_id=$2
                AND user_id=$3
                AND slot=$4
                """,
                price,
                int(auction["guild_id"]),
                buyer_user_id,
                buyer_slot,
            )

            await connection.execute(
                """
                UPDATE characters
                SET gold=gold+$1,
                    updated_at=now()
                WHERE guild_id=$2
                AND user_id=$3
                AND slot=$4
                """,
                price,
                int(auction["guild_id"]),
                int(auction["seller_user_id"]),
                str(auction["seller_slot"]),
            )

            await connection.execute(
                """
                UPDATE auctions
                SET status='completed',
                    winner_user_id=$1,
                    winner_slot=$2,
                    winning_amount=$3
                WHERE auction_id=$4
                """,
                buyer_user_id,
                buyer_slot,
                price,
                auction_id,
            )

    finished = await db().fetchrow(
        """
        SELECT *
        FROM auctions
        WHERE auction_id=$1
        """,
        auction_id,
    )

    if finished:
        await post_auction_thread_message(
            finished,
            (
                f"🎉 Congratulations <@{buyer_user_id}>! "
                f"You won this auction with Buy Now for **{price} Gold**!"
            ),
        )

    await refresh_auction_message(
        auction_id
    )

    return (
        True,
        f"You bought **{finished['item_name'] if finished else 'the auction item'}** for **{price} Gold**.",
    )


async def resolve_expired_auction(
    auction_id: int,
) -> None:
    winner_user_id: int | None = None
    winner_slot: str | None = None
    winning_amount: int | None = None
    final_status = "ended_no_bids"

    async with db().acquire() as connection:
        async with connection.transaction():
            auction = await connection.fetchrow(
                """
                SELECT *
                FROM auctions
                WHERE auction_id=$1
                FOR UPDATE
                """,
                auction_id,
            )

            if not auction:
                return

            if auction["status"] != "active":
                return

            if auction["ends_at"] > datetime.now(timezone.utc):
                return

            seller = await connection.fetchrow(
                """
                SELECT *
                FROM characters
                WHERE guild_id=$1
                AND user_id=$2
                AND slot=$3
                FOR UPDATE
                """,
                int(auction["guild_id"]),
                int(auction["seller_user_id"]),
                str(auction["seller_slot"]),
            )

            bids = await connection.fetch(
                """
                SELECT *
                FROM auction_bids
                WHERE auction_id=$1
                ORDER BY amount DESC, updated_at ASC
                """,
                auction_id,
            )

            if not bids:
                final_status = "ended_no_bids"

            elif not seller:
                final_status = "ended_no_valid_bid"

            else:
                for bid in bids:
                    candidate_user_id = int(
                        bid["bidder_user_id"]
                    )
                    candidate_slot = str(
                        bid["bidder_slot"]
                    )
                    candidate_amount = int(
                        bid["amount"]
                    )

                    bidder, _, available = (
                        await available_gold_for_character(
                            connection,
                            guild_id=int(auction["guild_id"]),
                            user_id=candidate_user_id,
                            slot=candidate_slot,
                            exclude_auction_id=auction_id,
                            lock=True,
                        )
                    )

                    if not bidder:
                        continue

                    if available < candidate_amount:
                        continue

                    winner_user_id = candidate_user_id
                    winner_slot = candidate_slot
                    winning_amount = candidate_amount
                    final_status = "completed"

                    await connection.execute(
                        """
                        UPDATE characters
                        SET gold=gold-$1,
                            updated_at=now()
                        WHERE guild_id=$2
                        AND user_id=$3
                        AND slot=$4
                        """,
                        candidate_amount,
                        int(auction["guild_id"]),
                        candidate_user_id,
                        candidate_slot,
                    )

                    await connection.execute(
                        """
                        UPDATE characters
                        SET gold=gold+$1,
                            updated_at=now()
                        WHERE guild_id=$2
                        AND user_id=$3
                        AND slot=$4
                        """,
                        candidate_amount,
                        int(auction["guild_id"]),
                        int(auction["seller_user_id"]),
                        str(auction["seller_slot"]),
                    )

                    break

                if winner_user_id is None:
                    final_status = "ended_no_valid_bid"

            await connection.execute(
                """
                UPDATE auctions
                SET status=$1,
                    winner_user_id=$2,
                    winner_slot=$3,
                    winning_amount=$4
                WHERE auction_id=$5
                """,
                final_status,
                winner_user_id,
                winner_slot,
                winning_amount,
                auction_id,
            )

    finished = await db().fetchrow(
        """
        SELECT *
        FROM auctions
        WHERE auction_id=$1
        """,
        auction_id,
    )

    if not finished:
        return

    if final_status == "completed":
        await post_auction_thread_message(
            finished,
            (
                f"🏆 Congratulations <@{winner_user_id}>! "
                f"You won this auction for **{winning_amount} Gold**!"
            ),
        )

    elif final_status == "ended_no_bids":
        await post_auction_thread_message(
            finished,
            "⏰ This auction has ended with no bids.",
        )

    else:
        await post_auction_thread_message(
            finished,
            "⏰ This auction ended without a valid winner.",
        )

    await refresh_auction_message(
        auction_id
    )


@tasks.loop(minutes=1)
async def auction_expiry_loop():
    rows = await db().fetch(
        """
        SELECT auction_id
        FROM auctions
        WHERE status='active'
        AND ends_at <= now()
        ORDER BY ends_at ASC
        LIMIT 50
        """
    )

    for row in rows:
        try:
            await resolve_expired_auction(
                int(row["auction_id"])
            )
        except Exception:
            log.exception(
                "Failed to resolve auction %s",
                row["auction_id"],
            )


@auction_expiry_loop.before_loop
async def before_auction_expiry_loop():
    await bot.wait_until_ready()


class BidAmountModal(
    discord.ui.Modal,
    title="Place Silent Bid",
):
    amount = discord.ui.TextInput(
        label="Bid amount",
        placeholder="Enter the amount of gold to bid",
        min_length=1,
        max_length=12,
    )

    def __init__(
        self,
        *,
        auction_id: int,
        user_id: int,
        guild_id: int,
        slot: str,
    ) -> None:
        super().__init__()

        self.auction_id = auction_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.slot = slot

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if (
            interaction.user.id != self.user_id
            or interaction.guild_id != self.guild_id
        ):
            await interaction.response.send_message(
                "This bid form belongs to another player.",
                ephemeral=True,
            )
            return

        try:
            amount = int(
                str(self.amount).replace(",", "").strip()
            )
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole number for the bid amount.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        success, message = await place_auction_bid(
            auction_id=self.auction_id,
            bidder_user_id=self.user_id,
            bidder_slot=self.slot,
            amount=amount,
        )

        await interaction.followup.send(
            message,
            ephemeral=True,
        )


class BidSlotView(discord.ui.View):
    def __init__(
        self,
        *,
        auction_id: int,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=300)

        self.auction_id = auction_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.selected_slot: str | None = None

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=0,
        )

        self.continue_button = discord.ui.Button(
            label="Enter Bid",
            emoji="🔨",
            style=discord.ButtonStyle.success,
            row=1,
        )

        self.slot_select.callback = self.slot_changed
        self.continue_button.callback = self.continue_bid

        self.add_item(
            self.slot_select
        )
        self.add_item(
            self.continue_button
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
        ):
            return True

        await interaction.response.send_message(
            "This bidding menu belongs to another player.",
            ephemeral=True,
        )
        return False

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = self.slot_select.values[0]
        await interaction.response.defer()

    async def continue_bid(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            BidAmountModal(
                auction_id=self.auction_id,
                user_id=self.user_id,
                guild_id=self.guild_id,
                slot=self.selected_slot,
            )
        )


class BuyNowSlotView(discord.ui.View):
    def __init__(
        self,
        *,
        auction_id: int,
        user_id: int,
        guild_id: int,
        buy_now_price: int,
    ) -> None:
        super().__init__(timeout=300)

        self.auction_id = auction_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.buy_now_price = buy_now_price
        self.selected_slot: str | None = None
        self.processed = False

        self.slot_select = discord.ui.Select(
            placeholder="Choose Main or Alt",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Main",
                    value="Main",
                ),
                discord.SelectOption(
                    label="Alt",
                    value="Alt",
                ),
            ],
            row=0,
        )

        self.confirm_button = discord.ui.Button(
            label=f"Buy Now • {buy_now_price} Gold",
            emoji="💰",
            style=discord.ButtonStyle.danger,
            row=1,
        )

        self.slot_select.callback = self.slot_changed
        self.confirm_button.callback = self.confirm_buy

        self.add_item(
            self.slot_select
        )
        self.add_item(
            self.confirm_button
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
        ):
            return True

        await interaction.response.send_message(
            "This Buy Now menu belongs to another player.",
            ephemeral=True,
        )
        return False

    async def slot_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.selected_slot = self.slot_select.values[0]
        await interaction.response.defer()

    async def confirm_buy(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.processed:
            await interaction.response.send_message(
                "This Buy Now request has already been processed.",
                ephemeral=True,
            )
            return

        if self.selected_slot is None:
            await interaction.response.send_message(
                "Choose Main or Alt first.",
                ephemeral=True,
            )
            return

        self.processed = True

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        success, message = await complete_auction_buy_now(
            auction_id=self.auction_id,
            buyer_user_id=self.user_id,
            buyer_slot=self.selected_slot,
        )

        if not success:
            self.processed = False

        await interaction.edit_original_response(
            content=message,
            embed=None,
            view=(
                None
                if success
                else self
            ),
        )


class AuctionView(discord.ui.View):
    def __init__(
        self,
        *,
        auction_id: int,
        has_buy_now: bool,
    ) -> None:
        super().__init__(timeout=None)

        self.auction_id = auction_id
        self.has_buy_now = has_buy_now

        bid_button = discord.ui.Button(
            label="Place Bid",
            emoji="🔨",
            style=discord.ButtonStyle.primary,
            custom_id=f"astrelius:auction:{auction_id}:bid",
        )

        bid_button.callback = self.place_bid

        self.add_item(
            bid_button
        )

        if has_buy_now:
            buy_button = discord.ui.Button(
                label="Buy Now",
                emoji="💰",
                style=discord.ButtonStyle.success,
                custom_id=f"astrelius:auction:{auction_id}:buy",
            )

            buy_button.callback = self.buy_now

            self.add_item(
                buy_button
            )

    async def get_active_auction(
        self,
    ):
        return await db().fetchrow(
            """
            SELECT *
            FROM auctions
            WHERE auction_id=$1
            """,
            self.auction_id,
        )

    async def place_bid(
        self,
        interaction: discord.Interaction,
    ) -> None:
        auction = await self.get_active_auction()

        if not auction or auction["status"] != "active":
            await interaction.response.send_message(
                "That auction has already ended.",
                ephemeral=True,
            )
            return

        if interaction.user.id == int(auction["seller_user_id"]):
            await interaction.response.send_message(
                "You cannot bid on your own auction.",
                ephemeral=True,
            )
            return

        if auction["ends_at"] <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "That auction has already reached its ending time.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose the character you want to bid with.",
            view=BidSlotView(
                auction_id=self.auction_id,
                user_id=interaction.user.id,
                guild_id=int(auction["guild_id"]),
            ),
            ephemeral=True,
        )

    async def buy_now(
        self,
        interaction: discord.Interaction,
    ) -> None:
        auction = await self.get_active_auction()

        if not auction or auction["status"] != "active":
            await interaction.response.send_message(
                "That auction has already ended.",
                ephemeral=True,
            )
            return

        if interaction.user.id == int(auction["seller_user_id"]):
            await interaction.response.send_message(
                "You cannot buy your own auction.",
                ephemeral=True,
            )
            return

        if auction["buy_now_price"] is None:
            await interaction.response.send_message(
                "This auction does not have a Buy Now price.",
                ephemeral=True,
            )
            return

        if auction["ends_at"] <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "That auction has already reached its ending time.",
                ephemeral=True,
            )
            return

        price = int(
            auction["buy_now_price"]
        )

        await interaction.response.send_message(
            (
                f"Choose the character that will pay "
                f"**{price} Gold**."
            ),
            view=BuyNowSlotView(
                auction_id=self.auction_id,
                user_id=interaction.user.id,
                guild_id=int(auction["guild_id"]),
                buy_now_price=price,
            ),
            ephemeral=True,
        )


@bot.tree.command(
    name="auction",
    description="Start a three-day silent auction",
)
@app_commands.choices(
    slot=SLOTS,
)
@app_commands.describe(
    slot="Character receiving the auction payment",
    item="What you are selling",
    description="What the item does or what is included",
    starting_price="Minimum opening bid",
    buy_now_price="Optional price that ends the auction immediately",
)
async def auction_command(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    item: app_commands.Range[str, 1, 100],
    description: app_commands.Range[str, 1, 1000],
    starting_price: app_commands.Range[int, 1, 1_000_000_000],
    buy_now_price: int | None = None,
):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=True,
        )
        return

    if buy_now_price is not None:
        if buy_now_price <= 0:
            await interaction.response.send_message(
                "Buy Now must be greater than 0.",
                ephemeral=True,
            )
            return

        if buy_now_price < starting_price:
            await interaction.response.send_message(
                "Buy Now cannot be lower than the starting price.",
                ephemeral=True,
            )
            return

    seller = await db().fetchrow(
        """
        SELECT *
        FROM characters
        WHERE guild_id=$1
        AND user_id=$2
        AND slot=$3
        """,
        interaction.guild_id,
        interaction.user.id,
        slot.value,
    )

    if not seller:
        await interaction.response.send_message(
            f"You do not have a {slot.value} character yet.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    ends_at = (
        datetime.now(timezone.utc)
        + AUCTION_DURATION
    )

    auction_id = await db().fetchval(
        """
        INSERT INTO auctions (
            guild_id,
            seller_user_id,
            seller_slot,
            item_name,
            item_description,
            starting_price,
            buy_now_price,
            ends_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        RETURNING auction_id
        """,
        interaction.guild_id,
        interaction.user.id,
        slot.value,
        item.strip(),
        description.strip(),
        starting_price,
        buy_now_price,
        ends_at,
    )

    auction = await db().fetchrow(
        """
        SELECT *
        FROM auctions
        WHERE auction_id=$1
        """,
        auction_id,
    )

    view = AuctionView(
        auction_id=int(auction_id),
        has_buy_now=buy_now_price is not None,
    )

    try:
        auction_message = await interaction.channel.send(
            embed=auction_embed(
                auction
            ),
            view=view,
        )

        thread_name = (
            f"Auction #{auction_id} • {item.strip()}"
        )[:100]

        try:
            thread = await auction_message.create_thread(
                name=thread_name,
                auto_archive_duration=4320,
            )
        except discord.HTTPException:
            thread = await auction_message.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
            )

        await db().execute(
            """
            UPDATE auctions
            SET channel_id=$1,
                message_id=$2,
                thread_id=$3
            WHERE auction_id=$4
            """,
            interaction.channel.id,
            auction_message.id,
            thread.id,
            auction_id,
        )

        await thread.send(
            "🔨 Auction opened."
        )

    except (
        discord.Forbidden,
        discord.HTTPException,
    ) as exc:
        await db().execute(
            """
            UPDATE auctions
            SET status='cancelled'
            WHERE auction_id=$1
            """,
            auction_id,
        )

        await interaction.followup.send(
            (
                "I could not create the auction post or thread. "
                "Check my channel and thread permissions."
            ),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        (
            f"Auction #{auction_id} created. "
            "It will end in three days."
        ),
        ephemeral=True,
    )




# =========================================================
# PLAYER GOLD TRANSFERS
# =========================================================

@bot.tree.command(
    name="pay",
    description="Pay another player's character gold",
)
@app_commands.choices(
    slot=SLOTS,
    recipient_slot=SLOTS,
)
@app_commands.describe(
    slot="Your character paying the gold",
    recipient="Player receiving the gold",
    recipient_slot="Their character receiving the gold",
    amount="Amount of gold to pay",
    reason="Why you are paying them",
)
async def pay_command(
    interaction: discord.Interaction,
    slot: app_commands.Choice[str],
    recipient: discord.Member,
    recipient_slot: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 1_000_000_000],
    reason: app_commands.Range[str, 1, 300],
):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside the server.",
            ephemeral=False,
        )
        return

    if recipient.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot use `/pay` to pay yourself.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(
        ephemeral=False,
        thinking=True,
    )

    sender_slot = slot.value
    receiver_slot = recipient_slot.value

    async with db().acquire() as connection:
        async with connection.transaction():
            # Lock both character rows in a consistent order so two
            # simultaneous payments cannot race each other.
            character_keys = sorted(
                [
                    (interaction.user.id, sender_slot),
                    (recipient.id, receiver_slot),
                ],
                key=lambda value: (value[0], value[1]),
            )

            locked_rows = {}

            for user_id, character_slot in character_keys:
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM characters
                    WHERE guild_id=$1
                    AND user_id=$2
                    AND slot=$3
                    FOR UPDATE
                    """,
                    interaction.guild_id,
                    user_id,
                    character_slot,
                )

                if row:
                    locked_rows[
                        (user_id, character_slot)
                    ] = row

            sender = locked_rows.get(
                (
                    interaction.user.id,
                    sender_slot,
                )
            )

            if not sender:
                await interaction.followup.send(
                    (
                        f"You do not have a "
                        f"**{sender_slot}** character yet."
                    ),
                    ephemeral=False,
                )
                return

            receiver = locked_rows.get(
                (
                    recipient.id,
                    receiver_slot,
                )
            )

            if not receiver:
                await interaction.followup.send(
                    (
                        f"{recipient.mention} does not have a "
                        f"**{receiver_slot}** character yet."
                    ),
                    ephemeral=False,
                )
                return

            _, reserved_gold, available_gold = (
                await available_gold_for_character(
                    connection,
                    guild_id=interaction.guild_id,
                    user_id=interaction.user.id,
                    slot=sender_slot,
                    lock=False,
                )
            )

            if available_gold < amount:
                await interaction.followup.send(
                    (
                        f"You only have **{available_gold} Gold available** "
                        f"to spend. "
                        f"**{reserved_gold} Gold** is currently committed "
                        "to active auction bids."
                    ),
                    ephemeral=False,
                )
                return

            new_sender_gold = (
                int(sender["gold"]) - amount
            )

            new_receiver_gold = (
                int(receiver["gold"]) + amount
            )

            await connection.execute(
                """
                UPDATE characters
                SET gold=$1,
                    updated_at=now()
                WHERE guild_id=$2
                AND user_id=$3
                AND slot=$4
                """,
                new_sender_gold,
                interaction.guild_id,
                interaction.user.id,
                sender_slot,
            )

            await connection.execute(
                """
                UPDATE characters
                SET gold=$1,
                    updated_at=now()
                WHERE guild_id=$2
                AND user_id=$3
                AND slot=$4
                """,
                new_receiver_gold,
                interaction.guild_id,
                recipient.id,
                receiver_slot,
            )

            sender_name = str(
                sender["character_name"]
            )

            receiver_name = str(
                receiver["character_name"]
            )

    embed = discord.Embed(
        title="💰 Gold Payment",
        description=(
            f"{interaction.user.mention} paid "
            f"{recipient.mention} **{amount} Gold**."
        ),
        color=discord.Color.gold(),
    )

    embed.add_field(
        name="From",
        value=(
            f"**{sender_name}** "
            f"({sender_slot})"
        ),
        inline=True,
    )

    embed.add_field(
        name="To",
        value=(
            f"**{receiver_name}** "
            f"({receiver_slot})"
        ),
        inline=True,
    )

    embed.add_field(
        name="Reason",
        value=reason.strip(),
        inline=False,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=False,
    )




# =========================================================
# FACTION POINTS
# =========================================================

async def change_faction_points(
    *,
    guild_id: int,
    member: discord.Member,
    slot: str,
    amount: int,
    mode: str,
) -> tuple[bool, str]:
    async with db().acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT *
                FROM characters
                WHERE guild_id=$1
                AND user_id=$2
                AND slot=$3
                FOR UPDATE
                """,
                guild_id,
                member.id,
                slot,
            )

            if not row:
                return False, f"{member.display_name} does not have a {slot} character."

            current = max(0, int(row["faction_points"]))
            new_value = current + amount if mode == "add" else max(0, current - amount)
            faction = faction_for_member(member)

            await connection.execute(
                """
                UPDATE characters
                SET faction_points=$1,
                    faction=$2,
                    updated_at=now()
                WHERE guild_id=$3
                AND user_id=$4
                AND slot=$5
                """,
                new_value,
                faction,
                guild_id,
                member.id,
                slot,
            )

    return True, f"{member.display_name} ({slot}) now has **{new_value} Faction Points**."


@faction_points_group.command(
    name="add",
    description="Add faction points to one or more characters",
)
@app_commands.check(staff_check)
@app_commands.choices(
    slot=SLOTS,
    slot2=SLOTS,
    slot3=SLOTS,
    slot4=SLOTS,
    slot5=SLOTS,
    slot6=SLOTS,
    slot7=SLOTS,
    slot8=SLOTS,
    slot9=SLOTS,
    slot10=SLOTS,
)
async def faction_points_add(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 1_000_000],
    member2: discord.Member | None = None,
    slot2: app_commands.Choice[str] | None = None,
    member3: discord.Member | None = None,
    slot3: app_commands.Choice[str] | None = None,
    member4: discord.Member | None = None,
    slot4: app_commands.Choice[str] | None = None,
    member5: discord.Member | None = None,
    slot5: app_commands.Choice[str] | None = None,
    member6: discord.Member | None = None,
    slot6: app_commands.Choice[str] | None = None,
    member7: discord.Member | None = None,
    slot7: app_commands.Choice[str] | None = None,
    member8: discord.Member | None = None,
    slot8: app_commands.Choice[str] | None = None,
    member9: discord.Member | None = None,
    slot9: app_commands.Choice[str] | None = None,
    member10: discord.Member | None = None,
    slot10: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer(ephemeral=True)

    targets = [
        (member, slot),
        (member2, slot2),
        (member3, slot3),
        (member4, slot4),
        (member5, slot5),
        (member6, slot6),
        (member7, slot7),
        (member8, slot8),
        (member9, slot9),
        (member10, slot10),
    ]

    cleaned_targets = []
    seen = set()

    for target_member, target_slot in targets:
        if target_member is None and target_slot is None:
            continue

        if target_member is None or target_slot is None:
            await interaction.followup.send(
                "Each optional target needs both a player and a Main/Alt slot.",
                ephemeral=True,
            )
            return

        key = (target_member.id, target_slot.value)
        if key in seen:
            continue

        seen.add(key)
        cleaned_targets.append((target_member, target_slot.value))

    results = []

    for target_member, target_slot in cleaned_targets:
        success, message = await change_faction_points(
            guild_id=interaction.guild_id,
            member=target_member,
            slot=target_slot,
            amount=amount,
            mode="add",
        )
        results.append(("✅ " if success else "❌ ") + message)

    await interaction.followup.send(
        "\n".join(results),
        ephemeral=True,
    )


@faction_points_group.command(
    name="remove",
    description="Remove faction points from a character",
)
@app_commands.check(staff_check)
@app_commands.choices(slot=SLOTS)
async def faction_points_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    slot: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 1_000_000],
):
    await interaction.response.defer(ephemeral=True)

    success, message = await change_faction_points(
        guild_id=interaction.guild_id,
        member=member,
        slot=slot.value,
        amount=amount,
        mode="remove",
    )

    await interaction.followup.send(
        message,
        ephemeral=True,
    )



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
