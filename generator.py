import argparse
import os
import time
import torch
import re
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from vibevoice.modular.modeling_vibevoice_inference import VibeVoiceForConditionalGenerationInference
from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor
from transformers.utils import logging
import traceback

logging.set_verbosity_info()
logger = logging.get_logger(__name__)

class VoiceMapper:
    """Maps speaker names to voice file paths"""

    def __init__(self):
        self.setup_voice_presets()

    def setup_voice_presets(self):
        voices_dir = os.path.join(os.path.dirname(__file__), "voices")
        if not os.path.exists(voices_dir):
            print(f"Warning: Voices directory not found at {voices_dir}")
            self.voice_presets = {}
            self.available_voices = {}
            return

        self.voice_presets = {}
        wav_files = [f for f in os.listdir(voices_dir)
                     if f.lower().endswith('.wav') and os.path.isfile(os.path.join(voices_dir, f))]
        for wav_file in wav_files:
            name = os.path.splitext(wav_file)[0]
            full_path = os.path.join(voices_dir, wav_file)
            self.voice_presets[name] = full_path

        self.voice_presets = dict(sorted(self.voice_presets.items()))
        self.available_voices = {name: path for name, path in self.voice_presets.items() if os.path.exists(path)}
        print(f"Found {len(self.available_voices)} voice files in {voices_dir}")
        print(f"Available voices: {', '.join(self.available_voices.keys())}")

    def get_voice_path(self, speaker_name: str) -> str:
        if speaker_name in self.voice_presets:
            return self.voice_presets[speaker_name]
        speaker_lower = speaker_name.lower()
        for preset_name, path in self.voice_presets.items():
            if preset_name.lower() in speaker_lower or speaker_lower in preset_name.lower():
                return path
        default_voice = list(self.voice_presets.values())[0]
        print(f"Warning: No voice preset found for '{speaker_name}', using default voice: {default_voice}")
        return default_voice

def parse_txt_script(txt_content: str):
    """Parse txt script content and extract speakers and their text"""
    lines = txt_content.strip().split('\n')
    scripts = []
    speaker_numbers = []
    speaker_pattern = r'^Speaker\s+(\d+):\s*(.*)$'
    current_speaker = None
    current_text = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = re.match(speaker_pattern, line, re.IGNORECASE)
        if match:
            if current_speaker and current_text:
                scripts.append(f"Speaker {current_speaker}: {current_text.strip()}")
                speaker_numbers.append(current_speaker)
            current_speaker = match.group(1).strip()
            current_text = match.group(2).strip()
        else:
            if current_text:
                current_text += " " + line
            else:
                current_text = line
    if current_speaker and current_text:
        scripts.append(f"Speaker {current_speaker}: {current_text.strip()}")
        speaker_numbers.append(current_speaker)
    return scripts, speaker_numbers

class TxtFileHandler(FileSystemEventHandler):
    def __init__(self, model_path, speaker_names, output_dir, device, cfg_scale, dtype):
        self.model_path = model_path
        self.speaker_names = speaker_names
        self.output_dir = output_dir
        self.device = device
        self.cfg_scale = cfg_scale
        self.dtype = dtype
        self.model = None
        self.processor = None
        self.load_model()

    def load_model(self):
        if self.dtype == "float32":
            torch_dtype = torch.float32
        elif self.dtype == "float16":
            torch_dtype = torch.float16
        elif self.dtype == "bfloat16":
            torch_dtype = torch.bfloat16
        else:
            raise ValueError(f"Unsupported dtype: {self.dtype}")
        print("Loading VibeVoice model...")
        self.processor = VibeVoiceProcessor.from_pretrained(self.model_path,)
        self.model = VibeVoiceForConditionalGenerationInference.from_pretrained(self.model_path,torch_dtype=torch_dtype,)
        self.model.to(self.device)
        self.model.eval()
        self.model.set_ddpm_inference_steps(num_steps=10)
        print("Model loaded successfully.")

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith(".txt"):
            return
        print(f"New text file detected: {event.src_path}")
        try:
            self.process_txt_file(event.src_path)
        except Exception as e:
            print(f"Error processing {event.src_path}: {e}")
            print(traceback.format_exc())

    def process_txt_file(self, txt_path):
        print(f"Starting generation with cfg_scale: {self.cfg_scale}")
        with open(txt_path, 'r', encoding='utf-8') as file:
            txt_content = file.read()

        scripts, speaker_numbers = parse_txt_script(txt_content)
        if not scripts:
            print(f"No valid scripts found in {txt_path}")
            return

        voice_mapper = VoiceMapper()
        unique_speakers = sorted(list(set(speaker_numbers)), key=int)
        speaker_paths = [voice_mapper.get_voice_path(self.speaker_names[int(num)-1]) for num in unique_speakers]

        # Combine all scripts into a single string, exactly like the working example
        full_script = '\n'.join(scripts)

        # Prepare inputs
        inputs = self.processor(
            text=[full_script],             # wrap in list
            voice_samples=[speaker_paths],  # wrap in list
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )
        # Move tensors to target device
        for k, v in inputs.items():
            if torch.is_tensor(v):
                inputs[k] = v.to(self.device)

        # Generate audio
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                cfg_scale=self.cfg_scale,
                max_new_tokens=None,
                generation_config={'do_sample': False},
                verbose=True,
                tokenizer=self.processor.tokenizer  # MUST be passed
            )

        # Save audio
        txt_filename = os.path.splitext(os.path.basename(txt_path))[0]
        output_path = os.path.join(self.output_dir, f"{txt_filename}_generated.wav")
        os.makedirs(self.output_dir, exist_ok=True)
        self.processor.save_audio(outputs.speech_outputs[0], output_path=output_path)
        print(f"Generated audio saved to {output_path}")

def main(model_path, speaker_names, output_dir, device, cfg_scale, watch_dir, dtype):
    event_handler = TxtFileHandler(model_path, speaker_names, output_dir, device, cfg_scale, dtype)
    observer = Observer()
    observer.schedule(event_handler, watch_dir, recursive=False)
    observer.start()
    print(f"Watching folder: {watch_dir} for new .txt files...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VibeVoice Watcher")
    parser.add_argument("--model_path", type=str, default="microsoft/VibeVoice-1.5b", help="Path to HuggingFace model directory")
    parser.add_argument("--speaker_names", type=str, nargs='+', default=['boris'], help="Speaker names in order")
    parser.add_argument("--output_dir", type=str, default="./outputs", help="Directory to save output audio files")
    parser.add_argument("--device", type=str, default=("cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")), help="Device for inference: cuda | mps | cpu")
    parser.add_argument("--cfg_scale", type=float, default=1.3, help="CFG scale for generation")
    parser.add_argument("--watch_dir", type=str, default="./txt", help="Directory to watch for new text files")
    parser.add_argument(
    "--dtype",
    type=str,
    default="float32",
    choices=["float32", "float16", "bfloat16"],
    help="Torch dtype to use for model (default: float32)",
)
    args = parser.parse_args()

    main(args.model_path, args.speaker_names, args.output_dir, args.device, args.cfg_scale, args.watch_dir, args.dtype)
