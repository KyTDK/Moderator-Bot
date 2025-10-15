import asyncio
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import time

import discord
from discord.ext import commands, tasks

from dotenv import load_dotenv

from modules.metrics import log_media_scan
from modules.utils import mysql, mod_logging
from modules.utils.discord_utils import safe_get_member
from modules.utils.time import parse_duration

from cogs.autonomous_moderation import helpers as am_helpers
from cogs.voice_moderation.models import VoiceModerationReport
from cogs.voice_moderation.prompt import VOICE_SYSTEM_PROMPT, BASE_SYSTEM_TOKENS
from cogs.voice_moderation.voice_io import (
    HARVEST_WINDOW_SECONDS,
    harvest_pcm_chunk,
    transcribe_harvest_chunk,
)
from modules.ai.pipeline import run_moderation_pipeline_voice

load_dotenv()
AUTOMOD_OPENAI_KEY = os.getenv("AUTOMOD_OPENAI_KEY")
AIMOD_MODEL = os.getenv("AIMOD_MODEL", "gpt-5-nano")

violation_cache: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10))

class _GuildVCState:
    def __init__(self) -> None:
        self.channel_ids: list[int] = []
        self.index: int = 0
        self.busy_task: Optional[asyncio.Task] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.next_start: datetime = datetime.now(timezone.utc)

class VoiceModeratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._state: dict[int, _GuildVCState] = {}
        self.loop.start()

    def cog_unload(self) -> None:
        self.loop.cancel()

    def _get_state(self, guild_id: int) -> _GuildVCState:
        st = self._state.get(guild_id)
        if not st:
            st = _GuildVCState()
            self._state[guild_id] = st
        return st

    @tasks.loop(seconds=10)
    async def loop(self):
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            try:
                await self._tick_guild(guild, now)
            except Exception as e:
                print(f"[VCMod] tick failed for {guild.id}: {e}")

    async def _tick_guild(self, guild: discord.Guild, now: datetime):
        settings = await mysql.get_settings(
            guild.id,
            [
                "vcmod-enabled",
                "vcmod-channels",
                "vcmod-listen-duration",
                "vcmod-idle-duration",
                "vcmod-saver-mode",
                "vcmod-rules",
                "vcmod-high-accuracy",
                "vcmod-high-quality-transcription",
                "vcmod-detection-action",
                "aimod-debug",
                "aimod-channel",
                "monitor-channel",
                "vcmod-transcript-channel",
                "vcmod-transcript-only",
            ],
        )

        enabled = settings.get("vcmod-enabled") or False
        channels = settings.get("vcmod-channels") or []
        saver_mode = settings.get("vcmod-saver-mode") or False
        listen_str = settings.get("vcmod-listen-duration") or "2m"
        idle_str = settings.get("vcmod-idle-duration") or "30s"

        if not enabled or not AUTOMOD_OPENAI_KEY:
            # Ensure we disconnect if previously connected
            st = self._get_state(guild.id)
            if st.voice and st.voice.is_connected():
                try:
                    await st.voice.disconnect(force=True)
                except Exception:
                    pass
                st.voice = None
            return

        # Resolve channel IDs list
        channel_ids: list[int] = []
        for ch in (channels or []):
            try:
                # settings store IDs; ensure int
                cid = int(getattr(ch, "id", ch))
                channel_ids.append(cid)
            except Exception:
                continue

        if not channel_ids:
            return

        st = self._get_state(guild.id)
        st.channel_ids = channel_ids

        if st.busy_task and not st.busy_task.done():
            return  # already working on this guild

        if now < st.next_start:
            return

        # Pick next channel
        if st.index >= len(channel_ids):
            st.index = 0

        chan_id = channel_ids[st.index]
        channel = guild.get_channel(chan_id)
        if not isinstance(channel, discord.VoiceChannel):
            # Try to fetch, but avoid REST spam
            try:
                fetched = await self.bot.fetch_channel(chan_id)
                if isinstance(fetched, discord.VoiceChannel):
                    channel = fetched
                else:
                    st.index += 1
                    st.next_start = datetime.now(timezone.utc)
                    return
            except Exception:
                st.index += 1
                st.next_start = datetime.now(timezone.utc)
                return

        high_accuracy = settings.get("vcmod-high-accuracy") or False
        rules = settings.get("vcmod-rules") or ""
        action_setting = settings.get("vcmod-detection-action") or ["auto"]
        aimod_debug = settings.get("aimod-debug") or False
        log_channel = settings.get("aimod-channel") or settings.get("monitor-channel")
        transcript_channel_id = settings.get("vcmod-transcript-channel")
        transcript_only = settings.get("vcmod-transcript-only") or False
        high_quality_transcription = settings.get("vcmod-high-quality-transcription") or False

        listen_delta = parse_duration(listen_str) or timedelta(minutes=2)
        idle_delta = parse_duration(idle_str) or timedelta(seconds=30)

        do_listen = not saver_mode

        async def _run():
            await self._run_cycle_for_channel(
                guild=guild,
                channel=channel,
                do_listen=do_listen,
                listen_delta=listen_delta,
                idle_delta=idle_delta,
                high_accuracy=high_accuracy,
                high_quality_transcription=high_quality_transcription,
                rules=rules,
                transcript_only=transcript_only,
                action_setting=action_setting,
                aimod_debug=aimod_debug,
                log_channel=log_channel,
                transcript_channel_id=transcript_channel_id,
            )

        st.busy_task = self.bot.loop.create_task(_run())

        def _done_callback(_):
            try:
                st.index += 1
                st.next_start = datetime.now(timezone.utc)
            except Exception:
                st.next_start = datetime.now(timezone.utc) + timedelta(seconds=10)

        st.busy_task.add_done_callback(_done_callback)

    async def _run_cycle_for_channel(
        self,
        *,
        guild: discord.Guild,
        channel: discord.VoiceChannel,
        do_listen: bool,
        listen_delta: timedelta,
        idle_delta: timedelta,
        high_accuracy: bool,
        high_quality_transcription: bool,
        rules: str,
        transcript_only: bool,
        action_setting: list[str],
        aimod_debug: bool,
        log_channel: Optional[int],
        transcript_channel_id: Optional[int],
    ):
        # Use the voice IO helper to handle connection and harvesting
        st = self._get_state(guild.id)
        utterances: list[tuple[int, str, datetime]] = []

        async def _record_voice_metrics(
            *,
            status: str,
            report_obj: VoiceModerationReport | None,
            total_tokens: int,
            request_cost: float,
            usage_snapshot: dict[str, Any],
            duration_ms: int,
            error: str | None = None,
        ) -> None:
            violations_payload: list[dict[str, Any]] = []
            if report_obj and getattr(report_obj, "violations", None):
                for violation in list(report_obj.violations)[:10]:
                    violations_payload.append(
                        {
                            "user_id": getattr(violation, "user_id", None),
                            "rule": getattr(violation, "rule", None),
                            "reason": getattr(violation, "reason", None),
                            "actions": list(getattr(violation, "actions", []) or []),
                        }
                    )

            scan_payload: dict[str, Any] = {
                "is_nsfw": bool(violations_payload),
                "reason": status,
                "violations": violations_payload,
                "violations_count": len(violations_payload),
                "total_tokens": int(total_tokens),
                "request_cost_usd": float(request_cost or 0),
                "usage_snapshot": usage_snapshot or {},
                "transcript_only": bool(transcript_only),
                "high_accuracy": bool(high_accuracy),
            }
            if error:
                scan_payload["error"] = error

            extra_context = {
                "status": status,
                "utterance_count": len(utterances),
                "listen_window_seconds": listen_delta.total_seconds(),
                "idle_window_seconds": idle_delta.total_seconds(),
                "transcript_only": bool(transcript_only),
                "high_accuracy": bool(high_accuracy),
            }

            try:
                await log_media_scan(
                    guild_id=guild.id,
                    channel_id=getattr(channel, "id", None),
                    user_id=None,
                    message_id=None,
                    content_type="voice",
                    detected_mime=None,
                    filename=None,
                    file_size=None,
                    source="voice_pipeline",
                    scan_result=scan_payload,
                    scan_duration_ms=duration_ms,
                    accelerated=await mysql.is_accelerated(guild_id=guild.id),
                    reference=f"voice:{guild.id}:{getattr(channel, 'id', 'unknown')}",
                    extra_context=extra_context,
                )
            except Exception as metrics_exc:
                print(f"[metrics] Voice metrics logging failed for guild {guild.id}: {metrics_exc}")
        chunk_tasks: list[asyncio.Task] = []
        sem = asyncio.Semaphore(2)
        if do_listen:
            # Harvest and transcribe repeatedly every window seconds during the listen duration
            start = time.monotonic()
            next_tick = start
            while True:
                # Harvest only, fast path
                st.voice, eligible_map, end_ts_map, duration_map_s = await harvest_pcm_chunk(
                    guild=guild,
                    channel=channel,
                    voice=st.voice,
                    do_listen=True,
                    idle_delta=idle_delta,
                    window_seconds=HARVEST_WINDOW_SECONDS,
                )
                if eligible_map:
                    # Spawn a background transcribe task, bounded by semaphore
                    async def _do_transcribe(em=eligible_map, etm=end_ts_map, dmap=duration_map_s):
                        async with sem:
                            try:
                                chunk_utts, _ = await transcribe_harvest_chunk(
                                    guild_id=guild.id,
                                    api_key=AUTOMOD_OPENAI_KEY or "",
                                    eligible_map=em,
                                    end_ts_map=etm,
                                    duration_map_s=dmap,
                                    high_quality=high_quality_transcription,
                                )
                                if chunk_utts:
                                    utterances.extend(chunk_utts)
                            except Exception as e:
                                print(f"[VCMod] transcribe task failed: {e}")

                    t = asyncio.create_task(_do_transcribe())
                    chunk_tasks.append(t)
                # schedule next harvest
                next_tick += HARVEST_WINDOW_SECONDS
                remaining = min(listen_delta.total_seconds(), next_tick - time.monotonic())
                if remaining > 0:
                    await asyncio.sleep(remaining)
                else:
                    # We overran the target tick; useful for debugging cadence
                    print(f"[VCMod] Harvest overrun by {abs(remaining):.2f}s (transcribe backlog likely)")
                # stop once listen window elapsed
                if (time.monotonic() - start) >= listen_delta.total_seconds():
                    break
            # Wait for all chunk tasks to complete before processing results
            if chunk_tasks:
                await asyncio.gather(*chunk_tasks, return_exceptions=True)
        else:
            # Saver mode: no listening/transcribing in this cycle
            await harvest_pcm_chunk(
                guild=guild,
                channel=channel,
                voice=st.voice,
                do_listen=False,
                idle_delta=idle_delta,
                window_seconds=HARVEST_WINDOW_SECONDS,
            )
            return
        if not utterances:
            return

        transcript_texts = self.bot.translate(
            "cogs.voice_moderation.transcript",
            guild_id=guild.id,
        )

        # Build transcript text for AI in chronological order (segment-level)
        lines: list[str] = []
        author_label = transcript_texts["author_label"]
        utterance_label = transcript_texts["utterance_label"]
        divider = transcript_texts["divider"]
        unknown_speaker = transcript_texts["unknown_speaker"]
        for uid, text, _ts in sorted(utterances, key=lambda x: x[2]):
            # Treat uid==0 as unmapped speaker; avoid showing id 0
            if uid and uid > 0:
                member = await safe_get_member(guild, uid)
                if member is not None:
                    name = member.display_name
                    mention = member.mention
                    author_str = f"{mention} ({name}, id = {uid})"
                else:
                    author_str = transcript_texts["user_fallback"].format(id=uid)
            else:
                author_str = unknown_speaker
            lines.append(
                f"{author_label}: {author_str}\n{utterance_label}: {text}\n{divider}"
            )

        transcript = "\n".join(lines)

        def _chunk_text(s: str, limit: int = 3900) -> list[str]:
            """Split text into chunks <= limit, preferring newline boundaries."""
            if len(s) <= limit:
                return [s]
            parts: list[str] = []
            i = 0
            n = len(s)
            while i < n:
                j = min(i + limit, n)
                if j < n:
                    # try to break at the last newline within the window
                    k = s.rfind("\n", i, j)
                    if k != -1 and k > i:
                        j = k + 1
                parts.append(s[i:j])
                i = j
            return parts

        if transcript_channel_id:
            try:
                # Build a chat-style transcript with real timestamps (end of harvested audio)
                pretty_lines: list[str] = []
                for uid, text, ts in sorted(utterances, key=lambda x: x[2]):
                    # Discord-localized timestamp (short time)
                    unix = int(ts.timestamp())
                    stamp = f"<t:{unix}:t>"
                    if uid and uid > 0:
                        member = await safe_get_member(guild, uid)
                        if member is not None:
                            who = member.display_name
                            who_prefix = member.mention
                        else:
                            who = transcript_texts["user_fallback"].format(id=uid)
                            who_prefix = f"<@{uid}>"
                    else:
                        who = unknown_speaker
                        who_prefix = transcript_texts["unknown_prefix"]
                    pretty_lines.append(
                        transcript_texts["line"].format(
                            timestamp=stamp,
                            prefix=who_prefix,
                            name=who,
                            text=text,
                        )
                    )
                embed_transcript = "\n".join(pretty_lines)

                chunks = _chunk_text(embed_transcript, limit=3900)  # margin under 4096
                total = len(chunks)
                # Post each chunk as its own embed
                transcript_mode = (
                    transcript_texts["footer_high"]
                    if high_quality_transcription
                    else transcript_texts["footer_normal"]
                )

                for idx, part in enumerate(chunks, start=1):
                    title = (
                        transcript_texts["title_single"]
                        if total == 1
                        else transcript_texts["title_part"].format(index=idx, total=total)
                    )
                    embed = discord.Embed(
                        title=title,
                        description=part,
                        colour=discord.Colour.blurple(),
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(
                        name=transcript_texts["field_channel"],
                        value=channel.mention,
                        inline=True,
                    )
                    embed.add_field(
                        name=transcript_texts["field_utterances"],
                        value=str(len(utterances)),
                        inline=True,
                    )
                    embed.set_footer(text=transcript_mode)
                    await mod_logging.log_to_channel(embed, transcript_channel_id, self.bot)

            except Exception as e:
                print(f"[VCMod] failed to post transcript: {e}")
        # Violation history: disable for voice to avoid biasing attribution/actions.
        # Keep the block explicit so the prompt reminds the model not to use history.
        vhist_blob = (
            "Violation history:\nNot provided by policy; do not consider prior violations.\n\n"
        )

        # Run the shared moderation pipeline
        pipeline_started = time.perf_counter()
        try:
            report, total_tokens, request_cost, usage, status = await run_moderation_pipeline_voice(
                guild_id=guild.id,
                api_key=AUTOMOD_OPENAI_KEY,
                system_prompt=VOICE_SYSTEM_PROMPT,
                rules=rules,
                transcript_only=transcript_only,
                violation_history_blob=vhist_blob,
                transcript=transcript,
                base_system_tokens=BASE_SYSTEM_TOKENS,
                default_model=AIMOD_MODEL,
                high_accuracy=high_accuracy,
                text_format=VoiceModerationReport,
                estimate_tokens_fn=am_helpers.estimate_tokens,
            )
        except Exception as e:
            print(f"[VCMod] pipeline failed: {e}")
            duration_ms = int(max((time.perf_counter() - pipeline_started) * 1000, 0))
            await _record_voice_metrics(
                status="exception",
                report_obj=None,
                total_tokens=0,
                request_cost=0.0,
                usage_snapshot={},
                duration_ms=duration_ms,
                error=str(e),
            )
            await asyncio.sleep(idle_delta.total_seconds())
            return
        duration_ms = int(max((time.perf_counter() - pipeline_started) * 1000, 0))
        await _record_voice_metrics(
            status=status,
            report_obj=report,
            total_tokens=total_tokens,
            request_cost=request_cost,
            usage_snapshot=usage,
            duration_ms=duration_ms,
        )

        # No violations
        if not report:
            # budget reached notification (debug only)
            if status == "budget" and aimod_debug and log_channel:
                budget_texts = self.bot.translate(
                    "cogs.voice_moderation.budget",
                    guild_id=guild.id,
                )
                embed = discord.Embed(
                    title=budget_texts["title"],
                    description=budget_texts["description"],
                    colour=discord.Colour.orange(),
                )
                await mod_logging.log_to_channel(embed, log_channel, self.bot)
            await asyncio.sleep(idle_delta.total_seconds())
            return

        if not getattr(report, "violations", None):
            if aimod_debug and log_channel:
                embed = am_helpers.build_no_violations_embed(
                    self.bot, guild, len(utterances), "vc"
                )
                await mod_logging.log_to_channel(embed, log_channel, self.bot)
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Aggregate by user
        aggregated: dict[int, dict] = {}
        for v in report.violations:
            try:
                uid = int(getattr(v, "user_id", 0))
            except Exception:
                continue
            if not uid:
                continue
            actions = list(getattr(v, "actions", []) or [])
            rule = (getattr(v, "rule", "") or "").strip()
            reason = (getattr(v, "reason", "") or "").strip()
            if not actions or not rule:
                continue

            agg = aggregated.setdefault(uid, {"actions": set(), "reasons": [], "rules": set()})
            agg["actions"].update(actions)
            if reason:
                agg["reasons"].append(reason)
            if rule:
                agg["rules"].add(rule)

        # Apply actions
        for uid, data in aggregated.items():
            member = await safe_get_member(guild, uid)
            if not member:
                continue
            all_actions = list(data["actions"]) if data.get("actions") else []
            configured = am_helpers.resolve_configured_actions(
                {"vcmod-detection-action": action_setting}, all_actions, "vcmod-detection-action"
            )

            out_reason, out_rule = am_helpers.summarize_reason_rule(
                self.bot, guild, data.get("reasons"), data.get("rules")
            )

            await am_helpers.apply_actions_and_log(
                bot=self.bot,
                member=member,
                configured_actions=configured,
                reason=out_reason,
                rule=out_rule,
                messages=[],
                aimod_debug=aimod_debug,
                ai_channel_id=log_channel,
                monitor_channel_id=None,
                ai_actions=all_actions,
                fanout=False,
                violation_cache=violation_cache,
            )


async def setup_voice_moderation(bot: commands.Bot):
    await bot.add_cog(VoiceModeratorCog(bot))
