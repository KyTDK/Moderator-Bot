import asyncio
import io
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from dotenv import load_dotenv

from modules.utils import mysql, mod_logging
from modules.utils.discord_utils import safe_get_member
from modules.utils.time import parse_duration

from cogs.autonomous_moderation import helpers as am_helpers
from cogs.voice_moderation.models import VoiceModerationReport
from cogs.voice_moderation.prompt import VOICE_SYSTEM_PROMPT, BASE_SYSTEM_TOKENS
from modules.ai.mod_utils import get_model_limit, pick_model, budget_allows
from modules.ai.mod_utils import get_price_per_mtok  # re-exported for callers that rely on cost calc
from modules.ai.engine import run_parsed_ai

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - import fallback
    AsyncOpenAI = None  # type: ignore


# Optional sinks support (Pycord or discord-ext-sinks)
SINKS_MODULE = None
try:  # Pycord-style
    import discord.sinks as _sinks  # type: ignore

    SINKS_MODULE = _sinks
except Exception:
    try:  # discord-ext-sinks package
        from discord.ext import sinks as _ext_sinks  # type: ignore

        SINKS_MODULE = _ext_sinks
    except Exception:
        SINKS_MODULE = None


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
                "vcmod-detection-action",
                "aimod-debug",
                "aimod-channel",
                "monitor-channel",
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
                if not isinstance(fetched, discord.VoiceChannel):
                    # skip if not a VC
                    st.index = (st.index + 1) % len(channel_ids)
                    st.next_start = now + timedelta(seconds=10)
                    return
                channel = fetched
            except Exception:
                st.index = (st.index + 1) % len(channel_ids)
                st.next_start = now + timedelta(seconds=10)
                return

        # Decide durations
        listen_delta = parse_duration(listen_str) or timedelta(minutes=2)
        idle_delta = parse_duration(idle_str) or timedelta(seconds=30)

        do_listen = (not saver_mode) and (SINKS_MODULE is not None)

        # Start a run for this guild
        st.busy_task = asyncio.create_task(
            self._run_cycle_for_channel(
                guild=guild,
                channel=channel,
                do_listen=do_listen,
                listen_delta=listen_delta,
                idle_delta=idle_delta,
                high_accuracy=settings.get("vcmod-high-accuracy") or False,
                rules=settings.get("vcmod-rules") or "",
                action_setting=settings.get("vcmod-detection-action") or ["auto"],
                aimod_debug=settings.get("aimod-debug") or False,
                log_channel=settings.get("aimod-channel") or settings.get("monitor-channel"),
            )
        )

        # Schedule next start immediately after this task completes
        def _done_callback(_):
            try:
                st.index = (st.index + 1) % len(channel_ids) if len(channel_ids) > 1 else st.index
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
        rules: str,
        action_setting: list[str],
        aimod_debug: bool,
        log_channel: Optional[int],
    ):
        # Join/move voice
        st = self._get_state(guild.id)
        try:
            if st.voice and st.voice.is_connected():
                try:
                    await st.voice.move_to(channel)
                except Exception:
                    await st.voice.disconnect(force=True)
                    st.voice = await channel.connect(self_deaf=False, self_mute=True)
            else:
                st.voice = await channel.connect(self_deaf=False, self_mute=True)
        except Exception as e:
            print(f"[VCMod] failed to connect/move in {guild.id} → {channel.id}: {e}")
            await asyncio.sleep(5)
            return

        # Presence-only saver mode or sinks unavailable → wait idle and return
        if not do_listen:
            await asyncio.sleep(idle_delta.total_seconds())
            return

        if AsyncOpenAI is None:
            print("[VCMod] openai client not available; skipping listen")
            await asyncio.sleep(idle_delta.total_seconds())
            return

        if SINKS_MODULE is None:
            print("[VCMod] sinks not available; cannot record audio")
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Record for the listen duration
        record_done = asyncio.Event()
        sink = getattr(SINKS_MODULE, "WaveSink", None)
        if sink is None:
            sink = getattr(SINKS_MODULE, "WAVSink", None)
        if sink is None:
            # Last resort: MP3
            sink = getattr(SINKS_MODULE, "MP3Sink", None)

        if sink is None:
            print("[VCMod] no compatible sink found (need WaveSink/MP3Sink)")
            await asyncio.sleep(idle_delta.total_seconds())
            return

        active_sink = sink()

        def _finished_cb(rec_sink, *_):
            try:
                record_done.set()
            except Exception:
                pass

        try:
            st.voice.start_recording(active_sink, _finished_cb)
        except Exception as e:
            print(f"[VCMod] start_recording failed: {e}")
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Wait for duration then stop
        await asyncio.sleep(listen_delta.total_seconds())
        try:
            st.voice.stop_recording()
        except Exception:
            pass
        # Wait for sink to flush
        try:
            await asyncio.wait_for(record_done.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

        # Collect per-user audio bytes
        audio_map: dict[int, bytes] = {}
        try:
            data = getattr(active_sink, "audio_data", {}) or {}
            for user, udata in data.items():
                uid = int(getattr(user, "id", user))
                # Try common attributes across sinks
                content: Optional[bytes] = None
                for attr in ("file", "data", "audio", "buffer"):
                    if hasattr(udata, attr):
                        obj = getattr(udata, attr)
                        if isinstance(obj, (bytes, bytearray)):
                            content = bytes(obj)
                            break
                        if hasattr(obj, "read"):
                            try:
                                content = obj.read()
                                break
                            except Exception:
                                pass
                if content:
                    audio_map[uid] = content
        except Exception as e:
            print(f"[VCMod] failed to extract audio: {e}")

        # Transcribe with Whisper API
        client = AsyncOpenAI(api_key=AUTOMOD_OPENAI_KEY)
        utterances: list[tuple[int, str]] = []  # (user_id, text)
        for uid, audio_bytes in audio_map.items():
            try:
                fobj = io.BytesIO(audio_bytes)
                fobj.name = "audio.wav"  # hint for encoder
                tr = await client.audio.transcriptions.create(model="whisper-1", file=fobj)
                text = getattr(tr, "text", None) or ""
                if text.strip():
                    utterances.append((uid, text.strip()))
            except Exception as e:
                print(f"[VCMod] transcription failed for {uid}: {e}")

        # If nothing to analyze, idle dwell
        if not utterances:
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Build transcript text and token estimate
        lines: list[str] = []
        for uid, text in utterances:
            member = await safe_get_member(guild, uid)
            name = member.display_name if member else str(uid)
            lines.append(f"AUTHOR: {name} (id = {uid})\nUTTERANCE: {text}\n---")

        transcript = "\n".join(lines)
        # Violation history for context (shared helper)
        user_ids = {uid for uid, _ in utterances}
        vhist_blob = am_helpers.build_violation_history_for_users(user_ids, violation_cache)

        high_model = pick_model(high_accuracy, AIMOD_MODEL)
        limit = get_model_limit(high_model)
        max_tokens = int(limit * 0.9)
        current_tokens = BASE_SYSTEM_TOKENS + am_helpers.estimate_tokens(vhist_blob) + am_helpers.estimate_tokens(rules)
        total_tokens = current_tokens + am_helpers.estimate_tokens(transcript)

        if total_tokens >= max_tokens:
            # Skip this cycle if too large
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Budget check
        allow, request_cost, usage = await budget_allows(guild.id, high_model, total_tokens)
        if not allow:
            if aimod_debug and log_channel:
                embed = discord.Embed(
                    title="VC Moderation Budget Reached",
                    description="Skipping analysis for this cycle due to budget limit.",
                    colour=discord.Colour.orange(),
                )
                await mod_logging.log_to_channel(embed, log_channel, self.bot)
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Call AI moderation
        user_prompt = f"Rules:\n{rules}\n\n{vhist_blob}Transcript:\n{transcript}"
        try:
            report: VoiceModerationReport | None = await run_parsed_ai(
                api_key=AUTOMOD_OPENAI_KEY,
                model=high_model,
                system_prompt=VOICE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                text_format=VoiceModerationReport,
            )
        except Exception as e:
            print(f"[VCMod] AI analysis failed: {e}")
            await asyncio.sleep(idle_delta.total_seconds())
            return

        # Record usage
        try:
            await mysql.add_aimod_usage(guild.id, total_tokens, request_cost)
        except Exception as e:
            print(f"[VCMod] failed to record usage: {e}")

        # No violations
        if not report or not getattr(report, "violations", None):
            if aimod_debug and log_channel:
                embed = am_helpers.build_no_violations_embed(len(utterances), "vc")
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

            reasons = data.get("reasons") or []
            if not reasons:
                out_reason = "Violation detected"
            elif len(reasons) == 1:
                out_reason = reasons[0]
            else:
                out_reason = "Multiple violations: " + "; ".join(reasons)

            rules_set = list(data.get("rules") or [])
            if not rules_set:
                out_rule = "Rule violation"
            elif len(rules_set) == 1:
                out_rule = rules_set[0]
            else:
                out_rule = "Multiple rules: " + ", ".join(rules_set)

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
