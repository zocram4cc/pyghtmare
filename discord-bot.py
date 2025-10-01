import discord
from discord.ext import commands, tasks
import os
import asyncio
from collections import deque
import time
import re
import uuid
import subprocess
from discord.ext import voice_recv

# Create a subfolder for model input
if not os.path.exists("./txt"):
    os.makedirs("./txt")

# Bot's intents and command prefix
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration
GUILD_ID = int(os.environ.get("GUILD_ID", 0))
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", 0))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "0")
OUTPUTS_FOLDER = "./outputs"
THROTTLE_TIME = 30 # seconds
CHARACTER_LIMIT = 250
is_muted = False
local_playback_bot_enabled = os.environ.get('LOCAL_PLAYBACK_BOT', 'false').lower() in ('true', '1', 't')
local_playback_channel_enabled = os.environ.get('LOCAL_PLAYBACK_CHANNEL', 'false').lower() in ('true', '1', 't')


# User-specific message queues for throttling
user_throttles = {}

# Queue for voice playback
voice_queue = asyncio.Queue()

# Event to handle bot readiness
@bot.event
async def on_ready():
    print(f'Bot logged in as {bot.user}')
    check_voice_channel.start()
    play_audio_from_queue.start()

# Task to connect the bot to the voice channel
@tasks.loop(count=1)
async def check_voice_channel():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        voice_channel = guild.get_channel(VOICE_CHANNEL_ID)
        if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
            try:
                vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
                print(f"Joined voice channel: {voice_channel.name}")
                if local_playback_channel_enabled:
                    start_listening(vc)
            except Exception as e:
                print(f"Failed to join voice channel: {e}")
    else:
        print("Guild not found. Make sure the bot is in the guild.")

# Task to check for new WAV files and add them to the queue
@tasks.loop(seconds=5)
async def play_audio_from_queue():
    global is_muted
    try:
        # New: Check if the bot is muted before proceeding
        if is_muted:
            return

        if bot.voice_clients and bot.voice_clients[0].is_connected():
            for filename in os.listdir(OUTPUTS_FOLDER):
                if filename.endswith(".wav"):
                    filepath = os.path.join(OUTPUTS_FOLDER, filename)


                    if not any(item['path'] == filepath for item in voice_queue._queue):
                        await voice_queue.put({'path': filepath, 'name': filename})
                        print(f"Added {filename} to the voice queue.")


            voice_client = bot.voice_clients[0]
            if not voice_client.is_playing() and not voice_queue.empty():
                item = await voice_queue.get()
                filepath = item['path']
                print(f"Playing audio file: {filepath}")

                if local_playback_bot_enabled:
                    subprocess.Popen(['paplay', filepath])

                def after_playing(error):
                    if error:
                        print(f'Player error: {error}')
                    print(f"Finished playing {item['name']}. Deleting file.")
                    os.remove(filepath)


                voice_client.play(discord.FFmpegPCMAudio(filepath), after=after_playing)

    except discord.errors.ClientException as e:
        print(f"ClientException: {e}. Bot not connected to voice. Retrying...")

# New: Mute command
@bot.command()
@commands.has_permissions(stream=True)
async def mute(ctx):
    """Mutes the bot's voice playback."""
    global is_muted
    is_muted = True

    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and voice_client.is_playing():
        voice_client.stop()

    await ctx.send("Bot voice playback has been muted.")
    print(f"{ctx.author} has muted the bot.")



# New: Unmute command
@bot.command()
@commands.has_permissions(stream=True)
async def unmute(ctx):
    """Unmutes the bot's voice playback."""
    global is_muted
    is_muted = False
    await ctx.send("Bot voice playback has been unmuted.")
    print(f"{ctx.author} has unmuted the bot.")

class PaplaySink(voice_recv.AudioSink):
    def __init__(self):
        self.proc = subprocess.Popen(['paplay', '--raw', '--channels=2', '--rate=48000', '--format=s16le'], stdin=subprocess.PIPE)

    def wants_opus(self):
        return False

    def write(self, user, data):
        if self.proc.stdin:
            self.proc.stdin.write(data.pcm)

    def cleanup(self):
        self.proc.kill()

def start_listening(voice_client):
    sink = PaplaySink()
    voice_client.listen(sink)
    voice_client.sink = sink

def stop_listening(voice_client):
    if hasattr(voice_client, 'sink') and voice_client.sink:
        voice_client.sink.cleanup()
        voice_client.sink = None
    voice_client.stop_listening()

@bot.command()
@commands.has_permissions(stream=True)
async def local_playback_bot(ctx, state: str):
    """Enables or disables local playback of the bot's audio. Usage: !local_playback_bot <on|off>"""
    global local_playback_bot_enabled
    if state.lower() == 'on':
        local_playback_bot_enabled = True
        await ctx.send("Local playback of bot audio enabled.")
    elif state.lower() == 'off':
        local_playback_bot_enabled = False
        await ctx.send("Local playback of bot audio disabled.")
    else:
        await ctx.send("Invalid state. Use 'on' or 'off'.")

@bot.command()
@commands.has_permissions(stream=True)
async def local_playback_channel(ctx, state: str):
    """Enables or disables local playback of the voice channel's audio. Usage: !local_playback_channel <on|off>"""
    global local_playback_channel_enabled
    if state.lower() == 'on':
        local_playback_channel_enabled = True
        await ctx.send("Local playback of voice channel audio enabled.")
        # Start listening if not already
        if ctx.voice_client and not ctx.voice_client.is_listening():
            start_listening(ctx.voice_client)
    elif state.lower() == 'off':
        local_playback_channel_enabled = False
        await ctx.send("Local playback of voice channel audio disabled.")
        # Stop listening
        if ctx.voice_client and ctx.voice_client.is_listening():
            stop_listening(ctx.voice_client)
    else:
        await ctx.send("Invalid state. Use 'on' or 'off'.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    else:
        print(f"An unhandled error has occured: {error}")
        raise error

# Event to handle private messages

@bot.event
async def on_message(message):
    global is_muted
    if is_muted:
        await message.channel.send(f"The bot is muted and is not accepting any input.")
        return
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Check for a direct message (DM)
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        current_time = time.time()

        # Check for message length first
        if len(message.content) > CHARACTER_LIMIT:
            await message.channel.send(f"Sorry, your message is too long. Please keep it under {CHARACTER_LIMIT} characters.")
            return
        
        # Check for invalid characters using a regular expression
        # This pattern accepts letters, numbers, spaces, and the specified punctuation
        allowed_chars = r"^[a-zA-Z0-9 .,?!\'\n:]*$"
        if not re.match(allowed_chars, message.content):
            await message.channel.send("Sorry, your message contains special characters that are not allowed. Please only use letters, numbers, spaces, and the following punctuation: ., ?, !, '")
            return

        # Check for user throttle
        if user_id in user_throttles:
            last_message_time = user_throttles[user_id]
            if current_time - last_message_time < THROTTLE_TIME:
                print(f"Throttled message from {message.author}. Time remaining: {THROTTLE_TIME - (current_time - last_message_time):.2f}s")
                await message.channel.send("Slow down! You can only send a message every 20 seconds.")
                return

        user_throttles[user_id] = current_time
        

        content_to_write = ""
        # Check if the message is structured (e.g., "1: Hello", "2: World")
        if re.match(r"^\d:\s", message.content):
            lines = message.content.split('\n')
            processed_lines = []
            for line in lines:
                speaker_match = re.match(r"^(1|2):\s(.*)", line)
                if speaker_match:
                    speaker_num = speaker_match.group(1)
                    speaker_text = speaker_match.group(2)
                    processed_lines.append(f"Speaker {speaker_num}: {speaker_text}")
                else:
                    await message.channel.send("Invalid structured message format. Each line must start with '1:' or '2:'.")
                    return
            content_to_write = "\n".join(processed_lines)
        else:
            # Unstructured message, prepend "Speaker 1:"
            content_to_write = f"Speaker 1: {message.content}"

        filename = f"./txt/{message.author.name}_{uuid.uuid4().hex[:6]}.txt"
        
        # Create a user-specific log file
        with open(filename, "a", encoding="utf-8") as file:
            file.write(f"{content_to_write}\n")
        print(f"Logged message from {message.author.name} to {filename}")
        
    await bot.process_commands(message)

# Run the bot
bot.run(BOT_TOKEN)
