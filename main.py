from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio
import os
import random
import time

# ========== CONFIG (from environment variables for Railway) ==========
STRING_SESSION = os.environ.get('STRING_SESSION', '')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
# Optional: set BOT_ID to a unique number (1-5) for each bot to stagger timing
BOT_ID = int(os.environ.get('BOT_ID', 1))
# ============================

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

bot_entity = None
sticker_msg_id = None

# State machine
STATE_IDLE = 'idle'
STATE_FINDING = 'finding'
STATE_MATCHED = 'matched'
STATE_WAITING_PARTNER = 'waiting_partner'

current_state = STATE_IDLE
state_lock = asyncio.Lock()
partner_skipped = False
last_processed_msg_id = 0
last_click_time = 0

# Anti-self-match: track recent partner IDs to avoid matching same bot
recent_partner_ids = set()
MAX_RECENT_PARTNERS = 20


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


async def click_find_partner():
    global current_state, last_click_time

    async with state_lock:
        if current_state in (STATE_MATCHED, STATE_WAITING_PARTNER):
            print(f"[*] In match (state={current_state}), skipping Find a Partner click")
            return False

        now = time.time()
        if now - last_click_time < 5:
            print(f"[*] Click cooldown active ({now - last_click_time:.1f}s), skipping...")
            return False
        last_click_time = now

        if current_state == STATE_FINDING:
            print("[*] Already finding partner, skipping...")
            return False

        current_state = STATE_FINDING

    # ANTI-SELF-MATCH: staggered random delay based on BOT_ID
    # Each bot gets a different base delay so they rarely sync up
    base_delay = BOT_ID * 1.5  # Bot 1=1.5s, Bot 2=3s, Bot 3=4.5s, etc.
    random_delay = random.uniform(0, 3)
    total_delay = base_delay + random_delay
    print(f"[*] Anti-self-match: waiting {total_delay:.1f}s before clicking (bot_id={BOT_ID})...")
    await asyncio.sleep(total_delay)

    # Re-check state after delay
    async with state_lock:
        if current_state in (STATE_MATCHED, STATE_WAITING_PARTNER):
            print(f"[*] State changed to match during delay, aborting click")
            return False

    print("[*] Looking for Find a Partner button...")

    try:
        for attempt in range(5):
            async with state_lock:
                if current_state in (STATE_MATCHED, STATE_WAITING_PARTNER):
                    print(f"[*] State changed to match during search, aborting click")
                    return False

            msgs = await client.get_messages(bot_entity, limit=15)
            for m in msgs:
                if not m.reply_markup:
                    continue
                for row in m.reply_markup.rows:
                    for btn in row.buttons:
                        btn_text = btn.text or ''
                        if 'Find a Partner' in btn_text or 'Find' in btn_text:
                            try:
                                await m.click(text=btn.text)
                                print(f"[→] Find a Partner clicked (attempt {attempt+1})")
                                await asyncio.sleep(3)
                                return True
                            except Exception as click_err:
                                print(f"[!] Click error: {click_err}")
                                continue

            if attempt < 4:
                print(f"[*] Button not found, waiting... (attempt {attempt+1})")
                await asyncio.sleep(2)

        async with state_lock:
            if current_state == STATE_FINDING:
                print("[!] Button not found, using /search fallback")
                await client.send_message(bot_entity, '/search')
                await asyncio.sleep(3)
                return True

    except Exception as e:
        print(f"[!] Find partner error: {e}")
        async with state_lock:
            if current_state == STATE_FINDING:
                current_state = STATE_IDLE

    return False


async def handle_match():
    global current_state, partner_skipped, recent_partner_ids

    async with state_lock:
        if current_state != STATE_MATCHED:
            print(f"[*] Not in match (state={current_state}), aborting handle_match")
            return
        current_state = STATE_WAITING_PARTNER
        partner_skipped = False

    print("[*] Forwarding sticker...")
    try:
        if sticker_msg_id:
            await client.forward_messages(bot_entity, sticker_msg_id, 'me')
            print("[+] Sticker forwarded!")
        else:
            await client.send_message(bot_entity, "💜 @chatxbt_bot\nhttps://t.me/chatxbt_bot")
            print("[+] Text promo sent!")
    except Exception as e:
        print(f"[!] Sticker error: {e}")

    print("[*] Waiting 3 seconds for partner response...")
    await asyncio.sleep(3)

    async with state_lock:
        skipped = partner_skipped
        state = current_state

    if skipped:
        print("[✓] Partner skipped us (sent message), finding new match in 3 seconds...")
        await asyncio.sleep(3)
        async with state_lock:
            current_state = STATE_IDLE
        await click_find_partner()
        return

    if state != STATE_WAITING_PARTNER:
        print(f"[*] State changed to {state} during wait, aborting")
        return

    print("[*] Partner didn't skip, sending /stop...")
    try:
        await client.send_message(bot_entity, '/stop')
        print("[→] /stop sent")
    except Exception as e:
        print(f"[!] Stop error: {e}")

    await asyncio.sleep(2)

    async with state_lock:
        current_state = STATE_IDLE

    await click_find_partner()


async def handle_finding_timeout():
    global current_state
    await asyncio.sleep(10)

    try:
        async with state_lock:
            state = current_state

        if state != STATE_FINDING:
            return

        print("[!] Finding timeout! No match after 10 seconds.")

        await client.send_message(bot_entity, '/stop')
        print("[→] /stop sent (timeout)")
        await asyncio.sleep(2)

        async with state_lock:
            current_state = STATE_IDLE

        await click_find_partner()
    except Exception as e:
        print(f"[!] Finding timeout error: {e}")


async def recovery_watchdog():
    global current_state
    while True:
        await asyncio.sleep(30)

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
    global current_state, partner_skipped, last_processed_msg_id, recent_partner_ids

    if event.id <= last_processed_msg_id:
        return
    last_processed_msg_id = event.id

    text = event.text or ''

    if event.out:
        return

    # ========== COMMAND NOT AVAILABLE IN CHAT ==========
    if 'This command is not available in chat' in text:
        print("[!] Command not available in chat — we are still in a match!")

        async with state_lock:
            current_state = STATE_MATCHED

        await asyncio.sleep(1)
        try:
            await client.send_message(bot_entity, '/stop')
            print("[→] /stop sent (recovery)")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"[!] Recovery /stop error: {e}")

        async with state_lock:
            current_state = STATE_IDLE

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
            partner_skipped = False

        asyncio.create_task(handle_match())
        return

    # ========== PARTNER SENT MESSAGE DURING MATCH ==========
    async with state_lock:
        state = current_state

    if state == STATE_WAITING_PARTNER:
        print("[+] Partner sent message/sticker — they skipped us!")
        async with state_lock:
            partner_skipped = True
        return

    if state == STATE_MATCHED:
        print("[+] Partner sent message before our sticker!")
        async with state_lock:
            partner_skipped = True
        return


async def main():
    global bot_entity
    await client.start()
    print(f"[*] xbt1-bot (Anonymouslyrobot) started! BOT_ID={BOT_ID}")

    bot_entity = await client.get_entity('@Anonymouslyrobot')
    await find_sticker()
    await click_find_partner()

    asyncio.create_task(recovery_watchdog())

    await client.run_until_disconnected()


with client:
    client.loop.run_until_complete(main())
