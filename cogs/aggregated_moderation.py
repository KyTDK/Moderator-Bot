import discord
from discord.ext import commands
from modules.utils import mod_logging, mysql
from modules.moderation import strike
from modules.utils.discord_utils import safe_get_channel, safe_get_member, safe_get_message
from modules.nsfw_scanner import NSFWScanner, handle_nsfw_content
from modules.worker_queue import WorkerQueue
from datetime import timedelta
from discord.utils import utcnow
import os
from dotenv import load_dotenv
load_dotenv()

FREE_MAX_WORKERS = int(os.getenv("FREE_MAX_WORKERS", 2))
ACCELERATED_MAX_WORKERS = int(os.getenv("ACCELERATED_MAX_WORKERS", 5))
FREE_MAX_WORKERS_BURST = int(os.getenv("FREE_MAX_WORKERS_BURST", FREE_MAX_WORKERS))
ACCELERATED_MAX_WORKERS_BURST = int(os.getenv("ACCELERATED_MAX_WORKERS_BURST", ACCELERATED_MAX_WORKERS))
WORKER_BACKLOG_HIGH = int(os.getenv("WORKER_BACKLOG_HIGH", 30))
WORKER_BACKLOG_LOW = int(os.getenv("WORKER_BACKLOG_LOW", 5))
WORKER_AUTOSCALE_CHECK_INTERVAL = float(os.getenv("WORKER_AUTOSCALE_CHECK_INTERVAL", 2))
WORKER_AUTOSCALE_SCALE_DOWN_GRACE = float(os.getenv("WORKER_AUTOSCALE_SCALE_DOWN_GRACE", 15))

class AggregatedModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scanner = NSFWScanner(bot)
        self.free_queue = WorkerQueue(
            max_workers=FREE_MAX_WORKERS,
            autoscale_max=FREE_MAX_WORKERS_BURST,
            backlog_high_watermark=WORKER_BACKLOG_HIGH,
            backlog_low_watermark=WORKER_BACKLOG_LOW,
            autoscale_check_interval=WORKER_AUTOSCALE_CHECK_INTERVAL,
            scale_down_grace=WORKER_AUTOSCALE_SCALE_DOWN_GRACE,
            name="free",
        )
        self.accelerated_queue = WorkerQueue(
            max_workers=ACCELERATED_MAX_WORKERS,
            autoscale_max=ACCELERATED_MAX_WORKERS_BURST,
            backlog_high_watermark=WORKER_BACKLOG_HIGH,
            backlog_low_watermark=WORKER_BACKLOG_LOW,
            autoscale_check_interval=WORKER_AUTOSCALE_CHECK_INTERVAL,
            scale_down_grace=WORKER_AUTOSCALE_SCALE_DOWN_GRACE,
            name="accelerated",
        )

    def _is_new_guild(self, guild_id: int) -> bool:
        """Return True if the bot joined this guild within the last 30 minutes."""
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild or not self.bot.user:
                return False
            me = guild.me or guild.get_member(self.bot.user.id)
            if not me or not me.joined_at:
                return False
            return (utcnow() - me.joined_at) <= timedelta(minutes=30)
        except Exception:
            # On any lookup error, do not elevate priority
            return False

    async def add_to_queue(self, coro, guild_id: int):
        """
        Add a task to the appropriate queue.
        accelerated=True means higher priority
        """
        accelerated = await mysql.is_accelerated(guild_id=guild_id)
        # Treat newly added guilds as accelerated for the first 30 minutes
        if not accelerated and self._is_new_guild(guild_id):
            accelerated = True

        queue = self.accelerated_queue if accelerated else self.free_queue
        await queue.add_task(coro)

    async def handle_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        guild_id = message.guild.id

        if not await mysql.get_settings(guild_id, "nsfw-enabled"):
            return
        
        # Skip age restricted
        scan_age_restricted = await mysql.get_settings(guild_id, "scan-age-restricted")

        chan = message.channel
        parent = getattr(chan, "parent", None)
        is_age_restricted = (
            (hasattr(chan, "is_nsfw") and chan.is_nsfw())
            or (parent is not None and hasattr(parent, "is_nsfw") and parent.is_nsfw())
        )

        if is_age_restricted and not scan_age_restricted:
            return
        
        # Exclude age restricted
        if message.channel.id in [int(c) for c in (await mysql.get_settings(guild_id, "exclude-channels") or [])]:
            return

        async def scan_task():
            flagged = await self.scanner.is_nsfw(
                message=message,
                guild_id=guild_id,
                nsfw_callback=handle_nsfw_content
            )
            if flagged:
                try:
                    embed = discord.Embed(
                        title="NSFW Content Detected",
                        description=(
                            f"{message.author.mention}, your message was removed because it contained "
                            "explicit or inappropriate content."
                        ),
                        color=discord.Color.red()
                    )
                    embed.set_thumbnail(url=message.author.display_avatar.url)
                    await mod_logging.log_to_channel(
                        embed=embed,
                        channel_id=message.channel.id,
                        bot=self.bot
                    )

                except (discord.Forbidden, discord.NotFound):
                    print("[NSFW] Could not notify user about message removal.")
        await self.add_to_queue(scan_task(), guild_id=guild_id)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        guild = reaction.message.guild
        if guild is None:
            return
        if not await mysql.get_settings(guild.id, "nsfw-enabled"):
            return
        
        if not isinstance(reaction.emoji, (discord.Emoji, discord.PartialEmoji)):
            return
        if reaction.count > 1:
            return

        await self._queue_emoji_scan(
            guild=reaction.message.guild,
            message=reaction.message,
            emoji=reaction.emoji,
            member=user
        )

    # Fallback for when message isn't cached
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.member and payload.member.bot:
            return
    
        # Skip non-custom emoji
        if not payload.emoji.is_custom_emoji():
            return

        # If cached, was scanned in on_reaction_add, skip
        cached_msg = self.bot._connection._get_message(payload.message_id)
        if cached_msg is not None:
            return

        try:
            channel = await safe_get_channel(self.bot, payload.channel_id)
            message = await safe_get_message(channel, payload.message_id)
        except discord.NotFound:
            return
        except discord.HTTPException as e:
            print(f"[raw] fetch failed: {e}")
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if not await mysql.get_settings(guild.id, "nsfw-enabled"):
            return
        
        member = await safe_get_member(guild, payload.user_id)
        emoji = payload.emoji

        # Avoid scanning again
        for r in message.reactions:
            if r.emoji == emoji and r.count > 1:
                return

        await self._queue_emoji_scan(
            guild=guild,
            message=message,
            emoji=emoji,
            member=member
        )

    async def _queue_emoji_scan(
        self,
        *,
        guild: discord.Guild,
        message: discord.Message,
        emoji: discord.Emoji | discord.PartialEmoji,
        member: discord.Member | discord.User | None
    ):
        async def scan_task():
            flagged = await self.scanner.is_nsfw(
                url=str(emoji.url),
                member=member,
                guild_id=guild.id,
                nsfw_callback=handle_nsfw_content
            )
            if flagged:
                try:
                    await message.remove_reaction(emoji, member)
                except discord.Forbidden:
                    print("[emoji] lacking permissions to remove reaction")
                except discord.HTTPException as e:
                    print(f"[emoji] failed to remove reaction: {e}")

        await self.add_to_queue(scan_task(), guild_id=guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._queue_avatar_scan(member.guild, member, is_join=True)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if before.avatar == after.avatar:
            return

        for guild in self.bot.guilds:
            if not await mysql.get_settings(guild.id, "check-pfp"):
                continue

            member = await safe_get_member(guild, after.id)
            if member:
                await self._queue_avatar_scan(guild, member)

    async def _queue_avatar_scan(self, guild: discord.Guild, member: discord.Member, is_join: bool = False):
        if not await mysql.get_settings(guild.id, "nsfw-enabled"):
            return
        if not await mysql.get_settings(guild.id, "check-pfp"):
            return

        avatar_url = member.avatar.url if member.avatar else None
        if not avatar_url:
            return

        async def scan_task():
            is_nsfw = await self.scanner.is_nsfw(
                url=avatar_url,
                member=member,
                guild_id=guild.id
            )

            if is_nsfw:
                action = await mysql.get_settings(guild.id, "nsfw-pfp-action")
                message = await mysql.get_settings(guild.id, "nsfw-pfp-message")
                await strike.perform_disciplinary_action(member, self.bot, action, message, source="pfp")
                if message:
                    try:
                        await member.send(message)
                    except discord.Forbidden:
                        print(f"[PFP] Cannot DM {member.display_name}.")
            else:
                result, _ = await mysql.execute_query(
                    """
                    SELECT timeout_until FROM timeouts
                    WHERE user_id = %s AND guild_id = %s
                    AND timeout_until > UTC_TIMESTAMP()
                    AND source = 'pfp'
                    """,
                    (member.id, guild.id),
                    fetch_one=True,
                )
                if await mysql.get_settings(guild.id, "unmute-on-safe-pfp") and result is not None:
                    try:
                        await member.edit(timed_out_until=None, reason="Profile picture updated to a safe image.")
                        await mysql.execute_query(
                            "DELETE FROM timeouts WHERE user_id=%s AND guild_id=%s AND source='pfp'",
                            (member.id, guild.id)
                        )
                        print(f"[PFP] Cleared timeout for {member.display_name}")
                    except discord.Forbidden:
                        print(f"[PFP] Missing permission to untimeout {member.display_name}")
                    except discord.HTTPException as e:
                        if e.code != 50035:
                            print(f"[PFP] Failed to untimeout {member.display_name}: {e}")

        await self.add_to_queue(scan_task(), guild_id=guild.id)

    async def cog_load(self):
        await self.scanner.start()
        await self.free_queue.start()
        await self.accelerated_queue.start()

    async def cog_unload(self):
        await self.scanner.stop()
        await self.free_queue.stop()
        await self.accelerated_queue.stop()

async def setup(bot: commands.Bot):
    await bot.add_cog(AggregatedModerationCog(bot))
