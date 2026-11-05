
import os
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")

if not account_sid or not auth_token:
    raise ValueError("Missing Twilio credentials in .env")

client = Client(account_sid, auth_token)

to_number = input(
    "Enter your verified phone number (+country code, no spaces): "
).strip().replace(" ", "")

from_number = os.getenv("TWILIO_PHONE_NUMBER")

call = client.calls.create(
    to=to_number,
    from_=from_number,
    url="https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech",
)

print("Call requested!")
print("Call SID:", call.sid)
print("Status:", call.status)
