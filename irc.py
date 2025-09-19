import asyncio
import time
import uuid
from irc.bot import SingleServerIRCBot
from pathlib import Path

# ---- Queue setup ----
queue = asyncio.Queue(maxsize=10)
active_users = set()   # Prevent repeat usernames
THROTTLE_SECONDS = 20  # Minimum time between tasks
OUTPUT_DIR = Path("./txt")
OUTPUT_DIR.mkdir(exist_ok=True)


async def worker():
    """Processes items from the queue with throttling."""
    while True:
        username, text = await queue.get()
        try:
            print(f"[Worker] Processing from {username}: {text}")

            # Save text to file
            out_file = OUTPUT_DIR / f"text-{uuid.uuid4().hex[:6]}.txt"
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(f"Speaker 1: {text}\n")

            print(f"[Worker] Saved: {out_file}")

            # Throttle
            await asyncio.sleep(THROTTLE_SECONDS)

        except Exception as e:
            print(f"[Worker] Error: {e}")
        finally:
            active_users.discard(username)
            queue.task_done()


async def add_to_queue(username, text):
    """Add text to queue if user not already in queue."""
    if username in active_users:
        print(f"[Queue] Skipping {username}, already queued.")
        return
    try:
        queue.put_nowait((username, text))
        active_users.add(username)
        print(f"[Queue] Added {username}: {text}")
    except asyncio.QueueFull:
        print("[Queue] Queue full, dropping message.")


# ---- IRC Bot ----
class SimpleIRCBot(SingleServerIRCBot):
    def __init__(self, channel, nickname, server, loop, port=6667):
        super().__init__([(server, port)], nickname, nickname)
        self.channel = channel
        self.loop = loop

    def on_welcome(self, connection, event):
        connection.join(self.channel)
        print(f"[IRC] Joined {self.channel}")

    def on_privmsg(self, connection, event):
        username = event.source.nick
        message = event.arguments[0].strip()
        if message:
            asyncio.run_coroutine_threadsafe(
                add_to_queue(username, message), self.loop
            )


async def main():
    # Start worker
    asyncio.create_task(worker())

    # Start IRC bot in executor
    loop = asyncio.get_running_loop()
    def start_bot():
        bot = SimpleIRCBot("#4chancup", "DrBOTrisG", "irc.implyingrigged.info", loop)
        bot.start()
    await loop.run_in_executor(None, start_bot)


# ---- Entry ----
if __name__ == "__main__":
    asyncio.run(main())

