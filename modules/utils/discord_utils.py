import logging
from collections.abc import Iterable
from typing import Optional

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from modules import cache
from modules.utils import mysql

def has_roles(*role_names: str):
    async def predicate(interaction: Interaction) -> bool:
        # Check if the user has any of the specified roles by name
        for role_name in role_names:
            if discord.utils.get(interaction.user.roles, name=role_name):
                return True
        return False
    return app_commands.check(predicate)


def has_role_or_permission(*role_names: str):
    async def predicate(interaction: Interaction) -> bool:
        user = interaction.user

        # Check if the user has moderate_members permission
        if user.guild_permissions.moderate_members:
            return True

        # Check if the user has any of the specified roles
        if any(role.name in role_names for role in user.roles):
            return True

        return False

    return app_commands.check(predicate)

async def message_user(user: discord.User, content: str, embed: discord.Embed = None):
    # Attempt to send a DM
    try:
        message = await user.send(content, embed=embed) if embed else await user.send(content)
    except discord.Forbidden:
        # ignore
        message = None
        pass
    return message

async def safe_get_channel(bot: commands.Bot, channel_id: int) -> discord.TextChannel:
    chan = bot.get_channel(channel_id)
    if chan is None:
        try:
            chan = await bot.fetch_channel(channel_id)
        except discord.HTTPException as e:
            print(f"failed to fetch channel {channel_id}: {e}")
    return chan

async def safe_get_user(bot: discord.Client, user_id: int, *, force_fetch: bool = False) -> Optional[discord.User]:
    """
    Return a User object, optionally forcing a fresh REST fetch.

    - When `force_fetch` is False (default), try the local cache first.
    - When `force_fetch` is True, bypass the cache but still fall back to it if the fetch fails.
    - Returns None only when no data could be retrieved.
    """
    cached = bot.get_user(user_id)
    if cached is not None and not force_fetch:
        return cached

    try:
        user = await bot.fetch_user(user_id)   # 1 REST call
        return user
    except discord.NotFound:
        # user_id no longer exists (account deleted)
        return None
    except discord.Forbidden:
        # we don't share a guild and the user has DMs disabled
        return cached
    except discord.HTTPException as e:
        # network / rate-limit issue - log and fail gracefully
        print(f"[safe_get_user] fetch_user({user_id}) failed: {e}")
        return cached


async def safe_get_member(guild: discord.Guild, user_id: int, *, force_fetch: bool = False) -> Optional[discord.Member]:
    """
    Safely get a Member from cache or fetch.

    When ``force_fetch`` is True, always perform a fresh fetch to hydrate newer fields
    (e.g. member flags). Returns None if the user cannot be fetched.
    """
    member = guild.get_member(user_id)
    if member is not None and not force_fetch:
        return member

    try:
        fetched = await guild.fetch_member(user_id)
        return fetched or member
    except (discord.NotFound, discord.Forbidden):
        return member if member is not None else None
    except discord.HTTPException as e:
        print(f"[safe_get_member] fetch_member({user_id}) failed: {e}")
        return member if member is not None else None

async def ensure_member_with_presence(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    """
    Ensure we have a Member object enhanced with presence/activities when possible.

    - If the member is cached and already has the `activities` attribute populated, return it.
    - Otherwise, use `guild.query_members` with `presences=True` to fetch a fresh Member with presence.
    - Falls back to the cached Member (or None) if the query fails.
    """
    m = guild.get_member(user_id)
    if m is not None and getattr(m, "activities", None) is not None:
        return m
    try:
        # Query a single member by ID and request presences
        members = await guild.query_members(user_ids=[user_id], presences=True, limit=1)
        return members[0] if members else m
    except Exception:
        return m
    
async def safe_get_message(channel: discord.TextChannel, message_id: int) -> Optional[discord.Message]:
    """
    Safely get a Message from cache or fetch.
    Returns None if the message is not found or can't be fetched.
    """
    message = cache.get_cached_message(channel.guild.id, message_id)
    if message is not None:
        return message
    try:
        message = await channel.fetch_message(message_id)
        await cache.cache_message(message)  # Cache the message for future use
        return message
    except (discord.NotFound, discord.Forbidden):
        return None
    except discord.HTTPException as e:
        print(f"[safe_get_message] fetch_message({message_id}) failed: {e}")
        return None
    
async def require_accelerated(interaction: Interaction):
    """
    Check if the command is being used in a server with an Accelerated subscription.
    If not, respond with an error message.
    """
    if not await mysql.is_accelerated(guild_id=interaction.guild.id):
        translator = getattr(interaction.client, "translate", None)
        fallback = "This command is only available for Accelerated (Premium) servers. Use `/accelerated subscribe` to enable it."
        message = (
            translator(
                "modules.utils.discord_utils.require_accelerated",
                fallback=fallback,
            )
            if callable(translator)
            else fallback
        )
        await interaction.response.send_message(
            message,
            ephemeral=True
        )
        return False
    return True


def resolve_role_references(
    guild: discord.Guild,
    references: Iterable[object],
    *,
    allow_names: bool = True,
    logger: logging.Logger | None = None,
) -> list[discord.Role]:
    """Resolve a collection of role identifiers or names to concrete :class:`discord.Role` objects.

    Parameters
    ----------
    guild:
        The guild whose role cache will be consulted.
    references:
        An iterable of role references. Items may be role IDs (``int`` or digit-only ``str``),
        role names (``str``) when ``allow_names`` is True, or concrete :class:`discord.Role`
        instances.
    allow_names:
        When True (default), fall back to role name lookups for non-numeric strings. When False,
        name lookups are skipped and only numeric identifiers are considered valid.
    logger:
        Optional :class:`logging.Logger` used for debug-level diagnostics when references are
        invalid or cannot be resolved.

    Returns
    -------
    list[:class:`discord.Role`]
        Unique role objects resolved from the provided references, preserving input order.
    """

    resolved: list[discord.Role] = []
    seen: set[int] = set()

    for reference in references:
        if reference is None:
            continue

        role: discord.Role | None = None
        role_id: int | None = None

        if isinstance(reference, discord.Role):
            role = reference
        else:
            text = str(reference).strip()
            if not text:
                if logger is not None:
                    logger.info(
                        "Ignoring blank role reference for guild %s", guild.id
                    )
                continue

            if text.isdigit():
                try:
                    role_id = int(text)
                except ValueError:
                    role_id = None
            elif hasattr(reference, "id"):
                try:
                    role_id = int(getattr(reference, "id"))
                except (TypeError, ValueError):
                    role_id = None
            elif allow_names:
                role = discord.utils.get(guild.roles, name=text)
                if role is None and logger is not None:
                    logger.info(
                        "Role with name '%s' not found in guild %s", text, guild.id
                    )

        if role is None and role_id is not None:
            role = guild.get_role(role_id)
            if role is None and logger is not None:
                logger.info(
                    "Role with ID %s not found in guild %s", role_id, guild.id
                )

        if role is None:
            if logger is not None and role_id is None:
                logger.info(
                    "Ignoring invalid role reference for guild %s: %r", guild.id, reference
                )
            continue

        if role.id in seen:
            continue

        seen.add(role.id)
        resolved.append(role)

    return resolved
