
import os
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise ValueError("GROQ_API_KEY is missing from .env")

client = Groq(api_key=api_key)

audio_path = Path("telugu_test.wav")

if not audio_path.exists():
    raise FileNotFoundError(
        "telugu_test.wav not found. Run test_sarvam.py first."
    )

with audio_path.open("rb") as audio_file:
    transcription = client.audio.transcriptions.create(
        file=audio_file,
        model="whisper-large-v3-turbo",
        language="te",
        response_format="text",
    )

print("Transcribed text:")
print(transcription)
