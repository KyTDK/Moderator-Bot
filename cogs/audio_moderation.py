import asyncio
import wave
import discord
from discord.ext import commands, tasks, voice_recv
import openai
import os
from collections import defaultdict
import discord.opus
from modules.utils import mysql

ALLOWED_GUILD_ID = 985715695532773420

class ModerationSink(voice_recv.AudioSink):
    def __init__(self):
        super().__init__()
        self.buffers = defaultdict(list)

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        self.buffers[user.id].append(data.pcm)

    def get_audio(self, user_id: int):
        chunks = self.buffers.pop(user_id, [])
        return b''.join(chunks) if chunks else None
    
    def cleanup(self):
        self.buffers.clear()
        print("[ModerationSink] Cleared all audio buffers.")

class AudioModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_clients = {}
        self.monitor_audio.start()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot or member.guild.id != ALLOWED_GUILD_ID:
            return

        if before.channel is None and after.channel is not None:
            gid = member.guild.id

            if not self.bot.is_ready():
                print("[AudioModeration] Skipping voice join — bot not ready yet.")
                return

            if gid in self.voice_clients:
                return


            # Check required settings
            settings = await mysql.get_settings(gid, [
                "autonomous-mod",
                "api-key",
            ])
            autonomous = settings.get("autonomous-mod")
            api_key = settings.get("api-key")
            if not (autonomous and api_key):
                return            

            if not discord.opus.is_loaded():
                try:
                    discord.opus.load_opus("libopus.so")
                except Exception as e:
                    print(f"[DEBUG] Failed to load Opus: {e}")

            if after.channel is None:
                print("[AudioModeration] Skipping connection — after.channel is None.")
                return

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot or member.guild.id != ALLOWED_GUILD_ID:
            return

        if before.channel is None and after.channel is not None:
            gid = member.guild.id

            if not self.bot.is_ready():
                return

            existing = self.voice_clients.get(gid)
            if existing:
                vc, _, _ = existing
                if vc.is_connected():
                    return
                else:
                    try:
                        await vc.disconnect(force=True)
                    except Exception as e:
                        print(f"[AudioModeration] Error during stale disconnect: {e}")
                    self.voice_clients.pop(gid, None)

            api_key = await mysql.get_settings(gid, "api-key")
            if not api_key:
                return

            if not discord.opus.is_loaded():
                try:
                    discord.opus.load_opus("libopus.so")
                except Exception as e:
                    print(f"[DEBUG] Failed to load Opus: {e}")

            if after.channel is None:
                return

            for attempt in range(3):
                try:
                    vc = await after.channel.connect(cls=voice_recv.VoiceRecvClient)

                    sink = ModerationSink()
                    vc.listen(sink)

                    break
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(2)

    @tasks.loop(seconds=10)
    async def monitor_audio(self):
        for gid, (vc, sink, api_key) in list(self.voice_clients.items()):
            client = openai.AsyncOpenAI(api_key=api_key)

            for user_id in list(sink.buffers.keys()):
                data = sink.get_audio(user_id)
                if not data:
                    continue

                if len(data) < 96000:  # less than 1 second of audio
                    print(f"[AudioModeration] Skipping short/empty audio for user {user_id}. Length: {len(data)} bytes")
                    continue

                filename = f"/tmp/audio_{gid}_{user_id}.wav"
                try:
                    with wave.open(filename, "wb") as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(48000)
                        wav_file.writeframes(data)

                    with open(filename, "rb") as file:
                        result = await client.audio.transcriptions.create(
                            model="whisper-1",
                            file=file
                        )
                    print(f"[Transcription] {user_id}: {result.text}")
                except Exception as e:
                    print(f"[AudioModeration] Transcription error: {e}")
                finally:
                    if os.path.exists(filename):
                        os.remove(filename)
                        print(f"[AudioModeration] Deleted temporary file: {filename}")

    @monitor_audio.before_loop
    async def before_loop(self):
        print("[AudioModeration] Waiting for bot to be ready before starting monitor loop...")
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(AudioModerationCog(bot))