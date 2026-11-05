
import os
import base64
from dotenv import load_dotenv
from sarvamai import SarvamAI

load_dotenv()

api_key = os.getenv("SARVAM_API_KEY")
if not api_key:
    raise ValueError("SARVAM_API_KEY is missing from .env")

client = SarvamAI(api_subscription_key=api_key)

response = client.text_to_speech.convert(
    text="నమస్కారం! మీకు స్వాగతం.",
    language_code="te-IN",
    model="bulbul:v3",
    speaker="kavitha",
)

with open("telugu_test.wav", "wb") as audio_file:
    audio_file.write(base64.b64decode(response.audios[0]))

print("Success! Telugu audio saved to telugu_test.wav")
