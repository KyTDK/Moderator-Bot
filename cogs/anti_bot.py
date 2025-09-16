import json
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from typing import Optional, List

from modules.utils import mysql
from modules.utils.mod_logging import log_to_channel
from modules.utils.discord_utils import safe_get_member, safe_get_user, ensure_member_with_presence
from modules.antibot.scoring import evaluate_member
from modules.antibot.embeds import build_inspection_embed, build_join_embed
from modules.antibot.conditions import (
    Condition,
    evaluate_conditions,
    list_signals,
    make_condition,
    get_signal,
    format_actual,
    format_expected,
)


class AntiBotCog(commands.Cog):
    """Anti-bot utilities: inspect users and (optionally) auto-act on joins."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Slash commands ----------
    antibot = app_commands.Group(
        name="antibot",
        description="Anti-bot tools and inspection",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    conditions_group = app_commands.Group(
        name="conditions",
        description="Manage AntiBot inspection conditions",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )
    antibot.add_command(conditions_group)

    # ----- Helpers -----
    @staticmethod
    def _load_conditions(raw: Optional[List[dict]]) -> List[Condition]:
        conditions: List[Condition] = []
        if not raw:
            return conditions
        for entry in raw:
            try:
                conditions.append(Condition.from_dict(entry))
            except Exception:
                continue
        return conditions

    async def _save_conditions(self, guild_id: int, conditions: List[Condition]):
        payload = [cond.to_dict() for cond in conditions]
        await mysql.update_settings(guild_id, "antibot-conditions", payload)

    async def _condition_signal_autocomplete(self, interaction: Interaction, current: str):
        options = []
        query = (current or "").lower()
        for signal in list_signals():
            if query and query not in signal.key.lower() and query not in signal.name.lower():
                continue
            options.append(app_commands.Choice(name=f"{signal.name} ({signal.key})"[:100], value=signal.key[:100]))
            if len(options) >= 25:
                break
        return options

    async def _condition_operator_autocomplete(self, interaction: Interaction, current: str):
        signal_key = getattr(interaction.namespace, "signal", None)
        signal = get_signal(signal_key) if signal_key else None
        if not signal:
            return []
        query = (current or "").lower()
        if signal.value_type == "boolean":
            bool_options = [("True", "true"), ("False", "false")]
            return [
                app_commands.Choice(name=name, value=value)
                for name, value in bool_options
                if not query or query in name.lower() or query in value.lower()
            ][:25]
        return [
            app_commands.Choice(name=op, value=op)
            for op in signal.operators
            if query in op.lower()
        ][:25]

    async def _condition_remove_autocomplete(self, interaction: Interaction, current: str):
        raw = await mysql.get_settings(interaction.guild.id, "antibot-conditions") or []
        conditions = self._load_conditions(raw)
        query = (current or "").lower()
        results = []
        for idx, cond in enumerate(conditions):
            signal_meta = get_signal(cond.signal)
            label = cond.label or (signal_meta.name if signal_meta else cond.signal)
            expected = format_expected(cond)
            display = f"{idx + 1}. {label} {cond.operator} {expected}"
            if query and query not in label.lower() and query not in cond.signal.lower() and query not in expected.lower():
                continue
            results.append(app_commands.Choice(name=display[:100], value=str(idx)))
            if len(results) >= 25:
                break
        return results

    # ----- Conditions commands -----
    @conditions_group.command(name="add", description="Add a new AntiBot condition")
    @app_commands.autocomplete(signal=_condition_signal_autocomplete, operator=_condition_operator_autocomplete)
    @app_commands.describe(signal="Signal to check", operator="Comparator", value="Target value", label="Optional label")
    async def condition_add(self, interaction: Interaction, signal: str, operator: str, value: Optional[str] = None, label: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        signal_meta = get_signal(signal)
        if not signal_meta:
            await interaction.followup.send("Unknown signal.", ephemeral=True)
            return

        operator_input = operator.strip() if operator else ""
        value_input = value

        if signal_meta.value_type == "boolean":
            if operator_input.lower() in {"true", "false"} and not value_input:
                value_input = operator_input
                operator_input = "=="
            if not operator_input:
                operator_input = "=="
            if not value_input:
                value_input = "true"
        if operator_input not in signal_meta.operators:
            await interaction.followup.send(
                f"Operator must be one of {', '.join(signal_meta.operators)}.",
                ephemeral=True,
            )
            return
        try:
            condition = make_condition(signal_meta, operator_input, value_input, label)
        except ValueError as err:
            await interaction.followup.send(str(err), ephemeral=True)
            return

        raw = await mysql.get_settings(interaction.guild.id, "antibot-conditions") or []
        conditions = self._load_conditions(raw)
        conditions.append(condition)
        await self._save_conditions(interaction.guild.id, conditions)

        await interaction.followup.send(
            f"Added condition: {signal_meta.name} {condition.operator} {format_expected(condition)}.",
            ephemeral=True,
        )

    @conditions_group.command(name="remove", description="Remove an existing condition")
    @app_commands.autocomplete(condition_id=_condition_remove_autocomplete)
    async def condition_remove(self, interaction: Interaction, condition_id: str):
        await interaction.response.defer(ephemeral=True)
        raw = await mysql.get_settings(interaction.guild.id, "antibot-conditions") or []
        conditions = self._load_conditions(raw)
        try:
            index = int(condition_id)
        except (TypeError, ValueError):
            await interaction.followup.send("Select a condition from the suggestions.", ephemeral=True)
            return
        if index < 0 or index >= len(conditions):
            await interaction.followup.send("Condition not found.", ephemeral=True)
            return

        removed = conditions.pop(index)
        await self._save_conditions(interaction.guild.id, conditions)

        signal_meta = get_signal(removed.signal)
        name = removed.label or (signal_meta.name if signal_meta else removed.signal)
        expected = format_expected(removed)
        await interaction.followup.send(
            f"Removed condition: {name} {removed.operator} {expected}.",
            ephemeral=True,
        )

    @conditions_group.command(name="list", description="List configured AntiBot conditions")
    async def condition_list(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = await mysql.get_settings(interaction.guild.id, "antibot-conditions") or []
        conditions = self._load_conditions(raw)
        if not conditions:
            await interaction.followup.send("No conditions configured. Use `/antibot conditions add`.", ephemeral=True)
            return
        embed = discord.Embed(title="Configured AntiBot Conditions", color=discord.Color.blurple())
        for cond in conditions:
            signal = get_signal(cond.signal)
            name = cond.label or (signal.name if signal else cond.signal)
            expected = format_expected(cond)
            embed.add_field(
                name=f"{cond.id}: {name}",
                value=f"Signal `{cond.signal}` {cond.operator} `{expected}`",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @conditions_group.command(name="clear", description="Remove all AntiBot conditions")
    async def condition_clear(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._save_conditions(interaction.guild.id, [])
        await interaction.followup.send("Cleared all AntiBot conditions.", ephemeral=True)

    # ----- Inspect command -----
    @antibot.command(name="inspect", description="Run AntiBot checks using configured conditions")
    @app_commands.describe(user="Select a user to inspect (or provide ID)", user_id="Optional user ID if not selectable")
    async def inspect(self, interaction: Interaction, user: Optional[discord.User] = None, user_id: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        raw_conditions = await mysql.get_settings(guild.id, "antibot-conditions") or []
        conditions = self._load_conditions(raw_conditions)
        if not conditions:
            await interaction.followup.send(
                "No AntiBot conditions configured. Use `/antibot conditions add` to create rules.",
                ephemeral=True,
            )
            return

        target_member: Optional[discord.Member] = None
        if user is not None:
            target_member = await safe_get_member(guild, user.id, force_fetch=True)
        elif user_id and user_id.isdigit():
            target_member = await safe_get_member(guild, int(user_id), force_fetch=True)

        if target_member is None:
            await interaction.followup.send("Could not resolve that user as a member of this server.", ephemeral=True)
            return

        try:
            enriched = await ensure_member_with_presence(guild, target_member.id)
            if enriched:
                target_member = enriched
        except Exception:
            pass

        try:
            fetched_member = await safe_get_member(guild, target_member.id, force_fetch=True)
            if fetched_member is not None:
                target_member = fetched_member
        except Exception:
            pass

        try:
            full_user = await safe_get_user(self.bot, target_member.id, force_fetch=True)
            if full_user:
                target_member._user = full_user
        except Exception:
            pass

        score, details = evaluate_member(target_member, bot=self.bot)
        results = evaluate_conditions(conditions, target_member, details)
        passed = sum(1 for r in results if r.passed)
        color = discord.Color.green() if passed == len(results) else discord.Color.orange() if passed else discord.Color.red()

        embed = discord.Embed(
            title=f"AntiBot Condition Check: {target_member}",
            description=f"Conditions satisfied: **{passed}/{len(results)}**",
            color=color,
        )
        embed.add_field(name="Score", value=f"`{score}` / 100", inline=False)

        for res in results:
            cond = res.condition
            signal_meta = get_signal(cond.signal)
            label = cond.label or (signal_meta.name if signal_meta else cond.signal)
            expected = format_expected(cond)
            actual = format_actual(cond.signal, res.actual_value)
            status = "✅" if res.passed else "❌"
            embed.add_field(
                name=f"{status} {label}",
                value=f"`{cond.signal}` {cond.operator} `{expected}`\nActual: `{actual}`",
                inline=False,
            )

        embed.set_footer(text="Detailed view available via /debug inspect.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    @antibot.command(name="debug", description="Inspect a user's signals and compute a trust score")
    @app_commands.describe(user="Select a user to inspect (or provide ID)", user_id="Optional user ID if not selectable")
    async def debug(self, interaction: Interaction, user: Optional[discord.User] = None, user_id: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        # Resolve the member within this guild
        target_member: Optional[discord.Member] = None
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        # Prefer an explicit chooser, then ID fallback
        if user is not None:
            target_member = await safe_get_member(guild, user.id, force_fetch=True)
        elif user_id and user_id.isdigit():
            target_member = await safe_get_member(guild, int(user_id), force_fetch=True)

        if target_member is None:
            await interaction.followup.send("Could not resolve that user as a member of this server.", ephemeral=True)
            return

        # Try to enhance the Member with presence/activities for better signals
        try:
            enriched = await ensure_member_with_presence(guild, target_member.id)
            if enriched is not None:
                target_member = enriched
        except Exception:
            pass

        try:
            fetched_member = await safe_get_member(guild, target_member.id, force_fetch=True)
            if fetched_member is not None:
                target_member = fetched_member
        except Exception:
            pass

        # Ensure we have a fully populated user for banner/accent
        try:
            full_user = await safe_get_user(self.bot, target_member.id, force_fetch=True)
            if full_user:
                # overwrite banner/accent fields if available
                target_member._user = full_user
        except Exception:
            pass

        score, details = evaluate_member(target_member, bot=self.bot)

        emb = build_inspection_embed(target_member, score, details)
        await interaction.followup.send(embed=emb, ephemeral=True)


    async def _hydrate_member_for_scoring(self, guild: discord.Guild, member: discord.Member) -> discord.Member:
        try:
            enriched = await ensure_member_with_presence(guild, member.id)
            if enriched is not None:
                member = enriched
        except Exception:
            pass

        try:
            fetched_member = await safe_get_member(guild, member.id, force_fetch=True)
            if fetched_member is not None:
                member = fetched_member
        except Exception:
            pass

        try:
            full_user = await safe_get_user(self.bot, member.id, force_fetch=True)
            if full_user:
                member._user = full_user
        except Exception:
            pass

        return member

    def _resolve_antibot_thresholds(self, settings: dict[str, object]) -> tuple[int, int]:
        raw_join_min = settings.get("antibot-min-pass")
        if raw_join_min is None:
            legacy_join_min = settings.get("antibot-min-score", 0)
            raw_join_min = 1 if legacy_join_min and int(legacy_join_min or 0) > 0 else 0
        join_required = int(raw_join_min or 0)

        raw_autorole_min = settings.get("antibot-autorole-min-pass")
        if raw_autorole_min is None:
            legacy_auto_min = settings.get("antibot-autorole-min-score", 0)
            raw_autorole_min = 1 if legacy_auto_min and int(legacy_auto_min or 0) > 0 else 0
        autorole_required = int(raw_autorole_min or 0)

        return join_required, autorole_required

    def _evaluate_join_conditions(self, conditions, member, details, join_required: int, autorole_required: int):
        condition_results = evaluate_conditions(conditions, member, details) if conditions else []
        conditions_passed = sum(1 for result in condition_results if result.passed)
        total_conditions = len(conditions)

        if total_conditions == 0:
            join_required = 0
            autorole_required = 0

        join_required = max(join_required, 0)
        autorole_required = max(autorole_required, 0)

        join_required_count = min(join_required, total_conditions) if total_conditions and join_required else 0
        autorole_required_count = min(autorole_required, total_conditions) if total_conditions and autorole_required else 0

        join_failure = join_required_count and conditions_passed < join_required_count
        autorole_failure = False
        if total_conditions and autorole_required_count:
            autorole_failure = conditions_passed < autorole_required_count
        elif autorole_required and not total_conditions:
            autorole_failure = True

        return {
            "results": condition_results,
            "passed": conditions_passed,
            "total": total_conditions,
            "join_required_count": join_required_count,
            "join_failure": join_failure,
            "autorole_required_count": autorole_required_count,
            "autorole_failure": autorole_failure,
        }

    @staticmethod
    def _should_kick(enabled: bool, failure: bool, total_conditions: int, required_count: int, conditions_passed: int) -> tuple[bool, str | None]:
        if not enabled or not required_count or not total_conditions:
            return False, None
        if not failure:
            return False, None
        return True, f"conditions {conditions_passed}/{required_count}"

    # ---------- Join hook ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            settings = await mysql.get_settings(member.guild.id, [
                "antibot-enabled",
                "antibot-min-pass",
                "antibot-min-score",
                "antibot-autorole",
                "antibot-autorole-min-pass",
                "antibot-autorole-min-score",
                "monitor-channel",
                "antibot-conditions",
            ])
            enabled = bool(settings.get("antibot-enabled", False))
            join_required, autorole_required = self._resolve_antibot_thresholds(settings)
            autorole_id = settings.get("antibot-autorole")
            monitor_channel_id = settings.get("monitor-channel")
            raw_conditions = settings.get("antibot-conditions") or []
            join_conditions = self._load_conditions(raw_conditions)

            member = await self._hydrate_member_for_scoring(member.guild, member)

            score, details = evaluate_member(member, bot=self.bot)
            condition_state = self._evaluate_join_conditions(join_conditions, member, details, join_required, autorole_required)
            details['conditions_total'] = condition_state['total']
            details['conditions_passed'] = condition_state['passed']

            if monitor_channel_id:
                emb = build_join_embed(member, score, details)
                await log_to_channel(emb, int(monitor_channel_id), self.bot)

            if autorole_id and not condition_state['autorole_failure'] and not member.pending:
                role = member.guild.get_role(int(autorole_id))
                if role:
                    try:
                        await member.add_roles(role, reason="Auto role via AntiBot (conditions met)")
                    except (discord.Forbidden, discord.HTTPException):
                        if monitor_channel_id:
                            warn = discord.Embed(
                                title="AntiBot: Auto-role failed",
                                description=(
                                    f"Could not assign {role.mention} to {member.mention}.\n"
                                    "Check the bot role is above the target role and has Manage Roles."
                                ),
                                color=discord.Color.orange(),
                            )
                            try:
                                await log_to_channel(warn, int(monitor_channel_id), self.bot)
                            except Exception:
                                pass

            should_kick, failure_reason = self._should_kick(
                enabled,
                condition_state['join_failure'],
                condition_state['total'],
                condition_state['join_required_count'],
                condition_state['passed'],
            )

            if should_kick:
                try:
                    detail = failure_reason or f"conditions {condition_state['passed']}/{condition_state['join_required_count']}"
                    await member.kick(reason=f"Auto-kicked: {detail}")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        except Exception:
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiBotCog(bot))


