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
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

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
THROTTLE_TIME = 30 # seconds
CHARACTER_LIMIT = 200

# Playback settings
local_playback_bot_enabled = os.environ.get('LOCAL_PLAYBACK_BOT', 'false').lower() in ('true', '1', 't')
local_playback_channel_enabled = os.environ.get('LOCAL_PLAYBACK_CHANNEL', 'false').lower() in ('true', '1', 't')

# Mute state management
is_muted = False
mute_timer_task = None 
local_playback_process = None
# User-specific message queues for throttling
user_throttles = {}

# --- Playback Control ---
# Queue for voice playback
voice_queue = asyncio.Queue()
# Event to signal that a playback has finished and the next one can start
playback_finished = asyncio.Event()
# Event to pause the playback worker when muted
play_allowed = asyncio.Event()
play_allowed.set() # Set by default to allow playing

# --- Watchdog File System Handler ---
class AudioFileHandler(FileSystemEventHandler):
    """Handles file system events to queue new audio files for playback."""
    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self.queue = queue
        self.loop = loop

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.wav'):
            print(f"Watchdog detected new file: {event.src_path}")
            # Use call_soon_threadsafe because watchdog runs in a separate thread
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event.src_path)

# --- Mute and Unmute Core Logic ---

async def _mute():
    """Pauses voice client and local playback, and sets the global muted state."""
    global is_muted, mute_timer_task, local_playback_process
    if is_muted:
        return

    print("Muting bot...")
    is_muted = True
    play_allowed.clear()  # PAUSE the playback worker

    if mute_timer_task and not mute_timer_task.done():
        mute_timer_task.cancel()
        mute_timer_task = None

    # Pause the Discord voice client
    guild = bot.get_guild(GUILD_ID)
    if guild:
        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if vc and vc.is_playing():
            vc.pause()
            print("Audio playback paused.")

    # Pause the local playback subprocess
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
    play_allowed.set()  # RESUME the playback worker

    if mute_timer_task and not mute_timer_task.done():
        mute_timer_task.cancel()
        mute_timer_task = None

    # Resume the Discord voice client
    guild = bot.get_guild(GUILD_ID)
    if guild:
        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if vc and vc.is_paused():
            vc.resume()
            print("Audio playback resumed.")

    # Resume the local playback subprocess
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
    site = web.TCPSite(runner, '0.0.0.0', 31335)
    await site.start()
    print("✅ Web API server is running on http://0.0.0.0:31335")

async def setup_hook():
    """This function is called once before the bot logs in."""
    bot.loop.create_task(start_api_server())

bot.setup_hook = setup_hook

# --- Discord Bot Events ---

@bot.event
async def on_ready():
    print(f'Bot logged in as {bot.user}')
    
    # Using a bot attribute to ensure this runs only once
    if not hasattr(bot, 'is_ready_once'):
        bot.is_ready_once = True
        print("Performing one-time setup...")

        # Ensure the outputs folder exists
        if not os.path.exists(OUTPUTS_FOLDER):
            os.makedirs(OUTPUTS_FOLDER)

        # Start watchdog observer to queue new audio files
        event_handler = AudioFileHandler(voice_queue, bot.loop)
        observer = Observer()
        observer.schedule(event_handler, OUTPUTS_FOLDER, recursive=False)
        observer.start()
        print(f"👀 Watchdog is now monitoring the {OUTPUTS_FOLDER} directory.")

        # Set the event initially to allow the first track to play
        playback_finished.set()

        # Start background tasks
        check_voice_channel.start()
        bot.loop.create_task(play_audio_worker())
        print("🔊 Playback worker has started.")

@bot.event
async def on_connect():
    print("Bot connected to Discord.")

@bot.event
async def on_disconnect():
    print("Bot disconnected from Discord. Attempting to reconnect...")

@bot.event
async def on_resume():
    print("Bot has resumed its session.")

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
            await message.channel.send("By the way the bot is muted, you may have to wait.")

    # (The rest of your on_message logic remains unchanged)
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        current_time = time.time()
        if len(message.content) > CHARACTER_LIMIT:
            await message.channel.send(f"Sorry, your message is too long. Please keep it under {CHARACTER_LIMIT} characters.")
            return
#        allowed_chars = r"^[a-zA-Z0-9 .,?!'\n:]*$"
        allowed_chars = r"^[a-zA-Z0-9 .,?!'\n:<>/]*$"
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
                speaker_match = re.match(r"^(1|2|3|4):\s(.*)", line)
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

@tasks.loop(seconds=15)
async def check_voice_channel():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("Guild not found. Make sure the bot is in the guild.")
        return

    vc = discord.utils.get(bot.voice_clients, guild=guild)
    
    if not vc or not vc.is_connected():
        print("Not connected to voice channel. Attempting to connect...")
        voice_channel = guild.get_channel(VOICE_CHANNEL_ID)
        if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
            for i in range(3): # Retry 3 times
                try:
                    await asyncio.wait_for(voice_channel.connect(cls=voice_recv.VoiceRecvClient), timeout=30.0)
                    print(f"Successfully connected to voice channel: {voice_channel.name}")
                    break # Exit loop on success
                except Exception as e:
                    print(f"Failed to connect to voice channel (attempt {i+1}/3): {e}")
                    await asyncio.sleep(5) # Wait 5 seconds before retrying
            else: # This runs if the loop completes without breaking
                print("Failed to connect to voice channel after multiple retries.")
        else:
            print("Voice channel not found or invalid.")
    elif vc.channel.id != VOICE_CHANNEL_ID:
        voice_channel = guild.get_channel(VOICE_CHANNEL_ID)
        if voice_channel:
            try:
                await vc.move_to(voice_channel)
                print(f"Moved to voice channel: {voice_channel.name}")
            except Exception as e:
                print(f"Failed to move to voice channel: {e}")

    # Re-get vc and check listening status
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if vc and vc.is_connected() and local_playback_channel_enabled and not vc.is_listening():
        start_listening(vc)

async def play_audio_worker():
    """A dedicated worker that plays audio from the queue."""
    global local_playback_process
    await bot.wait_until_ready()

    while True:
        # Get the next file to play. This will block until a file is available.
        filepath = await voice_queue.get()

        # Now, wait if the bot is muted. This prevents playing a new track after a mute is requested.
        await play_allowed.wait()

        # Wait for the previous track to finish before starting a new one.
        await playback_finished.wait()

        guild = bot.get_guild(GUILD_ID)
        vc = discord.utils.get(bot.voice_clients, guild=guild)

        # Ensure we are in a voice channel
        if not vc or not vc.is_connected():
            print(f"Not connected to voice, discarding {os.path.basename(filepath)}")
            if os.path.exists(filepath):
                os.remove(filepath)
            voice_queue.task_done()
            # The check_voice_channel task should handle reconnecting.
            # We must set playback_finished here to allow the worker to process the next item
            # in case the connection returns. Otherwise, the worker would be stuck.
            playback_finished.set()
            continue

        # Clear the event, ready for the new playback
        playback_finished.clear()

        print(f"Playing audio file: {filepath}")

        discord_finished_event = asyncio.Event()

        def after_playing_callback(error):
            if error:
                print(f'Player error: {error}')
            bot.loop.call_soon_threadsafe(discord_finished_event.set)

        try:
            # Reset global process handle at the start of a new playback
            local_playback_process = None

            # Start Discord playback
            print("Starting Discord playback...")
            vc.play(discord.FFmpegPCMAudio(filepath), after=after_playing_callback)
            print("Discord playback started.")

            # Start local playback if enabled
            if local_playback_bot_enabled:
                print("Attempting to start local playback with ffplay...")
                try:
                    with open(os.devnull, 'w') as devnull:
                        local_playback_process = subprocess.Popen(
                            ['ffplay', '-nodisp', '-autoexit', filepath],
                            stdout=devnull,
                            stderr=devnull
                        )
                    print("ffplay process started.")
                except Exception as e:
                    print(f"Error starting ffplay subprocess: {e}")

            # --- Wait for completion ---
            # 1. Wait for Discord playback to finish
            await discord_finished_event.wait()

            # 2. Wait for local playback to finish (if it was started)
            if local_playback_process and local_playback_process.poll() is None:
                print("Discord playback finished. Waiting for local playback...")
                # Use an executor to avoid blocking the event loop.
                await bot.loop.run_in_executor(None, local_playback_process.wait)
                print("Local playback finished.")

        except Exception as e:
            print(f"Error during playback: {e}")
            # Ensure local process is killed if an error occurs
            if local_playback_process and local_playback_process.poll() is None:
                print("Killing local playback process due to error.")
                local_playback_process.kill()
                local_playback_process.wait()
        finally:
            # --- Cleanup ---
            local_playback_process = None # Clear the global handle
            print(f"Playback finished for {os.path.basename(filepath)}. Deleting file.")
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError as e:
                    print(f"Error deleting file {filepath}: {e}")

            # Signal that the worker is ready for the next track
            playback_finished.set()
            voice_queue.task_done()

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
