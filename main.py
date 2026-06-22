from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
import asyncio
import os
import random
import time

# ========== CONFIG (from environment variables for Railway) ==========
STRING_SESSION = os.environ.get('STRING_SESSION', '')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_ID = int(os.environ.get('BOT_ID', 1))
# ============================

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

bot_entity = None
sticker_msg_id = None

# State machine
STATE_IDLE = 'idle'
STATE_FINDING = 'finding'
STATE_MATCHED = 'matched'
STATE_PROMO_SENT = 'promo_sent'

current_state = STATE_IDLE
state_lock = asyncio.Lock()
match_start_time = 0
last_click_time = 0

# Timeouts
FINDING_TIMEOUT = 20
MATCH_STUCK_TIMEOUT = 60
RECOVERY_INTERVAL = 60


async def safe_send_message(entity, message, retries=3):
    for attempt in range(retries):
        try:
            return await client.send_message(entity, message)
        except FloodWaitError as e:
            print(f"[!] FloodWait: Waiting {e.seconds} seconds...")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f"[!] Send error (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
    return None


async def safe_forward_messages(entity, msg_id, from_peer, retries=3):
    for attempt in range(retries):
        try:
            return await client.forward_messages(entity, msg_id, from_peer)
        except FloodWaitError as e:
            print(f"[!] FloodWait: Waiting {e.seconds} seconds...")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f"[!] Forward error (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
    return None


async def safe_click(message, text, retries=3):
    for attempt in range(retries):
        try:
            return await message.click(text=text)
        except FloodWaitError as e:
            print(f"[!] FloodWait on click: Waiting {e.seconds} seconds...")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f"[!] Click error (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
    return None


async def find_sticker():
    global sticker_msg_id
    try:
        msgs = await client.get_messages('me', limit=50)
        for m in msgs:
            if m.sticker and not sticker_msg_id:
                sticker_msg_id = m.id
                print("[+] Sticker found!")

        if sticker_msg_id:
            return True

    except Exception as e:
        print(f"[!] Find error: {e}")

    print("[!] Send a sticker to Saved Messages first!")
    return False


async def click_find_partner_button(limit=15):
    """Click Find a Partner button. Returns True if clicked."""
    try:
        msgs = await client.get_messages(bot_entity, limit=limit)
        for m in msgs:
            if not m.reply_markup:
                continue
            for row in m.reply_markup.rows:
                for btn in row.buttons:
                    btn_text = (btn.text or '').lower()
                    if 'find a partner' in btn_text or 'find partner' in btn_text:
                        try:
                            await m.click(text=btn.text)
                            print(f"[→] Clicked: {btn.text}")
                            await asyncio.sleep(3)
                            return True
                        except Exception as e:
                            print(f"[!] Click error: {e}")
                            continue
    except Exception as e:
        print(f"[!] Button search error: {e}")
    return False


async def click_find_partner():
    global current_state, last_click_time

    async with state_lock:
        if current_state in (STATE_MATCHED, STATE_PROMO_SENT):
            print(f"[*] In match (state={current_state}), skipping Find a Partner click")
            return False

        now = time.time()
        if now - last_click_time < 7:
            print(f"[*] Click cooldown active ({now - last_click_time:.1f}s), skipping...")
            return False
        last_click_time = now

        if current_state == STATE_FINDING:
            print("[*] Already finding partner, skipping...")
            return False

        current_state = STATE_FINDING

    # ANTI-SELF-MATCH: staggered random delay based on BOT_ID
    base_delay = (BOT_ID - 1) * 2
    random_delay = random.uniform(0, 3)
    total_delay = base_delay + random_delay
    print(f"[*] Anti-self-match: waiting {total_delay:.1f}s before clicking (bot_id={BOT_ID})...")
    await asyncio.sleep(total_delay)

    # Re-check state after delay
    async with state_lock:
        if current_state in (STATE_MATCHED, STATE_PROMO_SENT):
            print(f"[*] State changed to match during delay, aborting click")
            return False

    print("[*] Looking for Find a Partner button...")

    # Try button first
    if await click_find_partner_button(limit=15):
        return True

    # Fallback to /search
    async with state_lock:
        if current_state == STATE_FINDING:
            print("[!] Button not found, using /search fallback")
            await safe_send_message(bot_entity, '/search')
            await asyncio.sleep(3)
            return True

    return False


async def handle_match():
    global current_state

    async with state_lock:
        if current_state != STATE_MATCHED:
            print(f"[*] Not in match (state={current_state}), aborting handle_match")
            return
        current_state = STATE_PROMO_SENT

    print("[*] Forwarding sticker...")
    try:
        if sticker_msg_id:
            await safe_forward_messages(bot_entity, sticker_msg_id, 'me')
            print("[+] Sticker forwarded!")
        else:
            await safe_send_message(bot_entity, "💜 @chatxbt_bot\nhttps://t.me/chatxbt_bot")
            print("[+] Text promo sent!")
    except Exception as e:
        print(f"[!] Sticker error: {e}")

    # Wait 2 seconds
    print("[*] Waiting 2 seconds...")
    await asyncio.sleep(2)

    # CRITICAL: Re-check state before sending /stop
    async with state_lock:
        state = current_state

    if state != STATE_PROMO_SENT:
        print(f"[*] State changed to {state} during wait, aborting /stop")
        return

    # Send /stop to end chat
    print("[→] Sending /stop to end chat...")
    await safe_send_message(bot_entity, '/stop')

    # Wait for bot to process /stop
    await asyncio.sleep(3)

    # Now find new partner
    async with state_lock:
        current_state = STATE_IDLE

    await click_find_partner()


async def handle_finding_timeout():
    global current_state
    await asyncio.sleep(FINDING_TIMEOUT)

    try:
        async with state_lock:
            state = current_state

        if state != STATE_FINDING:
            return

        print(f"[!] Finding timeout! No match after {FINDING_TIMEOUT} seconds.")

        async with state_lock:
            current_state = STATE_IDLE

        await click_find_partner()
    except Exception as e:
        print(f"[!] Finding timeout error: {e}")


async def stuck_watchdog():
    global current_state
    await asyncio.sleep(MATCH_STUCK_TIMEOUT)

    try:
        async with state_lock:
            state = current_state

        # Check both MATCHED and PROMO_SENT states
        if state not in (STATE_MATCHED, STATE_PROMO_SENT):
            return

        elapsed = time.time() - match_start_time
        if elapsed >= MATCH_STUCK_TIMEOUT:
            print(f"[!] MATCH STUCK for {elapsed:.0f}s, forcing /stop and next...")

            async with state_lock:
                current_state = STATE_IDLE

            await safe_send_message(bot_entity, '/stop')
            await asyncio.sleep(3)
            await click_find_partner()
    except Exception as e:
        print(f"[!] Stuck watchdog error: {e}")


async def recovery_watchdog():
    global current_state
    while True:
        await asyncio.sleep(RECOVERY_INTERVAL)

        try:
            async with state_lock:
                state = current_state

            if state == STATE_IDLE:
                print("[!] Watchdog: Idle state detected, finding partner...")
                await click_find_partner()
        except Exception as e:
            print(f"[!] Watchdog error: {e}")


@client.on(events.NewMessage(chats='@Anonymouslyrobot'))
async def handler(event):
    global current_state, match_start_time

    text = event.text or ''

    if event.out:
        return

    # ========== COMMAND NOT AVAILABLE IN CHAT ==========
    if 'This command is not available in chat' in text:
        print("[!] Command not available in chat — forcing recovery...")

        async with state_lock:
            old_state = current_state
            current_state = STATE_IDLE

        print(f"[*] State was {old_state}, forced to idle. Sending /stop...")
        await safe_send_message(bot_entity, '/stop')
        await asyncio.sleep(3)
        await click_find_partner()
        return

    # ========== PARTNER ENDED CHAT ==========
    if 'Your partner ended the chat' in text:
        print("[✓] Partner ended chat")

        async with state_lock:
            current_state = STATE_IDLE

        await asyncio.sleep(2)
        await click_find_partner()
        return

    # ========== WE LEFT CHAT ==========
    if 'You left the chat' in text:
        print("[✓] We left the chat")

        async with state_lock:
            current_state = STATE_IDLE

        await asyncio.sleep(2)
        await click_find_partner()
        return

    # ========== BOT WELCOME / MENU ==========
    if "I'm an anonymous chat bot" in text or "Use the menu or enter the" in text:
        print("[*] Bot welcome/menu shown")

        async with state_lock:
            current_state = STATE_IDLE

        await asyncio.sleep(1)
        await click_find_partner()
        return

    # ========== FINDING PARTNER ==========
    if 'Finding a partner soon' in text:
        print("[...] Searching for partner...")

        async with state_lock:
            current_state = STATE_FINDING

        asyncio.create_task(handle_finding_timeout())
        return

    # ========== MATCH STARTED ==========
    if 'Start chatting' in text:
        print("[+] Match started!")

        async with state_lock:
            current_state = STATE_MATCHED
            match_start_time = time.time()

        # Start stuck watchdog
        asyncio.create_task(stuck_watchdog())

        # Start promo
        asyncio.create_task(handle_match())
        return

    # ========== PARTNER SENT MESSAGE DURING MATCH ==========
    async with state_lock:
        state = current_state

    if state == STATE_MATCHED:
        # Partner messaged before we sent sticker
        print("[+] Partner sent message before our sticker!")
        return

    if state == STATE_PROMO_SENT:
        # Partner messaged after sticker — they might have skipped
        # Our /stop is already sent or will be sent, just wait
        print("[+] Partner sent message after sticker")
        return


async def main():
    global bot_entity
    await client.start()
    print(f"[*] xbt1-bot (Anonymouslyrobot) started! BOT_ID={BOT_ID}")
    print(f"[*] FINDING_TIMEOUT={FINDING_TIMEOUT}s | MATCH_STUCK_TIMEOUT={MATCH_STUCK_TIMEOUT}s")
    print("[*] Flow: sticker → 2s → /stop → 3s → Find a Partner")
    print("[*] Connected to Telegram successfully!")

    bot_entity = await client.get_entity('@Anonymouslyrobot')
    await find_sticker()
    await click_find_partner()

    asyncio.create_task(recovery_watchdog())

    await client.run_until_disconnected()


if __name__ == '__main__':
    while True:
        try:
            with client:
                client.loop.run_until_complete(main())
        except KeyboardInterrupt:
            print("\n[*] Bot stopped by user.")
            break
        except Exception as e:
            print(f"[!] Fatal error: {e}")
            print("[*] Restarting in 10 seconds...")
            time.sleep(10)
