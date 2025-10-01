# Pyghtmare
Generate le funny AI cup voices and stream them to dicksword. Tested exclusively under LEENUX using an AMD 6900 XT. You will probably have much better results using nvidia.
This repo is 3 python scripts consisting of:
- a braindead VibeVoice implementation which will keep the VibeVoice model loaded in memory, and start generating audio whenever it detects a new .txt file under the txt folder.
- a simple discord bot which will join a single guild and voice channel and play back any new .wav files it detects under the outputs folder, while turning any private messages it gets into new .txt files for ingestion in the model.
- an alternative bot for IRC which will do the same txt ingestion, with a bash script to play it back on a virtual microphone using a pulseaudio sink. I developed this first, but it was too convoluted and Discord has some weird audio filtering so I decided to convert it into a native discord bot instead.
# Usage
Maybe setup a venv, I'm not your mom, do whatever the fuck you want.
```bash
python -m venv venv/
source venv/bin/activate
```
Install Vibevoice:

```bash
git clone https://github.com/vibevoice-community/VibeVoice.git
cd VibeVoice/

uv pip install -e .
```
Install the requirements.txt (NOT TESTED; I STARTED WITH COMFYUI VIBEVOICE AND JUST MADE THE REQUIREMENTS LATER)

```bash
pip install -r requirements.txt
```
If you have AMD you'll need to do this and be on LEEENUX:
```bash
pip install --pre torch torchvision torchaudio  --index-url https://download.pytorch.org/whl/nightly/rocm6.4
```

Run the damn thing and test it:
```bash
python generator.py --speaker_names boris crimson --model_path "vibevoice/VibeVoice-1.5B" --dtype float16
```
If you have enough VRAM (>16GB) you can also use VibeVoice-7B.
Drop a text file under txt/ formatted like so:
```
Speaker 1: By default, this will be read by boris.
Speaker 2: And this will be read by crimson.
```
Configure the discord bot by setting the following environment variables:
- `BOT_TOKEN`: Your Discord bot token.
- `GUILD_ID`: The ID of your Discord server.
- `VOICE_CHANNEL_ID`: The ID of the voice channel you want the bot to join.

You can find the IDs by right clicking the guild and the voice channel, it's the last option and it'll be a number like 283304740931201011.

You'll also need to make a discord bot [here](https://discord.com/developers/applications) and figure out permissions. There's documentation for that, I ain't explaining it.

Finally, run the bot:
```bash
python discord-bot.py
```
If you did everything right you will now have your bot join your chosen guild and occasionally yell at you with le funny boris voice. Currently discord-bot.py only accepts Speaker 1 (Boris by default) but it should be easy enough to make it spit out Speaker 2/3/4.

# Arguments for generator.py

| Argument          | Type         | Default                               | Description                                                                                                                                                        |
| ----------------- | ------------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--model_path`    | `str`        | `microsoft/VibeVoice-1.5b`            | Path or HuggingFace model identifier for the speech generation model.                                                                                              |
| `--speaker_names` | `str` (list) | `['boris']`                           | List of speaker names (in order) to assign to generated voices.                                                                                                    |
| `--output_dir`    | `str`        | `./outputs`                           | Directory where generated audio files will be saved.                                                                                                               |
| `--device`        | `str`        | Auto-detects (`cuda` → `mps` → `cpu`) | Device to use for inference. Choose between **cuda** (GPU), **mps** (Apple Silicon), or **cpu**.                                                                   |
| `--cfg_scale`     | `float`      | `1.3`                                 | Classifier-Free Guidance (CFG) scale. Higher values = stronger adherence to prompts, lower = more diverse output.                                                  |
| `--watch_dir`     | `str`        | `./txt`                               | Directory to watch for new `.txt` files. Each new file triggers voice generation.                                                                                  |
| `--dtype`         | `str`        | `float32`                             | Torch data type for model weights. Options: **float32** (high precision), **float16** (lower memory, faster on GPUs), **bfloat16** (efficient on modern hardware). |

# Discord Bot Commands

The Discord bot responds to commands and direct messages.

## Commands

*   `!mute`: Mutes the bot's voice playback. The bot will stop playing audio and will not accept any new messages (direct or commands) until unmuted.
*   `!unmute`: Unmutes the bot's voice playback, allowing it to resume playing audio and accepting messages.
*   `!local_playback_bot <on|off>`: Enables or disables local playback of the bot's generated audio. When `on`, the bot's audio will also be played through the local system's audio output.
*   `!local_playback_channel <on|off>`: Enables or disables local playback of audio from the voice channel the bot is connected to. When `on`, audio from other users in the voice channel will be played through the local system's audio output.

## Direct Messages (DMs)

The bot processes direct messages differently based on their format:

### Unstructured Messages

If a direct message does not start with a speaker number followed by a colon (e.g., "1:"), it is treated as an unstructured message. The bot will automatically prepend "Speaker 1: " to the message content before saving it to a `.txt` file for processing.

**Example:**
User sends: `Hello, how are you today?`
Saved as: `Speaker 1: Hello, how are you today?`

### Structured Messages

Direct messages can also be structured to specify different speakers. Each line in a structured message must start with either "1:" or "2:", followed by the speaker's text. The bot will validate each line; if any line does not conform to this format, the entire message will be rejected.

**Example:**
User sends:
```
1: This is the first speaker.
2: And this is the second speaker.
1: Back to the first speaker again.
```
Saved as:
```
Speaker 1: This is the first speaker.
Speaker 2: And this is the second speaker.
Speaker 1: Back to the first speaker again.
```

**Important Notes:**
*   Only speaker numbers "1" and "2" are currently supported for structured messages.
*   The bot will reject messages containing other numbers or lines that do not start with a valid speaker number and colon.
*   The character limit still applies to the total length of the message, including newlines.
