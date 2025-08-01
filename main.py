# main.py
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import asyncio
import io
from datetime import datetime, timezone
from flask import Flask
import threading
from collections import defaultdict

import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
from dotenv import load_dotenv
import requests

# ------------------ Load ENV ------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID_COMPLAINTS = os.getenv("GOOGLE_SHEET_ID_COMPLAINTS")
GOOGLE_SHEET_ID_AUDITOR = os.getenv("GOOGLE_SHEET_ID_AUDITOR")
creds_json = os.getenv("GOOGLE_CREDS_JSON")

# ------------------ Rate Limit ------------------
RATE_LIMIT = 10
RATE_LIMIT_WINDOW = 60
usage_tracker = defaultdict(list)

# ------------------ Google Auth ------------------
creds_dict = json.loads(creds_json)
creds = service_account.Credentials.from_service_account_info(
    creds_dict,
    scopes=['https://www.googleapis.com/auth/documents.readonly',
            'https://www.googleapis.com/auth/drive.readonly',
            'https://www.googleapis.com/auth/spreadsheets']
)
drive_service = build('drive', 'v3', credentials=creds)
docs_service = build('docs', 'v1', credentials=creds)
gc = gspread.authorize(creds)
sheet_complaints = gc.open_by_key(GOOGLE_SHEET_ID_COMPLAINTS)
sheet_auditor = gc.open_by_key(GOOGLE_SHEET_ID_AUDITOR)

# ------------------ Discord Setup ------------------
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.dm_messages = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ------------------ Allowed Roles ------------------
ALLOWED_ROLE_IDS = {1397015557185867799, 123456789012345678}

def is_allowed(interaction_or_ctx):
    if isinstance(interaction_or_ctx, discord.Interaction):
        member = interaction_or_ctx.guild.get_member(interaction_or_ctx.user.id)
        if member.guild_permissions.administrator:
            return True
        return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)
    elif isinstance(interaction_or_ctx, commands.Context):
        member = interaction_or_ctx.author
        if member.guild_permissions.administrator:
            return True
        return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)

# ------------------ Revert Checker ------------------
@tasks.loop(minutes=5)
async def check_reverts():
    ws = sheet_complaints.worksheet("Noosphere Collective")
    records = ws.get_all_records()
    for i, row in enumerate(records):
        if row.get("Revert") and row.get("Revert Sent") != "done":
            try:
                user = await bot.fetch_user(int(row["User Id"]))
                msg = row["Revert"]
                text_parts, files = [], []
                for part in msg.split("\n"):
                    if part.strip().startswith("http") and any(x in part for x in [".png", ".jpg", ".jpeg"]):
                        r = requests.get(part)
                        if r.status_code == 200:
                            files.append(discord.File(io.BytesIO(r.content), filename=part.split("/")[-1]))
                    else:
                        text_parts.append(part)
                await user.send("\n".join(text_parts), files=files if files else None)
                ws.update_cell(i + 2, 9, "done")
            except Exception as e:
                print(f"[REVERT ERROR] {e}")

# ------------------ Fetch Doc Content & Images ------------------
async def fetch_doc_content_and_images(interaction=None, channel=None):
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    content = ""
    image_files = []
    inline_objects = doc.get("inlineObjects", {})
    object_images = {}
    for obj_id, obj in inline_objects.items():
        try:
            uri = obj["inlineObjectProperties"]["embeddedObject"]["imageProperties"]["contentUri"]
            object_images[obj_id] = uri
        except KeyError:
            continue
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for elem in element["paragraph"].get("elements", []):
                if "textRun" in elem:
                    text = elem["textRun"]["content"]
                    if interaction and interaction.guild:
                        for role in interaction.guild.roles:
                            if f"@{role.name}" in text:
                                text = text.replace(f"@{role.name}", role.mention)
                            if f"@{role.id}" in text:
                                text = text.replace(f"@{role.id}", role.mention)
                    content += text
                elif "inlineObjectElement" in elem:
                    content += f"[image:{elem['inlineObjectElement']['inlineObjectId']}]"
    for obj_id, url in object_images.items():
        try:
            headers = {"Authorization": f"Bearer {creds.token}"}
            r = requests.get(url, headers=headers)
            if r.status_code == 200:
                ext = r.headers['content-type'].split('/')[-1]
                image_files.append(discord.File(io.BytesIO(r.content), filename=f"image_{obj_id}.{ext}"))
        except:
            continue
    for obj_id in object_images:
        content = content.replace(f"[image:{obj_id}]", "")
    return content.strip(), image_files

# ------------------ Rate Limit Check ------------------
def check_rate_limit(user_id):
    now = datetime.now().timestamp()
    usage = usage_tracker[user_id]
    usage = [t for t in usage if now - t < RATE_LIMIT_WINDOW]
    usage_tracker[user_id] = usage
    return len(usage) < RATE_LIMIT

# ------------------ /announce ------------------
@bot.tree.command(name="announce", description="Send an announcement from Google Docs")
@app_commands.describe(channel="Channel to send announcement")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_allowed(interaction):
        await interaction.response.send_message("You lack permission.", ephemeral=True)
        return
    if not check_rate_limit(interaction.user.id):
        await interaction.response.send_message("Rate limit hit.", ephemeral=True)
        return
    usage_tracker[interaction.user.id].append(datetime.now().timestamp())
    await interaction.response.defer()
    try:
        text, images = await fetch_doc_content_and_images(interaction, channel)
        await channel.send(text)
        if images:
            await channel.send(files=images)
        await interaction.followup.send(f"Announcement sent to {channel.mention}.", ephemeral=True)
    except Exception as e:
        print(e)
        await interaction.followup.send("Error sending announcement.", ephemeral=True)

# ------------------ !announce ------------------
@bot.command(name="announce")
async def announce_cmd(ctx, channel: discord.TextChannel):
    if not is_allowed(ctx):
        await ctx.send("You lack permission.")
        return
    if not check_rate_limit(ctx.author.id):
        await ctx.send("Rate limit hit.")
        return
    usage_tracker[ctx.author.id].append(datetime.now().timestamp())
    try:
        text, images = await fetch_doc_content_and_images(None, channel)
        await channel.send(text)
        if images:
            await channel.send(files=images)
    except Exception as e:
        print(e)
        await ctx.send("Error sending announcement.")

# ------------------ DM Complaint Logger ------------------
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.DMChannel):
        user_id = str(message.author.id)
        content = message.content
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        attachments = "\n".join([a.url for a in message.attachments])
        complaint = f"{content}\n{attachments}" if attachments else content
        ws = sheet_complaints.worksheet("Noosphere Collective")
        ws.append_row([user_id, complaint, date, "", "", "", "", "", ""])
        await message.reply("✅ Your complaint has been received. Thank you!")
    await bot.process_commands(message)

# ------------------ Auditor Logger ------------------
@bot.event
async def on_message_edit(before, after):
    await on_message(after)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    user_id = str(member.id)
    server_name = member.guild.name
    if before.channel is None and after.channel:
        msg, ch = "joined voice channel", f"🎙 {after.channel.name}"
    elif before.channel and after.channel is None:
        msg, ch = "left voice channel", f"🎙 {before.channel.name}"
    elif before.channel != after.channel:
        msg = "switched voice channel"
        ch = f"🎙 {before.channel.name} → {after.channel.name}"
    else:
        return
    try:
        ws = sheet_auditor.worksheet(server_name)
        ws.append_row([timestamp, user_id, msg, ch, server_name])
    except Exception as e:
        print(f"[AUDIT ERROR] {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user or isinstance(message.channel, discord.DMChannel):
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    user_id = str(message.author.id)
    content = message.content
    ch = f"💬 {message.channel.name}"
    server_name = message.guild.name
    try:
        ws = sheet_auditor.worksheet(server_name)
        ws.append_row([timestamp, user_id, content, ch, server_name])
    except Exception as e:
        print(f"[LOG ERROR] {e}")
    await bot.process_commands(message)

# ------------------ Flask Keep Alive ------------------
app = Flask('')
@app.route('/')
def home():
    return "Noosphere Collective Bot is alive!"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# ------------------ On Ready ------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Slash command sync failed: {e}")
    check_reverts.start()

# ------------------ Run Bot ------------------
bot.run(DISCORD_TOKEN)
