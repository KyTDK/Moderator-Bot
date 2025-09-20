import json
import os
import time
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from cryptography.fernet import Fernet, InvalidToken

from modules.utils import mysql

CAPTCHA_API_URL = "https://modbot.moderatorbot.com/api/accelerated/captcha?gid="
CAPTCHA_DEFAULT_VERIFY = {
    "hcaptcha": "https://hcaptcha.com/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
}

class CaptchaStartView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Solve captcha", url=url))


class CaptchaCog(commands.Cog):
    """A cog for captcha verification."""

    captcha = app_commands.Group(
        name="captcha", description="Captcha verification commands"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._api_token = os.getenv("CAPTCHA_API_TOKEN")
        self._verify_secret = os.getenv("CAPTCHA_VERIFY_SECRET")
        challenge_base = os.getenv(
            "CAPTCHA_CHALLENGE_URL", "https://example.com/captcha"
        )
        self._challenge_base = challenge_base.rstrip("/")
        state_key = os.getenv("CAPTCHA_STATE_KEY")
        if not state_key:
            raise RuntimeError("CAPTCHA_STATE_KEY environment variable is required.")
        self._state_signer = Fernet(state_key.encode())
        self._pending: Dict[Tuple[int, int], Dict[str, Any]] = {}

    @property
    def session(self):
        session = getattr(self.bot, "session", None)
        if session is None:
            raise RuntimeError(
                "The bot is missing an aiohttp.ClientSession on bot.session."
            )
        return session

    async def fetch_captcha_config(
        self, guild_id: int
    ) -> Optional[Dict[str, Any]]:
        headers: Dict[str, str] = {}
        if self._api_token:
            headers["Authorization"] = f"Bot {self._api_token}"

        async with self.session.get(
            f"{CAPTCHA_API_URL}{guild_id}", headers=headers
        ) as resp:
            if resp.status != 200:
                print(
                    f"[captcha] Failed to fetch config for guild {guild_id}: HTTP {resp.status}"
                )
                return None
            payload = await resp.json()
        return payload

    def _resolve_verify_url(self, captcha: Dict[str, Any]) -> Optional[str]:
        verify_url = captcha.get("verificationEndpoint")
        if verify_url:
            return verify_url
        provider = (captcha.get("provider") or "").lower()
        return CAPTCHA_DEFAULT_VERIFY.get(provider)

    def _create_state(self, guild_id: int, member_id: int) -> str:
        payload = json.dumps(
            {"g": str(guild_id), "u": str(member_id), "ts": int(time.time())}
        ).encode("utf-8")
        return self._state_signer.encrypt(payload).decode("utf-8")

    def _decode_state(self, state: str) -> Optional[Dict[str, Any]]:
        try:
            payload = self._state_signer.decrypt(state.encode("utf-8"), ttl=3600)
        except InvalidToken:
            return None
        return json.loads(payload.decode("utf-8"))

    def _build_challenge_url(self, state: str) -> str:
        return f"{self._challenge_base}?state={state}"

    def _parse_roles(self, value: Any) -> List[int]:
        if not value:
            return []
        if isinstance(value, str):
            candidates = [chunk.strip() for chunk in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            return []
        role_ids: List[int] = []
        for candidate in candidates:
            try:
                role_ids.append(int(candidate))
            except (TypeError, ValueError):
                continue
        return role_ids

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        settings = await mysql.get_settings(
            member.guild.id,
            [
                "captcha-verification-enabled",
                "captcha-success-roles",
                "captcha-verification-channel",
            ],
        )
        if not settings.get("captcha-verification-enabled"):
            return

        payload = await self.fetch_captcha_config(member.guild.id)
        if not payload or not payload.get("allowed"):
            return

        captcha = payload.get("captcha") or {}
        site_key = captcha.get("siteKey")
        if not site_key:
            print(
                f"[captcha] Guild {member.guild.id} is missing a captcha site key."
            )
            return

        verify_url = self._resolve_verify_url(captcha)
        if not verify_url:
            print(
                f"[captcha] Guild {member.guild.id} has no verification endpoint configured."
            )
            return

        state = self._create_state(member.guild.id, member.id)
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        roles = self._parse_roles(settings.get("captcha-success-roles"))

        self._pending[(member.guild.id, member.id)] = {
            "state_hash": state_hash,
            "verify_url": verify_url,
            "roles": roles,
            "provider": captcha.get("provider") or "hcaptcha",
            "site_key": site_key,
        }

        embed = discord.Embed(
            title="Captcha verification required",
            description=(
                "Complete the captcha challenge to unlock the server.\n"
                "1. Press **Solve captcha**.\n"
                "2. Finish the challenge on the web page (state and site key are below).\n"
                "3. Run `/captcha submit` in the server with the token and state shown on the page."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Provider",
            value=self._pending[(member.guild.id, member.id)]["provider"],
            inline=True,
        )
        embed.add_field(name="Site key", value=site_key, inline=True)
        embed.add_field(name="State", value=state, inline=False)
        embed.set_footer(text="The state expires in one hour.")

        view = CaptchaStartView(self._build_challenge_url(state))

        try:
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            channel = None
            channel_id = settings.get("captcha-verification-channel")
            if channel_id:
                try:
                    channel = member.guild.get_channel(int(channel_id))
                except (TypeError, ValueError):
                    channel = None
            if channel:
                await channel.send(member.mention, embed=embed, view=view)
            else:
                print(
                    f"[captcha] Unable to notify {member.id} about captcha requirements."
                )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        self._pending.pop((member.guild.id, member.id), None)

    @captcha.command(
        name="submit",
        description="Submit the captcha response token from the verification page.",
    )
    @app_commands.describe(
        token="The captcha response token returned after solving the challenge.",
        state="The state string provided alongside the captcha link.",
    )
    @app_commands.guild_only()
    async def captcha_submit(self, interaction: Interaction, token: str, state: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        key = (guild.id, interaction.user.id)
        challenge = self._pending.get(key)
        if not challenge:
            await interaction.response.send_message(
                "I can't find an active captcha challenge for you. Please rejoin or ask a moderator.",
                ephemeral=True,
            )
            return

        decoded = self._decode_state(state)
        if not decoded:
            await interaction.response.send_message(
                "That state is invalid or has expired. Please request a new captcha.",
                ephemeral=True,
            )
            return

        if int(decoded.get("g", 0)) != guild.id or int(decoded.get("u", 0)) != interaction.user.id:
            await interaction.response.send_message(
                "That state was not issued for this server/member.", ephemeral=True
            )
            return

        if hashlib.sha256(state.encode("utf-8")).hexdigest() != challenge["state_hash"]:
            await interaction.response.send_message(
                "The provided state does not match the current challenge. Please restart the captcha flow.",
                ephemeral=True,
            )
            return

        if not self._verify_secret:
            await interaction.response.send_message(
                "Captcha verification secret is not configured on the bot.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        success, raw_response = await self._verify_with_provider(
            challenge["verify_url"], token
        )
        if not success:
            details = raw_response.get("error-codes") if isinstance(raw_response, dict) else raw_response
            await interaction.followup.send(
                f"Captcha verification failed. Details: {details}",
                ephemeral=True,
            )
            return

        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if member is None:
            await interaction.followup.send(
                "Could not resolve your member profile to grant roles.",
                ephemeral=True,
            )
            return

        granted: List[str] = []
        for role_id in challenge["roles"]:
            role = guild.get_role(role_id)
            if not role:
                continue
            try:
                await member.add_roles(role, reason="Captcha completed")
                granted.append(role.mention)
            except discord.Forbidden:
                print(
                    f"[captcha] Missing permissions to add role {role_id} in guild {guild.id}"
                )

        self._pending.pop(key, None)

        success_message = "Captcha solved successfully."
        if granted:
            success_message += f" Granted roles: {' '.join(granted)}"
        await interaction.followup.send(success_message, ephemeral=True)

    async def _verify_with_provider(self, verify_url: str, token: str) -> Tuple[bool, Any]:
        data = {"secret": self._verify_secret, "response": token}
        try:
            async with self.session.post(verify_url, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return False, {"status": resp.status, "body": body}
                if "application/json" in resp.headers.get("Content-Type", ""):
                    payload = await resp.json()
                else:
                    payload = await resp.text()
        except Exception as exc:
            return False, str(exc)

        if isinstance(payload, dict):
            return bool(payload.get("success")), payload
        return False, payload

    def cog_unload(self):
        try:
            self.bot.tree.remove_command(self.captcha.name)
        except Exception:
            pass
        self._pending.clear()


async def setup(bot: commands.Bot):
    cog = CaptchaCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.captcha)
