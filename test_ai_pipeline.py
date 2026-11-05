
import os
import base64
from dotenv import load_dotenv
from groq import Groq
from sarvamai import SarvamAI

load_dotenv()

groq_key = os.getenv("GROQ_API_KEY")
sarvam_key = os.getenv("SARVAM_API_KEY")

if not groq_key or not sarvam_key:
    raise ValueError("Check GROQ_API_KEY and SARVAM_API_KEY in .env")

groq = Groq(api_key=groq_key)
sarvam = SarvamAI(api_subscription_key=sarvam_key)

caller_text = input("Enter what the caller said in Telugu: ").strip()

if not caller_text:
    raise ValueError("Please enter some text.")

response = groq.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[
        {
            "role": "system",
            "content": (
                "You are a friendly Telugu phone assistant. "
                "Reply in natural Telugu script. "
                "Use one short, complete sentence suitable for speech."
            ),
        },
        {"role": "user", "content": caller_text},
    ],
    reasoning_effort="low",
    max_completion_tokens=1000,
)

reply = response.choices[0].message.content
if not reply:
    raise RuntimeError("Groq returned an empty reply.")

print("\nAI reply:")
print(reply)

audio = sarvam.text_to_speech.convert(
    text=reply,
    language_code="te-IN",
    model="bulbul:v3",
    speaker="kavitha",
)

with open("ai_reply.wav", "wb") as f:
    f.write(base64.b64decode(audio.audios[0]))

print("\nSuccess! Telugu reply saved to ai_reply.wav")
