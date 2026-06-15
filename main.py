from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio
import os

# ========== CONFIG (from environment variables for Railway) ==========
STRING_SESSION = os.environ.get('STRING_SESSION', '')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
# ============================

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

bot_entity = None
sticker_msg_id = None
heyyy_msg_id = None
f_msg_id = None

# State machine
STATE_IDLE = 'idle'
STATE_FINDING = 'finding'
STATE_MATCHED = 'matched'
STATE_SENDING_PROMO = 'sending_promo'
STATE_ENDING = 'ending'

current_state = STATE_IDLE
promo_cancelled = False
state_lock = asyncio.Lock()
active_promo_task = None
active_click_task = None  # Track click_find_partner task


async def find_sticker():
    global sticker_msg_id, heyyy_msg_id, f_msg_id
    try:
        msgs = await client.get_messages('me', limit=50)
        for m in msgs:
            if m.sticker and not sticker_msg_id:
                sticker_msg_id = m.id
                print("[+] Sticker found!")
            if m.text and m.text.lower() == 'heyyy' and not heyyy_msg_id:
                heyyy_msg_id = m.id
                print("[+] 'heyyy' message found!")
            if m.text and m.text.upper() == 'F' and not f_msg_id:
                f_msg_id = m.id
                print("[+] 'F' message found!")

        if all([sticker_msg_id, heyyy_msg_id, f_msg_id]):
            return True

    except Exception as e:
        print(f"[!] Find error: {e}")

    print("[!] Send 'heyyy', 'F', and sticker to Saved Messages first!")
    return False


async def _do_click_find_partner():
    """Internal: actually performs the click/find logic."""
    global current_state

    print("[*] Looking for Find a Partner button...")

    try:
        for attempt in range(5):
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
                                return
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

    except Exception as e:
        print(f"[!] Find partner error: {e}")
        async with state_lock:
            if current_state == STATE_FINDING:
                current_state = STATE_IDLE


async def click_find_partner():
    """Wrapper that ensures only ONE click_find_partner runs at a time."""
    global current_state, active_click_task

    async with state_lock:
        # If already finding or clicking, don't start another
        if current_state == STATE_FINDING:
            print("[*] Already finding partner, skipping...")
            return
        # If a click task is already running, cancel it first
        if active_click_task and not active_click_task.done():
            print("[*] Cancelling previous click task...")
            active_click_task.cancel()
            try:
                await active_click_task
            except asyncio.CancelledError:
                pass

        current_state = STATE_FINDING
        active_click_task = asyncio.create_task(_do_click_find_partner())

    try:
        await active_click_task
    except asyncio.CancelledError:
        print("[*] Click task was cancelled")
    finally:
        async with state_lock:
            active_click_task = None


async def _check_state(expected_state):
    """Check if we're still in expected state, return True if ok, False if cancelled."""
    async with state_lock:
        if promo_cancelled or current_state != expected_state:
            return False
    return True


async def send_promo_sequence():
    global current_state, promo_cancelled, active_promo_task

    async with state_lock:
        if current_state != STATE_MATCHED:
            print(f"[*] Not in match (state={current_state}), skipping promo")
            active_promo_task = None
            return
        current_state = STATE_SENDING_PROMO
        promo_cancelled = False

    print("[*] Starting promo sequence...")

    try:
        # Step 1: heyyy
        if not await _check_state(STATE_SENDING_PROMO):
            print("[!] Promo cancelled before heyyy")
            active_promo_task = None
            return

        if heyyy_msg_id:
            await client.forward_messages(bot_entity, heyyy_msg_id, 'me')
        else:
            await client.send_message(bot_entity, "heyyy")
        print("[+] Sent: heyyy")
        await asyncio.sleep(3)

        # Step 2: F
        if not await _check_state(STATE_SENDING_PROMO):
            print("[!] Promo cancelled before F")
            active_promo_task = None
            return

        if f_msg_id:
            await client.forward_messages(bot_entity, f_msg_id, 'me')
        else:
            await client.send_message(bot_entity, "F")
        print("[+] Sent: F")
        await asyncio.sleep(3)

        # Step 3: Sticker
        if not await _check_state(STATE_SENDING_PROMO):
            print("[!] Promo cancelled before sticker")
            active_promo_task = None
            return

        if sticker_msg_id:
            await client.forward_messages(bot_entity, sticker_msg_id, 'me')
            print("[+] Sticker forwarded!")
        else:
            await client.send_message(bot_entity, "💜 @chatxbt_bot\nhttps://t.me/chatxbt_bot")
            print("[+] Text promo sent!")
        await asyncio.sleep(2)

    except Exception as e:
        print(f"[!] Promo error: {e}")

    # After promo, end the chat and find next
    should_end = False
    async with state_lock:
        if current_state == STATE_SENDING_PROMO and not promo_cancelled:
            current_state = STATE_ENDING
            should_end = True

    active_promo_task = None

    if should_end and not promo_cancelled:
        await end_chat_and_find()


async def end_chat_and_find():
    global current_state

    async with state_lock:
        if current_state == STATE_IDLE or current_state == STATE_FINDING:
            print("[*] Not in a chat, skipping /stop")
            await click_find_partner()
            return
        current_state = STATE_ENDING

    try:
        await client.send_message(bot_entity, '/stop')
        print("[→] /stop sent")
        await asyncio.sleep(3)
    except Exception as e:
        print(f"[!] Stop error: {e}")

    async with state_lock:
        current_state = STATE_IDLE

    await click_find_partner()


async def cancel_active_promo():
    global active_promo_task, promo_cancelled

    async with state_lock:
        promo_cancelled = True
        task = active_promo_task

    if task and not task.done():
        print("[!] Cancelling active promo task...")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print("[✓] Promo task cancelled")


async def cancel_active_click():
    global active_click_task

    async with state_lock:
        task = active_click_task

    if task and not task.done():
        print("[!] Cancelling active click task...")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print("[✓] Click task cancelled")


async def handle_finding_timeout():
    global current_state
    await asyncio.sleep(10)

    async with state_lock:
        state = current_state

    if state != STATE_FINDING:
        return

    print("[!] Finding timeout! No match after 10 seconds.")

    try:
        await client.send_message(bot_entity, '/stop')
        print("[→] /stop sent (timeout)")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"[!] Timeout /stop error: {e}")

    async with state_lock:
        current_state = STATE_IDLE

    await click_find_partner()


async def recovery_watchdog():
    global current_state
    while True:
        await asyncio.sleep(30)

        async with state_lock:
            state = current_state
            has_click_task = active_click_task is not None and not active_click_task.done()
            has_promo_task = active_promo_task is not None and not active_promo_task.done()

        if state == STATE_IDLE and not has_click_task and not has_promo_task:
            print("[!] Watchdog: Idle state detected, finding partner...")
            await click_find_partner()


@client.on(events.NewMessage(chats='@Anonymouslyrobot'))
async def handler(event):
    global current_state, promo_cancelled, active_promo_task

    text = event.text or ''

    if event.out:
        return

    # ========== COMMAND NOT AVAILABLE IN CHAT ==========
    if 'This command is not available in chat' in text:
        print("[!] Command not available in chat — we are still in a match!")

        await cancel_active_promo()
        await cancel_active_click()

        async with state_lock:
            current_state = STATE_MATCHED

        await asyncio.sleep(1)
        await end_chat_and_find()
        return

    # ========== PARTNER ENDED CHAT ==========
    if 'Your partner ended the chat' in text:
        print("[✓] Partner ended chat")

        await cancel_active_promo()
        await cancel_active_click()

        async with state_lock:
            current_state = STATE_IDLE

        await asyncio.sleep(2)
        await click_find_partner()
        return

    # ========== WE LEFT CHAT ==========
    if 'You left the chat' in text:
        print("[✓] We left the chat")

        await cancel_active_promo()
        await cancel_active_click()

        async with state_lock:
            current_state = STATE_IDLE

        await asyncio.sleep(2)
        await click_find_partner()
        return

    # ========== BOT WELCOME / MENU ==========
    if "I'm an anonymous chat bot" in text or "Use the menu or enter the" in text:
        print("[*] Bot welcome/menu shown")

        await cancel_active_promo()
        await cancel_active_click()

        async with state_lock:
            if current_state == STATE_MATCHED or current_state == STATE_SENDING_PROMO:
                print("[!] Desync detected: menu shown while in match")
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

        await cancel_active_promo()
        await cancel_active_click()

        async with state_lock:
            current_state = STATE_MATCHED
            promo_cancelled = False

        await asyncio.sleep(1)
        active_promo_task = asyncio.create_task(send_promo_sequence())
        return

    # ========== PARTNER SENT MESSAGE DURING MATCH ==========
    async with state_lock:
        state = current_state

    if state == STATE_MATCHED:
        print("[+] Partner sent message/sticker during match!")
        active_promo_task = asyncio.create_task(send_promo_sequence())
        return


async def main():
    global bot_entity
    await client.start()
    print("[*] xbt1-bot (Anonymouslyrobot) started!")

    bot_entity = await client.get_entity('@Anonymouslyrobot')
    await find_sticker()
    await click_find_partner()

    asyncio.create_task(recovery_watchdog())

    await client.run_until_disconnected()


with client:
    client.loop.run_until_complete(main())
