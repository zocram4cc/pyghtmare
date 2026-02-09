import argparse
import os
import time
import torch
import re
import sys
import traceback
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Add CosyVoice to path
current_dir = os.path.dirname(os.path.abspath(__file__))
cosyvoice_dir = os.path.join(current_dir, "CosyVoice")
sys.path.append(cosyvoice_dir)
sys.path.append(os.path.join(cosyvoice_dir, "third_party/Matcha-TTS"))

from cosyvoice.cli.cosyvoice import AutoModel
import torchaudio
import soundfile as sf
import numpy as np
import logging
from transformers.utils import logging as hf_logging

# Silence noisy loggers AFTER imports to override any library basicConfig calls
logging.getLogger('watchdog').setLevel(logging.WARNING)
logging.getLogger('cosyvoice').setLevel(logging.WARNING)
logging.root.setLevel(logging.WARNING)
hf_logging.set_verbosity_warning()

class VoiceMapper:
    """Maps speaker names to voice file paths and reference texts"""

    def __init__(self):
        self.setup_voice_presets()

    def setup_voice_presets(self):
        voices_dir = os.path.join(os.path.dirname(__file__), "voices_cut")
        if not os.path.exists(voices_dir):
            print(f"Warning: Voices directory not found at {voices_dir}")
            self.voice_presets = {}
            return

        self.voice_presets = {}
        wav_files = [f for f in os.listdir(voices_dir)
                     if f.lower().endswith('.wav') and os.path.isfile(os.path.join(voices_dir, f))]
        
        for wav_file in wav_files:
            name = os.path.splitext(wav_file)[0]
            full_path = os.path.join(voices_dir, wav_file)
            self.voice_presets[name] = full_path

        self.voice_presets = dict(sorted(self.voice_presets.items()))
        print(f"Found {len(self.voice_presets)} voice files in {voices_dir}")
        print(f"Available voices: {', '.join(self.voice_presets.keys())}")

    def get_voice_info(self, speaker_name: str):
        # Find path
        wav_path = None
        if speaker_name in self.voice_presets:
            wav_path = self.voice_presets[speaker_name]
        else:
            speaker_lower = speaker_name.lower()
            for preset_name, path in self.voice_presets.items():
                if preset_name.lower() in speaker_lower or speaker_lower in preset_name.lower():
                    wav_path = path
                    break
        
        if not wav_path:
            wav_path = list(self.voice_presets.values())[0]
            print(f"Warning: No voice preset found for '{speaker_name}', using default voice: {wav_path}")

        # Find reference text
        txt_path = os.path.splitext(wav_path)[0] + ".txt"
        ref_text = "Welcome to the world of voice generation." # Default fallback
        
        if os.path.exists(txt_path):
            try:
                with open(txt_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        ref_text = content
            except Exception as e:
                print(f"Error reading reference text {txt_path}: {e}")
        
        return wav_path, ref_text

def clean_text(text: str) -> str:
    """Remove emojis and excessive special characters while preserving TTS control tags."""
    # 1. Identify and protect tags like <slow>, <angry>, </slow>, etc.
    tag_placeholder = " [[[TAG_{}]]] "
    tags = re.findall(r'<[^>]+>', text)
    for i, tag in enumerate(tags):
        text = text.replace(tag, tag_placeholder.format(i))

    # 2. Perform cleaning on the rest of the text
    # Keep alphanumeric, basic punctuation, and whitespace
    text = re.sub(r'[^\x00-\x7F]+', ' ', text) 
    # Remove sequences of symbols that aren't useful for speech (e.g. symbol art)
    text = re.sub(r'[\x21-\x2F\x3A-\x40\x5B-\x60\x7B-\x7E]{3,}', ' ', text)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text)

    # 3. Restore protected tags
    for i, tag in enumerate(tags):
        text = text.replace(tag_placeholder.format(i), tag)

    return text.strip()

def parse_txt_script(txt_content: str):
    """Parse txt script content and extract segments: [{'speaker_num': '1', 'text': '...'}]"""
    lines = txt_content.strip().split('\n')
    segments = []
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
                cleaned = clean_text(current_text)
                if cleaned:
                    segments.append({'speaker_num': current_speaker, 'text': cleaned})
            current_speaker = match.group(1).strip()
            current_text = match.group(2).strip()
        else:
            if current_text:
                current_text += " " + line
            else:
                current_text = line
                
    if current_speaker and current_text:
        cleaned = clean_text(current_text)
        if cleaned:
            segments.append({'speaker_num': current_speaker, 'text': cleaned})
        
    return segments

class TxtFileHandler(FileSystemEventHandler):
    def __init__(self, model_dir, speaker_names, output_dir, device):
        self.model_dir = model_dir
        self.speaker_names = speaker_names
        self.output_dir = output_dir
        self.device = device
        self.model = None
        self.voice_mapper = VoiceMapper()
        self.load_model()
        self.register_speakers()

    def load_model(self):
        print(f"Loading CosyVoice model from {self.model_dir}...")
        # Note: AutoModel in CosyVoice 3 handledevice and fp16 internally usually
        # Enabling fp16=True for potentially faster inference
        self.model = AutoModel(model_dir=self.model_dir, fp16=True)
        print("Model loaded successfully.")

    def register_speakers(self):
        print("Registering speakers...")
        for spk_name, wav_path in self.voice_mapper.voice_presets.items():
            _, ref_text = self.voice_mapper.get_voice_info(spk_name)
            
            # For CosyVoice3, ensure the correct prompt structure
            if '<|endofprompt|>' not in ref_text:
                full_ref_text = f"You are a helpful assistant.<|endofprompt|>{ref_text}"
            else:
                full_ref_text = ref_text
                
            print(f"Registering {spk_name} with ref_text: '{full_ref_text[:30]}...'")
            try:
                self.model.add_zero_shot_spk(full_ref_text, wav_path, spk_name)
            except Exception as e:
                print(f"Error registering speaker {spk_name}: {e}")
        print("All speakers registered.")

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith(".txt"):
            return
        print(f"New text file detected: {event.src_path}")
        time.sleep(0.5) # Wait for file write
        try:
            self.process_txt_file(event.src_path)
        except Exception as e:
            print(f"Error processing {event.src_path}: {e}")
            print(traceback.format_exc())

    def process_txt_file(self, txt_path):
        with open(txt_path, 'r', encoding='utf-8') as file:
            txt_content = file.read()

        segments = parse_txt_script(txt_content)
        if not segments:
            print(f"No valid segments found in {txt_path}")
            return

        generated_wavs = []
        
        for i, seg in enumerate(segments):
            speaker_num = int(seg['speaker_num'])
            text = seg['text']
            
            # Map speaker number to name
            try:
                speaker_name = self.speaker_names[speaker_num - 1]
            except IndexError:
                speaker_name = self.speaker_names[0]
                
            print(f"Generating segment {i+1}/{len(segments)} for {speaker_name} (cached)...")
            
            # Use zero_shot_spk_id for faster inference
            try:
                # stream=True can be used for even lower latency if we had a player
                # but here we concatenate for saving.
                for chunk in self.model.inference_zero_shot(text, '', '', zero_shot_spk_id=speaker_name, stream=True):
                    generated_wavs.append(chunk['tts_speech'])
            except Exception as e:
                print(f"Error during inference for segment {i+1}: {e}")
                traceback.print_exc()

        if generated_wavs:
            # Concatenate
            final_wav = torch.cat(generated_wavs, dim=-1)
            
            # Save audio
            txt_filename = os.path.splitext(os.path.basename(txt_path))[0]
            output_path = os.path.join(self.output_dir, f"{txt_filename}_cosy_generated.wav")
            os.makedirs(self.output_dir, exist_ok=True)
            
            audio_np = final_wav.cpu().numpy()
            if audio_np.ndim > 1:
                audio_np = audio_np.T # (C, T) -> (T, C)
            
            sf.write(output_path, audio_np, self.model.sample_rate)
            os.chmod(output_path, 0o666)
            print(f"Generated audio saved to {output_path}")
        else:
            print("No audio generated.")

def main(model_dir, speaker_names, output_dir, device, watch_dir):
    event_handler = TxtFileHandler(model_dir, speaker_names, output_dir, device)
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
    parser = argparse.ArgumentParser(description="CosyVoice Watcher")
    parser.add_argument("--model_dir", type=str, default="pretrained_models/Fun-CosyVoice3-0.5B", help="Path to model directory")
    parser.add_argument("--speaker_names", type=str, nargs='+', default=['boris', 'crimson', 'sou-hype', 'QD'], help="Speaker names in order")
    parser.add_argument("--output_dir", type=str, default="./outputs", help="Directory to save output audio files")
    parser.add_argument("--device", type=str, default=("cuda" if torch.cuda.is_available() else "cpu"), help="Device")
    parser.add_argument("--watch_dir", type=str, default="./txt", help="Directory to watch")
    
    args = parser.parse_args()
    main(args.model_dir, args.speaker_names, args.output_dir, args.device, args.watch_dir)
