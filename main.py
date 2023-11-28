#!/usr/bin/env python3
from pathlib import Path
import argparse
import os
import re
import subprocess
import tempfile
import yaml
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import uvicorn
from pydantic import BaseModel
from TTS.api import TTS

xtts = None
args = None
app = FastAPI()

class xtts_wrapper():
    def __init__(self, model_name):
        self.xtts = TTS(model_name=model_name, progress_bar=False).to(args.xtts_device)

    def tts(self, text, speaker_wav, speed):
        tf, file_path = tempfile.mkstemp(suffix='.wav')

        file_path = self.xtts.tts_to_file(
            text,
            language='en',
            speaker_wav=speaker_wav,
            speed=speed,
            file_path=file_path,
        )
        
        os.unlink(file_path)
        return tf

# Read pre process map on demand so it can be changed without restarting the server
def preprocess(raw_input):
    with open('pre_process_map.yaml', 'r') as file:
        pre_process_map = yaml.safe_load(file)
        for a, b in pre_process_map:
            raw_input = re.sub(a, b, raw_input)
    return raw_input

# Read voice map on demand so it can be changed without restarting the server
def map_voice_to_speaker(voice: str, model: str):
    with open('voice_to_speaker.yaml', 'r') as file:
        voice_map = yaml.safe_load(file)
        return voice_map[model][voice]['model'], voice_map[model][voice]['speaker'], 

class GenerateSpeechRequest(BaseModel):
    model: str = "tts-1" # or "tts-1-hd"
    input: str
    voice: str = "alloy"  # alloy, echo, fable, onyx, nova, and shimmer
    response_format: str = "mp3" # mp3, opus, aac, flac
    speed: float = 1.0 # 0.25 - 4.0

@app.post("/v1/audio/speech")
async def generate_speech(request: GenerateSpeechRequest):
    input_text = preprocess(request.input)
    model = request.model
    voice = request.voice
    response_format = request.response_format
    speed = request.speed

    # Set the Content-Type header based on the requested format
    if response_format == "mp3":
        media_type = "audio/mpeg"
    elif response_format == "opus":
        media_type = "audio/ogg;codecs=opus"
    elif response_format == "aac":
        media_type = "audio/aac"
    elif response_format == "flac":
        media_type = "audio/x-flac"

    # Convert the output to the desired format using ffmpeg
    ffmpeg_args = ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "-"]

    if response_format == "mp3":
        ffmpeg_args.extend(["-f", "mp3", "-c:a", "libmp3lame", "-ab", "64k"])
    elif response_format == "opus":
        ffmpeg_args.extend(["-f", "ogg", "-c:a", "libopus"])
    elif response_format == "aac":
        ffmpeg_args.extend(["-f", "adts", "-c:a", "aac", "-ab", "64k"])
    elif response_format == "flac":
        ffmpeg_args.extend(["-f", "flac", "-c:a", "flac"])

    tts_io_out = None

    # Use piper for tts-1, and if xtts_device == none use for all models.
    if model == 'tts-1' or args.xtts_device == 'none':
        piper_model, speaker = map_voice_to_speaker(voice, 'tts-1')
        tts_args = ["piper", "--model", str(piper_model), "--data-dir", "voices", "--download-dir", "voices", "--output-raw"]
        if args.piper_cuda:
            tts_args.extend(["--cuda"])
        if speaker:
            tts_args.extend(["--speaker", str(speaker)])
        if speed != 1.0:
            tts_args.extend(["--length-scale", f"{1.0/speed}"])

        tts_proc = subprocess.Popen(tts_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        tts_proc.stdin.write(bytearray(input_text.encode('utf-8')))
        tts_proc.stdin.close()
        tts_io_out = tts_proc.stdout
        
    # Use xtts_v2 for tts-1-hd
    elif model == 'tts-1-hd':
        if not xtts:
            xtts = xtts_wrapper("tts_models/multilingual/multi-dataset/xtts_v2")

        tts_model, speaker = model, speaker = map_voice_to_speaker(voice, 'tts-1-hd')

        if speed > 2.0: # tts has a max speed of 2.0
            ffmpeg_args.extend(["-af", "atempo=2.0"]) 
            speed = min(speed / 2.0, 2.0)

        tts_io_out = xtts.tts(text=input_text, speaker_wav=speaker, speed=speed)
    
    # Pipe the output from piper/xtts to the input of ffmpeg
    ffmpeg_args.extend(["-"])
    ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdin=tts_io_out, stdout=subprocess.PIPE)

    return StreamingResponse(content=ffmpeg_proc.stdout, media_type=media_type)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog='main.py',
        description='OpenedAI Speech API Server',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    parser.add_argument('--piper_cuda', action='store_true', default=False, help="Enable cuda for piper. Note: --cuda/onnxruntime-gpu is not working for me, but cpu is fast enough") 
    parser.add_argument('--xtts_device', action='store', default="cuda", help="Set the device for the xtts model. The special value of 'none' will use piper for all models.")
    parser.add_argument('--preload_xtts', action='store_true', default=False, help="Preload the xtts model. By default it's loaded on first use.")
    parser.add_argument('-P', '--port', action='store', default=8000, help="Server tcp port")
    parser.add_argument('-H', '--host', action='store', default='localhost', help="Host to listen on, Ex. 0.0.0.0")

    args = parser.parse_args()

    if args.preload_xtts:
        xtts = xtts_wrapper("tts_models/multilingual/multi-dataset/xtts_v2")

    uvicorn.run(app, host=args.host, port=args.port) # , root_path=cwd, access_log=False, log_level="info", ssl_keyfile="cert.pem", ssl_certfile="cert.pem")