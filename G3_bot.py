import telebot
import sqlite3
import datetime
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
import os
import logging
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Setup logging to file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
MERCHANT_ID = os.getenv('MERCHANT_ID')
PRICE = int(os.getenv('PRICE'))
ZARINPAL_CALLBACK_URL = os.getenv('ZARINPAL_CALLBACK_URL')

# Initialize bot
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Database connection
conn = sqlite3.connect('members.db', check_same_thread=False)
cursor = conn.cursor()

# Create table if it doesn't exist
cursor.execute('''
    CREATE TABLE IF NOT EXISTS members (
        user_id INTEGER PRIMARY KEY,
        join_date DATE,
        is_admin INTEGER,
        last_payment_date DATE,
        days_remaining INTEGER
    )
''')

# Add missing columns if they don't exist
try:
    cursor.execute("ALTER TABLE members ADD COLUMN invite_link TEXT")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE members ADD COLUMN invite_expiry INTEGER")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE members ADD COLUMN payment_status TEXT")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE members ADD COLUMN authority TEXT")
except sqlite3.OperationalError:
    pass

conn.commit()

# Function to generate ZarinPal payment link
def generate_payment_link(user_id):
    url = "https://api.zarinpal.com/pg/v4/payment/request.json"
    callback_url = ZARINPAL_CALLBACK_URL
    data = {
        "merchant_id": MERCHANT_ID,
        "amount": PRICE,
        "description": f"Payment for channel membership - User {user_id}",
        "callback_url": callback_url
    }
    try:
        response = requests.post(url, json=data)
        response_data = response.json()
        logger.info(f"ZarinPal response for user {user_id}: {response_data}")
        
        if 'data' in response_data and 'code' in response_data['data'] and response_data['data']['code'] == 100:
            authority = response_data['data']['authority']
            payment_url = f"https://www.zarinpal.com/pg/StartPay/{authority}"
            cursor.execute("""
                INSERT OR REPLACE INTO members (user_id, payment_status, authority)
                VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET payment_status = ?, authority = ?
            """, (user_id, 'pending', authority, 'pending', authority))
            conn.commit()
            return payment_url, authority
        else:
            error_message = response_data.get('errors', {}).get('message', 'Unknown error')
            logger.error(f"Failed to generate payment link for user {user_id}: {error_message}")
            return None, None
    except Exception as e:
        logger.error(f"Error generating payment link for user {user_id}: {e}")
        return None, None

# Function to verify payment
def verify_payment(user_id, authority):
    url = "https://api.zarinpal.com/pg/v4/payment/verify.json"
    data = {
        "merchant_id": MERCHANT_ID,
        "amount": PRICE,
        "authority": authority
    }
    try:
        response = requests.post(url, json=data)
        response_data = response.json()
        if 'data' in response_data and 'code' in response_data['data'] and response_data['data']['code'] == 100:
            cursor.execute("UPDATE members SET payment_status = ? WHERE user_id = ?", ('success', user_id))
            conn.commit()
            logger.info(f"Payment verified for user {user_id}")
            # Generate and send invite link as inline button
            invite_expiry = int((datetime.datetime.now() + datetime.timedelta(days=1)).timestamp())
            invite_link = bot.create_chat_invite_link(CHAT_ID, member_limit=1, expire_date=invite_expiry)
            cursor.execute("UPDATE members SET invite_link = ?, invite_expiry = ? WHERE user_id = ?", 
                           (invite_link.invite_link, invite_expiry, user_id))
            conn.commit()
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Join Channel", url=invite_link.invite_link))
            bot.send_message(user_id, f"Payment successful! Click the button below to join the channel:", reply_markup=markup)
            logger.info(f"Sent invite link to user {user_id}: {invite_link.invite_link}")
            return True
        else:
            cursor.execute("UPDATE members SET payment_status = ? WHERE user_id = ?", ('failed', user_id))
            conn.commit()
            logger.error(f"Payment verification failed for user {user_id}: {response_data}")
            bot.send_message(user_id, "Payment failed. Please try again.")
            return False
    except Exception as e:
        logger.error(f"Error verifying payment for user {user_id}: {e}")
        return False

# HTTP Server for ZarinPal callback
class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urlparse(self.path).query
        params = parse_qs(query)
        authority = params.get('Authority', [None])[0]
        status = params.get('Status', [None])[0]
        
        if authority and status == 'OK':
            user_data = cursor.execute("SELECT user_id FROM members WHERE authority = ? AND payment_status = 'pending'", (authority,)).fetchone()
            if user_data:
                user_id = user_data[0]
                if verify_payment(user_id, authority):
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b"Payment verified")
                else:
                    self.send_response(400)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b"Payment verification failed")
            else:
                self.send_response(404)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"No pending payment found for this authority")
        else:
            self.send_response(400)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Invalid callback parameters")

def run_callback_server():
    server_address = ('127.252.4.177', 8001)
    httpd = HTTPServer(server_address, CallbackHandler)
    logger.info("Starting callback server on 127.252.4.177:8001")
    httpd.serve_forever()

# Function to sync channel members (admins and non-admins)
def sync_channel_members():
    try:
        admins = bot.get_chat_administrators(CHAT_ID)
        admin_ids = {admin.user.id for admin in admins}
        logger.info(f"Found {len(admins)} administrators")
        join_date = datetime.date.today() - datetime.timedelta(days=50)

        for admin in admins:
            user_id = admin.user.id
            if not cursor.execute("SELECT * FROM members WHERE user_id = ?", (user_id,)).fetchone():
                cursor.execute("""
                    INSERT INTO members (user_id, join_date, is_admin, last_payment_date, days_remaining)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, join_date, 1, None, 30))
                logger.info(f"Added admin {user_id} to database with join_date {join_date}")

        non_admin_ids = [111111111, 222222222, 333333333]
        for user_id in non_admin_ids:
            if user_id not in admin_ids:
                if not cursor.execute("SELECT * FROM members WHERE user_id = ?", (user_id,)).fetchone():
                    cursor.execute("""
                        INSERT INTO members (user_id, join_date, is_admin, last_payment_date, days_remaining)
                        VALUES (?, ?, ?, ?, ?)
                    """, (user_id, join_date, 0, None, 30))
                    logger.info(f"Added non-admin {user_id} to database with join_date {join_date}")

        conn.commit()
        logger.info("Channel members synced with database")
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Failed to sync channel members: {e}")

# Handle messages from users
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    current_date = datetime.date.today()
    current_timestamp = int(datetime.datetime.now().timestamp())
    logger.info(f"Received message from user {user_id}")

    try:
        member = bot.get_chat_member(CHAT_ID, user_id)
        logger.info(f"User {user_id} status: {member.status}")

        if member.status in ['member', 'administrator', 'creator']:
            user_data = cursor.execute("SELECT join_date, days_remaining FROM members WHERE user_id = ?", (user_id,)).fetchone()
            if user_data:
                join_date, days_remaining = user_data
                join_date = datetime.datetime.strptime(join_date, '%Y-%m-%d').date() if join_date else current_date
                
                if days_remaining is None:
                    expiration_date = join_date + datetime.timedelta(days=30)
                    days_remaining = (expiration_date - current_date).days
                    if days_remaining < 0:
                        days_remaining = 0
                    cursor.execute("UPDATE members SET days_remaining = ? WHERE user_id = ?", (days_remaining, user_id))
                    conn.commit()

                bot.reply_to(message, f"You joined the channel on {join_date} and your membership expires in {days_remaining} days.")
                logger.info(f"User {user_id} is a member. Sent membership info.")
            else:
                join_date = datetime.date(2025, 1, 23)
                days_remaining = 0
                is_admin = 1 if member.status in ['administrator', 'creator'] else 0
                cursor.execute("""
                    INSERT INTO members (user_id, join_date, is_admin, last_payment_date, days_remaining)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, join_date, is_admin, None, days_remaining))
                conn.commit()
                bot.reply_to(message, f"You joined the channel on 2025-01-23 and your membership expires in 0 days.")
                logger.info(f"User {user_id} added to database with join_date 2025-01-23 and 0 days remaining.")
        else:
            logger.info(f"User {user_id} is not an active member (status: {member.status}). Checking payment status.")
            user_data = cursor.execute("SELECT payment_status, invite_link, invite_expiry FROM members WHERE user_id = ?", (user_id,)).fetchone()

            if user_data and user_data[0] == 'success':
                if user_data[1] and user_data[2] > current_timestamp:
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("Join Channel", url=user_data[1]))
                    bot.reply_to(message, "You are not a member of the channel. Here’s your previous one-time invite link:", reply_markup=markup)
                    logger.info(f"User {user_id} not in channel. Re-sent previous invite link: {user_data[1]}")
                else:
                    invite_expiry = int((datetime.datetime.now() + datetime.timedelta(days=1)).timestamp())
                    invite_link = bot.create_chat_invite_link(CHAT_ID, member_limit=1, expire_date=invite_expiry)
                    cursor.execute("""
                        UPDATE members SET invite_link = ?, invite_expiry = ? WHERE user_id = ?
                    """, (invite_link.invite_link, invite_expiry, user_id))
                    conn.commit()
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("Join Channel", url=invite_link.invite_link))
                    bot.reply_to(message, "You are not a member of the channel. Here’s a one-time invite link:", reply_markup=markup)
                    logger.info(f"User {user_id} not in channel. Sent new invite link: {invite_link.invite_link} with expiry {invite_expiry}")
            else:
                payment_url, authority = generate_payment_link(user_id)
                if payment_url:
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("Pay Now", url=payment_url))
                    bot.reply_to(message, f"Please complete your payment of {PRICE} Tomans to join the channel:", reply_markup=markup)
                    logger.info(f"Sent payment link to user {user_id}: {payment_url}")
                else:
                    bot.reply_to(message, "Error generating payment link. Please try again later.")
                    logger.error(f"Failed to send payment link to user {user_id}")

    except telebot.apihelper.ApiTelegramException as e:
        logger.info(f"User {user_id} not in channel or error occurred: {e}")
        user_data = cursor.execute("SELECT payment_status, invite_link, invite_expiry FROM members WHERE user_id = ?", (user_id,)).fetchone()

        if user_data and user_data[0] == 'success':
            if user_data[1] and user_data[2] > current_timestamp:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("Join Channel", url=user_data[1]))
                bot.reply_to(message, "You are not a member of the channel. Here’s your previous one-time invite link:", reply_markup=markup)
                logger.info(f"User {user_id} not in channel. Re-sent previous invite link: {user_data[1]}")
            else:
                invite_expiry = int((datetime.datetime.now() + datetime.timedelta(days=1)).timestamp())
                invite_link = bot.create_chat_invite_link(CHAT_ID, member_limit=1, expire_date=invite_expiry)
                cursor.execute("""
                    UPDATE members SET invite_link = ?, invite_expiry = ? WHERE user_id = ?
                """, (invite_link.invite_link, invite_expiry, user_id))
                conn.commit()
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("Join Channel", url=invite_link.invite_link))
                bot.reply_to(message, "You are not a member of the channel. Here’s a one-time invite link:", reply_markup=markup)
                logger.info(f"User {user_id} not in channel. Sent new invite link: {invite_link.invite_link} with expiry {invite_expiry}")
        else:
            payment_url, authority = generate_payment_link(user_id)
            if payment_url:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("Pay Now", url=payment_url))
                bot.reply_to(message, f"Please complete your payment of {PRICE} Tomans to join the channel:", reply_markup=markup)
                logger.info(f"Sent payment link to user {user_id}: {payment_url}")
            else:
                bot.reply_to(message, "Error generating payment link. Please try again later.")
                logger.error(f"Failed to send payment link to user {user_id}")

# Remove webhook and start polling
def remove_webhook_and_start():
    try:
        bot.remove_webhook()
        logger.info("Webhook removed successfully")
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Failed to remove webhook: {e}")
    
    logger.info("Starting polling...")
    bot.polling(none_stop=True)

if __name__ == "__main__":
    # Start callback server in a separate thread
    callback_thread = threading.Thread(target=run_callback_server, daemon=True)
    callback_thread.start()
    
    logger.info("Starting bot and syncing channel members...")
    sync_channel_members()
    logger.info("Bot initialization complete.")
    remove_webhook_and_start()
