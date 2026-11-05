
import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise ValueError("GROQ_API_KEY is missing from .env")

client = Groq(api_key=api_key)

response = client.chat.completions.create(
    model="llama-3.1-8b-instant",
    messages=[
        {
            "role": "system",
            "content": (
                "You are a friendly Telugu-speaking phone assistant. "
                "Always reply in natural Telugu script. "
                "Use one short, complete sentence suitable for a phone call."
            ),
        },
        {
            "role": "user",
            "content": "నమస్కారం, మీరు నాకు ఎలా సహాయం చేయగలరు?",
        },
    ],
    reasoning_effort="low",
    max_completion_tokens=1000,
)

choice = response.choices[0]
reply = choice.message.content

print("AI Telugu reply:")
print(reply if reply else "[Empty response]")
print("Finish reason:", choice.finish_reason)
