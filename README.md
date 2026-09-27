# D&D Discord Bot

A PostgreSQL-backed Discord bot for two character slots per member (`Main` and `Alt`).

## Player commands

- `/character create` — create or rename a Main/Alt record.
- `/character delete` — delete a character after typing `DELETE`.
- `/inventory` — show display name, level/rank, MP, MP to next level, gold, downtime, stats, proficiencies, and materials.
- `/downtime spend` — spend downtime in 8-hour increments, never below zero.
- `/sheet import` — import a D&D Beyond PDF (preferred) or screenshot. It updates name, level, stats, and proficiencies while preserving MP, gold, materials, downtime, and other server-owned data.

## DM/GM commands

Members with a Discord role named `DM` or `GM` can use:

- `/mp add`, `/mp remove`, `/mp set`
- `/level set`
- `/gold add`, `/gold remove`, `/gold set`
- `/downtime add`, `/downtime remove`, `/downtime set`
- `/materials add`, `/materials remove`

MP changes recalculate level from the configured threshold chart. Setting level sets MP to that level's minimum threshold. Level changes synchronize one of the `F Rank` through `S Rank` Discord roles. The bot role must be above those roles.

## Deployment

The included `render.yaml` creates a continuously running worker and PostgreSQL database. Add `DISCORD_TOKEN` only through the hosting provider's secret field—never commit it. `DATABASE_URL` is wired automatically by the blueprint.

The Discord application needs the `bot` and `applications.commands` scopes plus View Channels, Send Messages, Embed Links, Read Message History, Attach Files, and Manage Roles permissions. No privileged gateway intents are required.

