from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

import asyncpg
import discord
import fitz
import pytesseract
from discord import app_commands
from discord.ext import commands
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

STAT_NAMES = (
    "Strength",
    "Dexterity",
    "Constitution",
    "Intelligence",
    "Wisdom",
    "Charisma",
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

    # MP controls exact server level.
    server_mp = max(
        0,
        int(row["mp"]),
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

    # Fix older mismatched DB records.
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
            interaction.guild_id,
            interaction.user.id,
            slot.value,
        )

    # Rank displayed from actual Discord role.
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

    # Class and subclass are displayed
    # on separate lines, Class first.
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
        "\n".join(class_lines)
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

    # D&D Beyond never changes:
    #
    # MP
    # Level
    # Rank
    # Gold
    # Materials
    # Downtime
    #
    # It only updates sheet-derived details.
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
