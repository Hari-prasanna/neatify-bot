import asyncio
import os
from telegram import Bot
from dotenv import load_dotenv

# 1. Load the secret keys from the .env file
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")

async def main():
    # 2. Initialize the Bot (The Waitress)
    bot = Bot(token=TOKEN)
    
    print("Attempting to send a message...")
    
    try:
        # 3. Send a test message (Testing the microphone)
        await bot.send_message(
            chat_id=GROUP_ID, 
            text="🚀 Roomie Bot Connection Test: SUCCESS! I am ready to track some cleaning."
        )
        print("Message sent successfully!")
    except Exception as e:
        print(f"Failed to send message. Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())