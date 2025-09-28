from typing import Optional
from discord.ext import commands
from discord import app_commands, Interaction
import discord
from modules.utils import mysql
from modules.config.settings_schema import SETTINGS_SCHEMA
from modules.config.premium_plans import describe_plan_requirements
import traceback
from modules.variables.TimeString import TimeString

MAX_CHARS = 1900  # Leave buffer for formatting
CHUNK_SEPARATOR = "\n"

def paginate(text, limit=MAX_CHARS):
    chunks = []
    lines = text.split(CHUNK_SEPARATOR)
    current = ""

    for line in lines:
        if len(current) + len(line) + len(CHUNK_SEPARATOR) > limit:
            chunks.append(current)
            current = ""
        current += line + CHUNK_SEPARATOR
    if current:
        chunks.append(current)
    return chunks

class Settings(commands.Cog):
    """A cog for settings commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def value_autocomplete(this, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        name_option = getattr(interaction.namespace, "name", None)
        if not name_option:
            return []
        schema = SETTINGS_SCHEMA.get(name_option)
        if not schema or not schema.choices:
            return []
        return [
            app_commands.Choice(name=choice[:100], value=choice[:100])
            for choice in schema.choices if current.lower() in choice.lower()
        ][:25]

    # Autocomplete helpers
    async def name_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        query = (current or "").lower()
        results: list[app_commands.Choice[str]] = []
        for setting_name, setting in SETTINGS_SCHEMA.items():
            if getattr(setting, "hidden", False):
                continue
            if query and query not in setting_name.lower() and query not in setting.name.lower():
                continue
            results.append(app_commands.Choice(name=setting.name[:100], value=setting_name[:100]))
            if len(results) >= 25:
                break
        return results

    settings_group = app_commands.Group(
        name="settings",
        description="Manage server settings.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @settings_group.command(name="help", description="Get help on settings.")
    async def help_settings(self, interaction: Interaction):
        texts = self.bot.translate("cogs.settings.help")
        header = texts["header"]
        entry_template = texts["entry"]

        entries: list[str] = []
        for setting in SETTINGS_SCHEMA.values():
            if getattr(setting, "hidden", False):
                continue
            entries.append(
                entry_template.format(
                    name=setting.name,
                    description=setting.description,
                    type=setting.type.__name__,
                )
            )

        help_message = header + CHUNK_SEPARATOR.join(entries)
        chunks = paginate(help_message)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @settings_group.command(name="reset", description="Wipe all settings and start with defaults. This can't be undone")
    async def reset(self, interaction: Interaction):
        """Reset server settings."""
        await interaction.response.defer(ephemeral=True)
        _, rows = await mysql.execute_query("DELETE FROM settings WHERE guild_id = %s", (interaction.guild.id,))
        texts = self.bot.translate("cogs.settings.reset")
        message = texts["done"] if rows > 0 else texts["already"]
        await interaction.followup.send(message)

    @settings_group.command(name="set", description="Set a server setting.")
    @app_commands.autocomplete(value=value_autocomplete, name=name_autocomplete)
    async def set_setting(
        self,
        interaction: Interaction,
        name: str,
        value: str | None = None,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)
        texts = self.bot.translate("cogs.settings.set")
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.followup.send(texts["invalid_name"], ephemeral=True)
            return

        expected = schema.type
        try:
            required_plans = getattr(schema, "required_plans", None)
            if required_plans:
                active_plan = await mysql.resolve_guild_plan(interaction.guild.id)
                if active_plan not in required_plans:
                    requirement = describe_plan_requirements(required_plans)
                    raise ValueError(texts["requires_plan"].format(requirement=requirement))

            # Validate required parameters for expected type
            if expected == bool and value is None:
                raise ValueError(texts["missing_boolean"].format(name=name))
            if expected == int and value is None:
                raise ValueError(texts["missing_integer"].format(name=name))
            if expected == TimeString and value is None:
                raise ValueError(texts["missing_duration"].format(name=name))
            if expected == discord.TextChannel and channel is None:
                raise ValueError(texts["missing_channel"].format(name=name))
            if expected == discord.Role and role is None:
                raise ValueError(texts["missing_role"].format(name=name))
            if expected == list[discord.TextChannel] and channel is None:
                raise ValueError(texts["missing_channel_add"].format(name=name))
            if expected == list[discord.Role] and role is None:
                raise ValueError(texts["missing_role_add"].format(name=name))

            # Type conversion
            if expected == int:
                parsed = int(value)
            elif expected == bool:
                low = str(value).lower()
                boolean_values = texts.get("boolean_values", {})
                true_values = {v.lower() for v in boolean_values.get("true", ("true", "1", "yes"))}
                false_values = {v.lower() for v in boolean_values.get("false", ("false", "0", "no"))}
                if low in true_values:
                    parsed = True
                elif low in false_values:
                    parsed = False
                else:
                    raise ValueError(texts["boolean_only"].format(name=name))
            elif expected == TimeString:
                parsed = TimeString(value)
            elif expected == discord.TextChannel:
                parsed = channel.id
            elif expected == discord.Role:
                parsed = role.id
            elif expected == list[discord.TextChannel]:
                current = await mysql.get_settings(interaction.guild.id, name) or []
                if channel.id not in current:
                    current.append(channel.id)
                parsed = current
            elif expected == list[discord.Role]:
                current = await mysql.get_settings(interaction.guild.id, name) or []
                if role.id not in current:
                    current.append(role.id)
                parsed = current
            else:
                print(texts["unknown_type"])
                parsed = value

            await schema.validate(parsed)
            await mysql.update_settings(interaction.guild.id, name, parsed)
            value_repr = parsed if isinstance(parsed, str) else str(parsed)
            await interaction.followup.send(
                texts["success"].format(name=name, value=value_repr),
                ephemeral=True,
            )

        except ValueError as ve:
            await interaction.followup.send(str(ve), ephemeral=True)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                texts["unexpected"].format(error=e),
                ephemeral=True,
            )

    def _chunk_lines(self, lines: list[str], limit: int = 1000) -> list[str]:
        chunks, buf = [], ""
        for line in lines:
            if len(buf) + len(line) + 1 > limit:
                chunks.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            chunks.append(buf)
        return chunks

    @app_commands.command(name="help", description="Get help on a specific command group.")
    @app_commands.describe(command="Optional: command group to get help with")
    @app_commands.default_permissions(moderate_members=True)
    async def help(self, interaction: Interaction, command: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        dash_url = f"https://modbot.neomechanical.com/dashboard/{interaction.guild.id}"
        color = discord.Color.blurple()

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Dashboard", url=dash_url, emoji="🛠️"))

        avatar = interaction.client.user.display_avatar.url if interaction.client.user else None

        locale = self.bot.resolve_locale_for_interaction(interaction)
        translator = self.bot.translator
        if locale:
            locale_display = f"`{locale}`"
        else:
            locale_display = f"Using default `{translator.default_locale}`"

        if command:
            group = next(
                (cmd for cmd in self.bot.tree.walk_commands()
                if cmd.name == command and isinstance(cmd, app_commands.Group)),
                None
            )
            if not group:
                message = self.bot.translate(
                    "cogs.settings.help.not_found",
                    placeholders={"command": command},
                    fallback=f"No help found for `{command}`.",
                )
                await interaction.followup.send(message, ephemeral=True)
                return

            embed = discord.Embed(
                title=f"/{group.name}",
                description=(group.description or "No description.") +
                            f"\n\n🛠️ **Dashboard:** [Open Dashboard]({dash_url})\nUse `/help {group.name}` to view this again.",
                color=color,
            )
            if avatar:
                embed.set_thumbnail(url=avatar)

            lines = [f"• `/{sub.qualified_name}` — {sub.description or 'No description'}"
                    for sub in group.commands] or ["(No subcommands)"]

            for i, chunk in enumerate(self._chunk_lines(lines), start=1):
                name = "Subcommands" if len(lines) <= 12 else f"Subcommands (page {i})"
                embed.add_field(name=name, value=chunk, inline=False)

            embed.add_field(
                name="Locale Detection",
                value=locale_display,
                inline=False,
            )

            await interaction.followup.send(embed=embed, ephemeral=True, view=view)
            return

        embed = discord.Embed(
            title="Moderator Bot — Help",
            description=(
                 f"🛠️ **Dashboard:** [Open Dashboard]({dash_url})\n"
                "Configure everything faster in the web dashboard.\n"
                "Or run **`/help <group>`** for detailed subcommands."
            ),
            color=color,
        )
        if avatar:
            embed.set_thumbnail(url=avatar)

        groups = [cmd for cmd in self.bot.tree.walk_commands() if isinstance(cmd, app_commands.Group)]
        groups.sort(key=lambda c: c.name.lower())

        lines = [
            f"• `/{g.name}` — {g.description or 'No description'}  _Try:_ `/help {g.name}`"
            for g in groups
        ] or ["(No command groups found.)"]

        for i, chunk in enumerate(self._chunk_lines(lines), start=1):
            name = "Available Command Groups" if i == 1 else f"Available Command Groups (page {i})"
            embed.add_field(name=name, value=chunk, inline=False)

        is_accelerated = await mysql.is_accelerated(guild_id=interaction.guild.id)
        if not is_accelerated:
            embed.add_field(
                name="Speed Up Detection",
                value="⚡ Upgrade to Accelerated for faster NSFW & scam detection — run `/accelerated`.",
                inline=False,
            )

        embed.add_field(
            name="Links",
            value=(
                "[Support](https://discord.gg/invite/33VcwjfEXC) • "
                "[Donate](https://www.paypal.com/donate/?hosted_button_id=9FAG4EDFBBRGC) • "
                "[ToS](https://modbot.neomechanical.com/terms-of-service) • "
                "[Privacy](https://modbot.neomechanical.com/privacy-policy) • "
            ),
            inline=False,
        )

        embed.add_field(
            name="Trusted Developers",
            value=(
                "<@362421457759764481> (fork_prongs - 362421457759764481)"
            ),
            inline=False,
        )

        embed.add_field(
            name="Locale Detection",
            value=locale_display,
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True, view=view)

    @help.autocomplete("command")
    async def help_autocomplete(self, interaction: Interaction, current: str):
        return [
            app_commands.Choice(name=cmd.name, value=cmd.name)
            for cmd in self.bot.tree.walk_commands()
            if isinstance(cmd, app_commands.Group) and current.lower() in cmd.name.lower()
        ][:25]

    @settings_group.command(name="get", description="Get the current value of a server setting.")
    @app_commands.autocomplete(name=name_autocomplete)
    async def get_setting(self, interaction: Interaction, name: str):
        """Get the current value of a server setting."""
        await interaction.response.defer(ephemeral=True)

        texts = self.bot.translate("cogs.settings.get")
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.followup.send(texts["invalid_name"], ephemeral=True)
            return

        current_value = await mysql.get_settings(interaction.guild.id, name)
        expected_type = schema.type

        if current_value is None:
            await interaction.followup.send(
                texts["not_set"].format(name=name, default=schema.default),
                ephemeral=True,
            )
            return

        if schema.private:
            await interaction.followup.send(texts["private"], ephemeral=True)
            return

        if expected_type == list[discord.TextChannel]:
            mentions = []
            for cid in current_value:
                chan = interaction.guild.get_channel(cid)
                mentions.append(
                    chan.mention if chan else texts["list_channel_item"].format(id=cid)
                )
            value_str = ", ".join(mentions)

        elif expected_type == list[discord.Role]:
            mentions = []
            for rid in current_value:
                role = interaction.guild.get_role(rid)
                mentions.append(
                    role.mention if role else texts["list_role_item"].format(id=rid)
                )
            value_str = ", ".join(mentions)

        elif expected_type == discord.TextChannel:
            chan = interaction.guild.get_channel(current_value)
            value_str = chan.mention if chan else texts["list_channel_item"].format(id=current_value)

        elif expected_type == discord.Role:
            role = interaction.guild.get_role(current_value)
            value_str = role.mention if role else texts["list_role_item"].format(id=current_value)

        elif expected_type == dict[str, list[str]]:
            lines: list[str] = []
            for key, values in current_value.items():
                values_str = ", ".join(values)
                lines.append(texts["dict_item"].format(key=key, values=values_str))
            value_str = "\n".join(lines)

        else:
            value_str = str(current_value)

        await interaction.followup.send(
            texts["current"].format(name=name, value=value_str),
            ephemeral=True,
        )

    @settings_group.command(name="remove", description="Remove a server setting or an item from a list-type setting.")
    @app_commands.autocomplete(name=name_autocomplete)
    async def remove_setting(
        self,
        interaction: Interaction,
        name: str,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)
        texts = self.bot.translate("cogs.settings.remove")
        schema = SETTINGS_SCHEMA.get(name)
        if not schema:
            await interaction.followup.send(texts["invalid_name"], ephemeral=True)
            return

        current = await mysql.get_settings(interaction.guild.id, name)
        expected = schema.type

        if not current:
            await interaction.followup.send(
                texts["not_set"].format(name=name),
                ephemeral=True,
            )
            return

        try:
            if expected == list[discord.TextChannel]:
                if not channel:
                    raise ValueError(texts["channel_required"])
                if channel.id not in current:
                    raise ValueError(
                        texts["channel_missing"].format(
                            channel=channel.mention,
                            name=name,
                        )
                    )
                current.remove(channel.id)
                await mysql.update_settings(interaction.guild.id, name, current)
                await interaction.followup.send(
                    texts["removed_channel"].format(channel=channel.mention, name=name),
                    ephemeral=True,
                )

            elif expected == list[discord.Role]:
                if not role:
                    raise ValueError(texts["role_required"])
                if role.id not in current:
                    raise ValueError(
                        texts["role_missing"].format(
                            role=role.mention,
                            name=name,
                        )
                    )
                current.remove(role.id)
                await mysql.update_settings(interaction.guild.id, name, current)
                await interaction.followup.send(
                    texts["removed_role"].format(role=role.mention, name=name),
                    ephemeral=True,
                )

            else:
                await mysql.update_settings(interaction.guild.id, name, None)
                await interaction.followup.send(
                    texts["removed_setting"].format(name=name),
                    ephemeral=True,
                )

        except ValueError as ve:
            await interaction.followup.send(str(ve), ephemeral=True)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                texts["unexpected"].format(error=e),
                ephemeral=True,
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
