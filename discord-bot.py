import discord
from discord.ext import commands, tasks
import os
import asyncio
from collections import deque
import time
import re
import uuid
import subprocess
import threading
from discord.ext import voice_recv
from aiohttp import web
import json
import signal

# Create a subfolder for model input
if not os.path.exists("./txt"):
    os.makedirs("./txt")

# Bot's intents and command prefix
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Configuration & Global State ---
GUILD_ID = int(os.environ.get("GUILD_ID", 0))
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", 0))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "0")
OUTPUTS_FOLDER = "./outputs"
THROTTLE_TIME = 45 # seconds
CHARACTER_LIMIT = 250

# Playback settings
local_playback_bot_enabled = os.environ.get('LOCAL_PLAYBACK_BOT', 'false').lower() in ('true', '1', 't')
local_playback_channel_enabled = os.environ.get('LOCAL_PLAYBACK_CHANNEL', 'false').lower() in ('true', '1', 't')

# Mute state management
is_muted = False
mute_timer_task = None 
local_playback_process = None
# User-specific message queues for throttling
user_throttles = {}

# Queue for voice playback
voice_queue = asyncio.Queue()

# --- Mute and Unmute Core Logic ---

async def _mute():
    """Pauses voice client and local playback, and sets the global muted state."""
    global is_muted, mute_timer_task, local_playback_process
    if is_muted:
        return

    print("Muting bot...")
    is_muted = True

    if mute_timer_task and not mute_timer_task.done():
        mute_timer_task.cancel()
        mute_timer_task = None
    
    # Pause the Discord voice client
    if bot.voice_clients:
        vc = bot.voice_clients[0]
        if vc.is_playing():
            vc.pause()
            print("Audio playback paused.")

    # --- FIX: Pause the local playback subprocess ---
    if local_playback_process and local_playback_process.poll() is None:
        try:
            local_playback_process.send_signal(signal.SIGSTOP)
            print("Local playback process paused.")
        except Exception as e:
            print(f"Error pausing local playback process: {e}")


async def _unmute():
    """Resumes voice client and local playback, and clears the global muted state."""
    global is_muted, mute_timer_task, local_playback_process
    if not is_muted:
        return

    print("Unmuting bot...")
    is_muted = False
    
    if mute_timer_task and not mute_timer_task.done():
        mute_timer_task.cancel()
        mute_timer_task = None

    # Resume the Discord voice client
    if bot.voice_clients:
        vc = bot.voice_clients[0]
        if vc.is_paused():
            vc.resume()
            print("Audio playback resumed.")

    # --- FIX: Resume the local playback subprocess ---
    if local_playback_process and local_playback_process.poll() is None:
        try:
            local_playback_process.send_signal(signal.SIGCONT)
            print("Local playback process resumed.")
        except Exception as e:
            print(f"Error resuming local playback process: {e}")

# --- Web API Handlers ---

async def handle_mute(request):
    """API endpoint to mute the bot, with optional duration."""
    global mute_timer_task
    try:
        data = await request.json()
        duration = int(data.get("duration"))
    except (json.JSONDecodeError, ValueError, TypeError):
        duration = None
    
    await _mute()

    if duration and duration > 0:
        async def unmute_after_delay(delay):
            await asyncio.sleep(delay)
            print(f"Timed mute of {delay}s is over. Unmuting now.")
            await _unmute()
        
        mute_timer_task = bot.loop.create_task(unmute_after_delay(duration))
        return web.Response(text=f"Bot has been muted for {duration} seconds.", status=200)
    
    return web.Response(text="Bot has been muted indefinitely.", status=200)

async def handle_unmute(request):
    """API endpoint to unmute the bot."""
    await _unmute()
    return web.Response(text="Bot has been unmuted.", status=200)

# --- Web Server Setup ---

async def start_api_server():
    """Initializes and starts the aiohttp web server."""
    app = web.Application()
    app.router.add_post("/api/mute", handle_mute)
    app.router.add_post("/api/unmute", handle_unmute)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("✅ Web API server is running on http://0.0.0.0:8080")

async def setup_hook():
    """This function is called once before the bot logs in."""
    bot.loop.create_task(start_api_server())

bot.setup_hook = setup_hook

# --- Discord Bot Events ---

@bot.event
async def on_ready():
    print(f'Bot logged in as {bot.user}')
    check_voice_channel.start()
    play_audio_from_queue.start()

@bot.event
async def on_voice_state_update(member, before, after):
    """Handles server-side mutes and unmutes for the bot."""
    if member.id != bot.user.id:
        return

    # Bot was muted by a server admin
    if not before.mute and after.mute:
        print("Bot was server-muted.")
        await _mute()
    # Bot was unmuted by a server admin
    elif before.mute and not after.mute:
        print("Bot was server-unmuted.")
        await _unmute()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Reject messages if muted
    if is_muted:
        if isinstance(message.channel, discord.DMChannel):
            await message.channel.send("The bot is currently muted and cannot process messages.")
        return

    # (The rest of your on_message logic remains unchanged)
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        current_time = time.time()
        if len(message.content) > CHARACTER_LIMIT:
            await message.channel.send(f"Sorry, your message is too long. Please keep it under {CHARACTER_LIMIT} characters.")
            return
        allowed_chars = r"^[a-zA-Z0-9 .,?!\'\n:]*$"
        if not re.match(allowed_chars, message.content):
            await message.channel.send("Sorry, your message contains special characters that are not allowed.")
            return
        if user_id in user_throttles and current_time - user_throttles[user_id] < THROTTLE_TIME:
            await message.channel.send(f"Slow down! You can only send a message every {THROTTLE_TIME} seconds.")
            return
        user_throttles[user_id] = current_time
        content_to_write = ""
        if re.match(r"^\d:\s", message.content):
            lines = message.content.split('\n')
            processed_lines = []
            for line in lines:
                speaker_match = re.match(r"^(1|2):\s(.*)", line)
                if speaker_match:
                    speaker_num, speaker_text = speaker_match.groups()
                    processed_lines.append(f"Speaker {speaker_num}: {speaker_text}")
                else:
                    await message.channel.send("Invalid structured message format. Each line must start with '1:' or '2:'.")
                    return
            content_to_write = "\n".join(processed_lines)
        else:
            content_to_write = f"Speaker 1: {message.content}"
        filename = f"./txt/{message.author.name}_{uuid.uuid4().hex[:6]}.txt"
        with open(filename, "a", encoding="utf-8") as file:
            file.write(f"{content_to_write}\n")
        print(f"Logged message from {message.author.name} to {filename}")
        
    await bot.process_commands(message)

# --- Bot Tasks ---

@tasks.loop(count=1)
async def check_voice_channel():
    await bot.wait_until_ready()
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
            print("Voice channel not found or invalid.")
    else:
        print("Guild not found. Make sure the bot is in the guild.")

@tasks.loop(seconds=5)
async def play_audio_from_queue():
    global local_playback_process # Add global accessor
    await bot.wait_until_ready()

    if is_muted:
        return

    if not bot.voice_clients:
        return

    vc = bot.voice_clients[0]
    if not vc.is_connected():
        return
        
    for filename in os.listdir(OUTPUTS_FOLDER):
        if filename.endswith(".wav"):
            filepath = os.path.join(OUTPUTS_FOLDER, filename)
            if not any(item['path'] == filepath for item in voice_queue._queue):
                await voice_queue.put({'path': filepath, 'name': filename})
                print(f"Added {filename} to the voice queue.")

    if not vc.is_playing() and not voice_queue.empty():
        if local_playback_process:
            local_playback_process = None

        item = await voice_queue.get()
        filepath = item['path']
        print(f"Playing audio file: {filepath}")

        def after_playing(error):
            # --- THIS IS THE FIX ---
            # Use 'global' because the variable is defined at the top of the script
            global local_playback_process 
            
            if error:
                print(f'Player error: {error}')
            print(f"Finished playing {item['name']}. Deleting file.")
            if os.path.exists(filepath):
                os.remove(filepath)
            
            # Clean up the process reference after playback
            if local_playback_process:
                if local_playback_process.poll() is None:
                    local_playback_process.kill()
                local_playback_process = None

        vc.play(discord.FFmpegPCMAudio(filepath), after=after_playing)
        
        if local_playback_bot_enabled:
            local_playback_process = subprocess.Popen(['paplay', filepath])

# --- Discord Commands ---

@bot.command(help="Mutes the bot's voice playback.")
@commands.has_permissions(stream=True)
async def mute(ctx):
    if is_muted:
        await ctx.send("Bot is already muted.")
    else:
        await _mute()
        await ctx.send("Bot voice playback has been muted.")
        print(f"{ctx.author} has muted the bot.")

@bot.command(help="Unmutes the bot's voice playback.")
@commands.has_permissions(stream=True)
async def unmute(ctx):
    if not is_muted:
        await ctx.send("Bot is not muted.")
    else:
        await _unmute()
        await ctx.send("Bot voice playback has been unmuted.")
        print(f"{ctx.author} has unmuted the bot.")

# (Your other commands and classes like PaplaySink, start_listening, etc. remain unchanged)
class PaplaySink(voice_recv.AudioSink):
    def __init__(self):
        self.proc = subprocess.Popen(['paplay', '--raw', '--channels=2', '--rate=48000', '--format=s16le'], stdin=subprocess.PIPE)
        self.queue = asyncio.Queue()
        self.loop = asyncio.get_event_loop()
        self.write_task = self.loop.create_task(self._write_to_paplay())
    def wants_opus(self): return False
    def write(self, user, data): self.loop.call_soon_threadsafe(self.queue.put_nowait, data.pcm)
    async def _write_to_paplay(self):
        while True:
            pcm_data = await self.queue.get()
            if pcm_data is None: break
            try: self.proc.stdin.write(pcm_data)
            except: break
    def cleanup(self):
        if self.write_task and not self.write_task.done():
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
            self.loop.call_soon_threadsafe(self.write_task.cancel)
        if self.proc.poll() is None: self.proc.kill()
        self.proc.wait()

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
    global local_playback_bot_enabled
    if state.lower() == 'on':
        local_playback_bot_enabled = True
        await ctx.send("Local playback of bot audio enabled.")
    elif state.lower() == 'off':
        local_playback_bot_enabled = False
        await ctx.send("Local playback of bot audio disabled.")
    else: await ctx.send("Invalid state. Use 'on' or 'off'.")

@bot.command()
@commands.has_permissions(stream=True)
async def local_playback_channel(ctx, state: str):
    global local_playback_channel_enabled
    if state.lower() == 'on':
        local_playback_channel_enabled = True
        await ctx.send("Local playback of voice channel audio enabled.")
        if ctx.voice_client and not ctx.voice_client.is_listening():
            start_listening(ctx.voice_client)
    elif state.lower() == 'off':
        local_playback_channel_enabled = False
        await ctx.send("Local playback of voice channel audio disabled.")
        if ctx.voice_client and ctx.voice_client.is_listening():
            stop_listening(ctx.voice_client)
    else: await ctx.send("Invalid state. Use 'on' or 'off'.")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    else:
        print(f"An unhandled error has occured: {error}")
        raise error

# --- Run Bot ---
if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "0":
        print("ERROR: BOT_TOKEN environment variable not set.")
    else:
        bot.run(BOT_TOKEN)