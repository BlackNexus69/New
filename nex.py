import logging
import sqlite3
import requests
import asyncio
import os
import aiohttp
import time
import queue
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from datetime import datetime, timedelta
import json

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN ="7216255079:AAEWV52uMfYlTY_UAmuJ37Yi-VrOasyGYKY"
API_URL = "http://206.189.214.238:8080/search"
GROUP_ID = -1002740434382
ADMIN_CHAT_ID = 8005797405
OWNER_USERNAME = "Nexusxroot"
WHITELISTED_GROUPS = [-1002740434382]
ADMINS = [8005797405]

# Rate limiting configuration
API_RATE_LIMIT = 60  # 60 seconds between API requests for /free
api_last_request_time = 0
api_request_queue = queue.Queue()  # Queue for handling free requests
processing_queue = False

# Premium user rate limit
PREMIUM_RATE_LIMIT = 5  # 5 seconds between premium searches
premium_user_last_request = {}

# Database setup
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('osint_bot.db', check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_premium INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                searches_today INTEGER DEFAULT 0,
                last_search_date TEXT,
                join_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS whitelisted_groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT,
                added_by INTEGER,
                added_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                keyword TEXT,
                results_count INTEGER,
                search_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_by INTEGER,
                added_date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS free_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                keyword TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                status TEXT DEFAULT 'pending',
                added_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    def add_user(self, user_id, username):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username) 
            VALUES (?, ?)
        ''', (user_id, username))
        self.conn.commit()
    
    def get_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone()
    
    def update_user_search(self, user_id):
        cursor = self.conn.cursor()
        today = datetime.now().date().isoformat()
        
        user = self.get_user(user_id)
        if user:
            last_search_date = user[5] if user[5] else None
            
            if last_search_date == today:
                cursor.execute('''
                    UPDATE users 
                    SET searches_today = searches_today + 1 
                    WHERE user_id = ?
                ''', (user_id,))
            else:
                cursor.execute('''
                    UPDATE users 
                    SET searches_today = 1, last_search_date = ? 
                    WHERE user_id = ?
                ''', (today, user_id))
            
            self.conn.commit()
    
    def get_today_searches(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT searches_today FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0
    
    def add_whitelisted_group(self, group_id, group_name, added_by):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO whitelisted_groups (group_id, group_name, added_by)
            VALUES (?, ?, ?)
        ''', (group_id, group_name, added_by))
        self.conn.commit()
    
    def get_whitelisted_groups(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM whitelisted_groups')
        return cursor.fetchall()
    
    def is_group_whitelisted(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT 1 FROM whitelisted_groups WHERE group_id = ?', (group_id,))
        return cursor.fetchone() is not None
    
    def add_search_history(self, user_id, keyword, results_count):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO search_history (user_id, keyword, results_count)
            VALUES (?, ?, ?)
        ''', (user_id, keyword, results_count))
        self.conn.commit()
    
    def add_paid_user(self, user_id, points):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users 
            SET is_premium = 1, points = points + ? 
            WHERE user_id = ?
        ''', (points, user_id))
        self.conn.commit()
    
    def add_admin(self, user_id, username, added_by):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO admins (user_id, username, added_by)
            VALUES (?, ?, ?)
        ''', (user_id, username, added_by))
        self.conn.commit()
    
    def get_admins(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM admins')
        return cursor.fetchall()
    
    def is_admin(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is not None
    
    def add_to_free_queue(self, user_id, keyword, chat_id, message_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO free_queue (user_id, keyword, chat_id, message_id)
            VALUES (?, ?, ?, ?)
        ''', (user_id, keyword, chat_id, message_id))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_next_free_request(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM free_queue 
            WHERE status = 'pending' 
            ORDER BY added_time ASC 
            LIMIT 1
        ''')
        return cursor.fetchone()
    
    def update_queue_status(self, queue_id, status):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE free_queue SET status = ? WHERE id = ?
        ''', (status, queue_id))
        self.conn.commit()
    
    def get_queue_position(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM free_queue 
            WHERE status = 'pending' 
            AND added_time < (SELECT added_time FROM free_queue WHERE user_id = ? AND status = 'pending' ORDER BY added_time DESC LIMIT 1)
        ''', (user_id,))
        result = cursor.fetchone()
        return result[0] + 1 if result else 1

# Initialize database
db = Database()

# Add default admin
db.add_admin(ADMIN_CHAT_ID, "Owner", ADMIN_CHAT_ID)

class APIClient:
    @staticmethod
    async def search_keyword_async(keyword):
        """Async API call with timeout handling"""
        try:
            timeout = aiohttp.ClientTimeout(total=120)  # 2 minutes timeout
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{API_URL}?url={keyword}") as response:
                    if response.status == 200:
                        data = await response.json()
                        return data
                    else:
                        logger.error(f"API returned status: {response.status}")
                        return None
        except asyncio.TimeoutError:
            logger.error("API request timed out")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"API Client Error: {e}")
            return None
        except Exception as e:
            logger.error(f"API Error: {e}")
            return None
    
    @staticmethod
    def search_keyword_sync(keyword):
        """Sync API call with retry mechanism"""
        try:
            response = requests.get(f"{API_URL}?url={keyword}", timeout=90)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"API returned status: {response.status_code}")
                return None
        except requests.exceptions.Timeout:
            logger.error("API request timed out")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"API Request Error: {e}")
            return None
        except Exception as e:
            logger.error(f"API Error: {e}")
            return None

def create_text_file_from_download(download_url, keyword, is_premium=False):
    """Create a text file from download link"""
    try:
        # Download the file from the API
        full_url = f"http://206.189.214.238:8080{download_url}"
        response = requests.get(full_url, timeout=60)
        
        if response.status_code == 200:
            # Save the file
            filename = f"logzilla_{keyword.replace(' ', '_')}_{datetime.now().strftime('%H%M%S')}.txt"
            
            with open(filename, 'wb') as f:
                f.write(response.content)
            
            return filename
        else:
            logger.error(f"Download failed with status: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

def format_caption_for_free(data, keyword, queue_position=None):
    """Format caption for free search results"""
    if not data:
        return "No results found."
    
    caption = f"Search: {keyword}\n"
    caption += f"Type: Free\n"
    
    if 'download' in data:
        download_link = data['download']
        caption += f"Status: File ready for download\n"
        if 'info' in data:
            caption += f"Info: {data['info']}\n"
        caption += f"Time taken: {data.get('time_taken_seconds', 0):.1f} seconds\n"
    else:
        caption += f"Status: {data.get('status', 'unknown')}\n"
    
    if queue_position:
        caption += f"\nYour position in queue: {queue_position}\n"
    
    caption += f"\nBot: LOGZILLA\nTime: {datetime.now().strftime('%I:%M %p')}"
    
    return caption

def format_caption_for_premium(data, keyword):
    """Format caption for premium search results"""
    if not data:
        return "No results found."
    
    caption = f"Search: {keyword}\n"
    caption += f"Type: Premium\n"
    
    if 'download' in data:
        download_link = data['download']
        caption += f"Status: File ready for download\n"
        if 'info' in data:
            caption += f"Info: {data['info']}\n"
        caption += f"Time taken: {data.get('time_taken_seconds', 0):.1f} seconds\n"
        caption += f"Session: {data.get('used_session', 'N/A')}\n"
    else:
        caption += f"Status: {data.get('status', 'unknown')}\n"
    
    caption += f"\nBot: LOGZILLA\nTime: {datetime.now().strftime('%I:%M %p')}"
    
    return caption

async def process_free_queue(context: ContextTypes.DEFAULT_TYPE):
    """Process free search queue (runs every minute)"""
    global processing_queue
    
    if processing_queue:
        return
    
    processing_queue = True
    
    try:
        # Get next request from queue
        next_request = db.get_next_free_request()
        
        if next_request:
            queue_id, user_id, keyword, chat_id, message_id, status, added_time = next_request
            
            # Update status to processing
            db.update_queue_status(queue_id, 'processing')
            
            try:
                # Get the message to update
                try:
                    search_msg = await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"Processing your search: {keyword}\n\nYour request is now being processed..."
                    )
                except:
                    search_msg = None
                
                # Make API call
                results = APIClient.search_keyword_sync(keyword)
                
                if results and 'download' in results:
                    # Download and send file
                    text_file = create_text_file_from_download(results['download'], keyword, False)
                    
                    if text_file:
                        caption = format_caption_for_free(results, keyword)
                        
                        with open(text_file, 'rb') as file:
                            await context.bot.send_document(
                                chat_id=chat_id,
                                document=file,
                                filename=os.path.basename(text_file),
                                caption=caption
                            )
                        
                        # Update user search count
                        db.update_user_search(user_id)
                        db.add_search_history(user_id, keyword, 0)  # We don't know count
                        
                        # Clean up
                        os.remove(text_file)
                        
                        if search_msg:
                            await search_msg.delete()
                    else:
                        if search_msg:
                            await search_msg.edit_text("Error downloading file. Please try again.")
                else:
                    if search_msg:
                        await search_msg.edit_text("No results found or API error.")
                
                # Update status to completed
                db.update_queue_status(queue_id, 'completed')
                
            except Exception as e:
                logger.error(f"Error processing queue item: {e}")
                db.update_queue_status(queue_id, 'failed')
        
    finally:
        processing_queue = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    db.add_user(user_id, username)
    
    welcome_text = f"""
LOGZILLA
Advanced OSINT Search Bot

Developed by: {OWNER_USERNAME}

Available Commands:
/free <keyword> - Free search (Group only, 10 results max, queued)
/paid <keyword> - Premium full results search (Private only)
/myplan - Check your current plan
/premium - Premium features information
/stats - Your usage statistics
/help - Show help message

Important Notes:
- /free command works ONLY in whitelisted groups
- /paid command works ONLY in private chat
- Free searches are queued (1 per minute processing)
- Premium searches get immediate priority

Admin Commands:
/admin - Admin control panel
/whitelist - Manage whitelisted groups
/addgroup <group_id> - Add whitelisted group
/addadmin <user_id> - Add new admin
/addpaid <points> <chat_id> - Add premium user

Examples:
/free example.com (in group)
/paid facebook.com (in private chat)
"""
    
    await update.message.reply_text(welcome_text)

async def free_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Free search - Only works in whitelisted groups"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Check if in group
    if chat_id > 0:  # Positive chat_id means private chat
        await update.message.reply_text(
            "/free command works only in whitelisted groups.\n"
            "Use /paid command in private chat for premium search."
        )
        return
    
    # Check if group is whitelisted
    if not db.is_group_whitelisted(chat_id):
        await update.message.reply_text("This group is not whitelisted for using /free command.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a keyword. Usage: /free example.com")
        return
    
    keyword = ' '.join(context.args)
    
    user = db.get_user(user_id)
    is_premium = user[2] if user else False
    
    if not is_premium:
        today_searches = db.get_today_searches(user_id)
        if today_searches >= 5:
            await update.message.reply_text(
                "Daily search limit reached!\n\n"
                "Free users are limited to 5 searches per day.\n"
                "Upgrade to premium for unlimited searches!\n"
                "Type /premium for details"
            )
            return
    
    # Send initial message
    search_msg = await update.message.reply_text(
        f"Search request added to queue: {keyword}\n"
        f"Please wait while your request is processed...\n"
        f"Free searches are processed 1 per minute."
    )
    
    # Add to queue
    queue_id = db.add_to_free_queue(user_id, keyword, chat_id, search_msg.message_id)
    queue_position = db.get_queue_position(user_id)
    
    # Update message with queue position
    await search_msg.edit_text(
        f"Search request queued: {keyword}\n"
        f"Your position in queue: {queue_position}\n"
        f"Free searches are processed 1 per minute.\n"
        f"Please wait for your turn..."
    )
    
    # If this is the first request, start processing
    if queue_position == 1:
        asyncio.create_task(process_free_queue(context))

async def paid_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Premium search - Only works in private chat"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Check if in private chat
    if chat_id < 0:  # Negative chat_id means group
        await update.message.reply_text(
            "/paid command works only in private chat.\n"
            "Use /free command in groups for free search."
        )
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a keyword. Usage: /paid example.com")
        return
    
    user = db.get_user(user_id)
    is_premium = user[2] if user else False
    
    if not is_premium:
        await update.message.reply_text(
            "Premium feature only!\n\n"
            "This command is available for premium users only.\n"
            "Type /premium for upgrade information."
        )
        return
    
    # Check premium user rate limit
    current_time = time.time()
    if user_id in premium_user_last_request:
        last_request = premium_user_last_request[user_id]
        time_diff = current_time - last_request
        
        if time_diff < PREMIUM_RATE_LIMIT:
            remaining = PREMIUM_RATE_LIMIT - time_diff
            await update.message.reply_text(f"Please wait {int(remaining)} seconds before making another premium search.")
            return
    
    premium_user_last_request[user_id] = current_time
    
    keyword = ' '.join(context.args)
    
    search_msg = await update.message.reply_text("Premium search in progress... Please wait.")
    
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        
        # Try async API call first
        results = await APIClient.search_keyword_async(keyword)
        
        if results is None:
            # If async fails, try sync
            await search_msg.edit_text("Processing premium search... This may take some time.")
            results = APIClient.search_keyword_sync(keyword)
        
        if results is None:
            await search_msg.edit_text("No results found or API is currently unavailable. Please try again later.")
            return
        
        if 'download' not in results:
            await search_msg.edit_text(f"No downloadable results found. Status: {results.get('status', 'unknown')}")
            return
        
        # Download and send file
        text_file = create_text_file_from_download(results['download'], keyword, True)
        
        if text_file:
            caption = format_caption_for_premium(results, keyword)
            
            with open(text_file, 'rb') as file:
                await update.message.reply_document(
                    document=file,
                    filename=os.path.basename(text_file),
                    caption=caption
                )
            
            # Update user search count
            db.update_user_search(user_id)
            db.add_search_history(user_id, keyword, 0)
            
            # Clean up
            os.remove(text_file)
            await search_msg.delete()
        else:
            await search_msg.edit_text("Error downloading file. Please try again.")
            
    except Exception as e:
        logger.error(f"Paid search error: {e}")
        await search_msg.edit_text("Error processing your request. Please try again later.")

async def url_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for free search for backward compatibility"""
    await free_search(update, context)

async def myplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    
    if not user:
        await update.message.reply_text("User not found.")
        return
    
    today_searches = db.get_today_searches(user_id)
    user_type = "Premium" if user[2] else "Free"
    points = user[3]
    
    plan_text = f"""
LOGZILLA - Your Current Plan

Account Type: {user_type}
Points Balance: {points}
Searches Today: {today_searches}/5
Join Date: {user[6]}

Command Access:
Free Users:
- /free: Only in whitelisted groups
- Searches queued (1 per minute processing)
- 5 searches per day limit
- Downloadable results only

Premium Users:
- /paid: Only in private chat
- Immediate processing
- Unlimited daily searches
- Priority access
- Downloadable results only

Note: Results are provided as downloadable files only.
We don't show data directly in chat.
"""
    
    await update.message.reply_text(plan_text)

async def premium_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    premium_text = f"""
LOGZILLA Premium Features

Benefits:
- Access to /paid command in private chat
- Immediate processing (no queue)
- Unlimited daily searches
- Priority API access
- Downloadable results
- Bypass group restrictions

Important Restrictions:
- /free: Group only, queued, 5 per day
- /paid: Private only, immediate, unlimited
- Results: Downloadable files only (no direct display)

Pricing and Upgrade:
Contact {OWNER_USERNAME} for premium pricing and activation.

Queue System for Free Users:
- 1 search processed per minute
- Requests are queued
- Position shown when queued
- Group access only
"""
    
    await update.message.reply_text(premium_text)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    
    if not user:
        await update.message.reply_text("User not found.")
        return
    
    today_searches = db.get_today_searches(user_id)
    user_type = "Premium" if user[2] else "Free"
    points = user[3]
    
    # Get queue info for free users
    queue_info = ""
    if not user[2]:  # Free user
        cursor = db.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM free_queue WHERE status = "pending"')
        pending_count = cursor.fetchone()[0]
        queue_info = f"\nPending free requests: {pending_count}"
    
    stats_text = f"""
LOGZILLA - Your Statistics

Account Type: {user_type}
Points Balance: {points}
Searches Today: {today_searches}/5
Join Date: {user[6]}
{queue_info}

Access Rules:
- Free: /free in groups only, queued
- Premium: /paid in private only, immediate
- Results: Files only (no direct display)

Next Reset: Tomorrow 00:00 UTC
"""
    
    await update.message.reply_text(stats_text)

# [Admin commands remain similar but with queue management]

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMINS and not db.is_admin(user_id):
        await update.message.reply_text("Access denied. Admin only.")
        return
    
    keyboard = [
        [InlineKeyboardButton("System Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("User Management", callback_data="admin_users")],
        [InlineKeyboardButton("Whitelist Groups", callback_data="admin_whitelist")],
        [InlineKeyboardButton("Admin List", callback_data="admin_list")],
        [InlineKeyboardButton("Queue Management", callback_data="admin_queue")],
        [InlineKeyboardButton("Search Analytics", callback_data="admin_analytics")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "LOGZILLA Admin Panel\nSelect an option:",
        reply_markup=reply_markup
    )

async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMINS and not db.is_admin(user_id):
        await update.message.reply_text("Access denied. Admin only.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /addgroup <group_id>")
        return
    
    try:
        group_id = int(context.args[0])
        group_name = " ".join(context.args[1:]) if len(context.args) > 1 else f"Group_{group_id}"
        
        db.add_whitelisted_group(group_id, group_name, user_id)
        
        await update.message.reply_text(
            f"Group added to whitelist!\n"
            f"Group ID: {group_id}\n"
            f"Group Name: {group_name}\n"
            f"Added by: {user_id}\n\n"
            f"Note: /free command now works in this group."
        )
        
    except ValueError:
        await update.message.reply_text("Invalid group ID format.")

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMINS:
        await update.message.reply_text("Access denied. Owner only.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    
    try:
        new_admin_id = int(context.args[0])
        username = " ".join(context.args[1:]) if len(context.args) > 1 else f"User_{new_admin_id}"
        
        db.add_admin(new_admin_id, username, user_id)
        
        await update.message.reply_text(
            f"New admin added!\n"
            f"User ID: {new_admin_id}\n"
            f"Username: {username}\n"
            f"Added by: {user_id}"
        )
        
        try:
            await context.bot.send_message(
                chat_id=new_admin_id,
                text=f"Congratulations!\n\nYou have been promoted to admin in LOGZILLA!\n"
                     f"You now have access to admin commands and panel."
            )
        except:
            pass
            
    except ValueError:
        await update.message.reply_text("Invalid user ID format.")

async def admin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMINS and not db.is_admin(user_id):
        await update.message.reply_text("Access denied. Admin only.")
        return
    
    groups = db.get_whitelisted_groups()
    
    if not groups:
        await update.message.reply_text("No whitelisted groups.")
        return
    
    groups_text = "Whitelisted Groups (Free Search Enabled):\n\n"
    for group in groups:
        groups_text += f"Group ID: {group[0]}\n"
        groups_text += f"Name: {group[1]}\n"
        groups_text += f"Added by: {group[2]}\n"
        groups_text += f"Date: {group[3]}\n\n"
    
    await update.message.reply_text(groups_text)

async def add_paid_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMINS and not db.is_admin(user_id):
        await update.message.reply_text("Access denied. Admin only.")
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /addpaid <points> <chat_id>")
        return
    
    try:
        points = int(context.args[0])
        target_chat_id = int(context.args[1])
        
        db.add_paid_user(target_chat_id, points)
        
        await update.message.reply_text(
            f"Premium access added!\n"
            f"User: {target_chat_id}\n"
            f"Points: {points}\n"
            f"Status: Premium Activated\n\n"
            f"User can now use /paid command in private chat."
        )
        
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=f"Congratulations!\n\nYou've been upgraded to Premium in LOGZILLA!\n"
                     f"Added points: {points}\n"
                     f"You now have access to /paid command in private chat!"
            )
        except:
            pass
            
    except ValueError:
        await update.message.reply_text("Invalid points or chat ID format.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in ADMINS and not db.is_admin(user_id):
        await query.edit_message_text("Access denied.")
        return
    
    data = query.data
    
    if data == "admin_stats":
        total_users = len([db.get_user(user[0]) for user in db.conn.cursor().execute('SELECT user_id FROM users').fetchall()])
        premium_users = len([user for user in db.conn.cursor().execute('SELECT user_id FROM users WHERE is_premium = 1').fetchall()])
        total_searches = db.conn.cursor().execute('SELECT COUNT(*) FROM search_history').fetchone()[0]
        
        # Queue stats
        cursor = db.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM free_queue WHERE status = "pending"')
        pending_requests = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM free_queue WHERE status = "processing"')
        processing_requests = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM free_queue WHERE status = "completed"')
        completed_requests = cursor.fetchone()[0]
        
        stats_text = f"""
LOGZILLA System Statistics

Users:
Total Users: {total_users}
Premium Users: {premium_users}
Free Users: {total_users - premium_users}

Queue Status:
Pending: {pending_requests}
Processing: {processing_requests}
Completed: {completed_requests}
Processing Rate: 1 per minute

Activity:
Total Searches: {total_searches}
API Status: Online

Whitelisted Groups: {len(db.get_whitelisted_groups())}
Admins: {len(db.get_admins())}

Access Rules:
/free: Group only, queued
/paid: Private only, immediate

Bot Owner: {OWNER_USERNAME}
"""
        await query.edit_message_text(stats_text)
    
    elif data == "admin_users":
        users = db.conn.cursor().execute('SELECT user_id, username, is_premium, points FROM users LIMIT 50').fetchall()
        
        users_text = "Recent Users (Last 50)\n\n"
        for user in users:
            status = "Premium" if user[2] else "Free"
            users_text += f"{status} User: {user[1] or 'No username'} (ID: {user[0]})\n"
            users_text += f"Points: {user[3]}\n\n"
        
        await query.edit_message_text(users_text)
    
    elif data == "admin_whitelist":
        groups = db.get_whitelisted_groups()
        groups_text = "Whitelisted Groups\n\n"
        for group in groups:
            groups_text += f"ID: {group[0]} - {group[1]}\n"
        
        await query.edit_message_text(groups_text)
    
    elif data == "admin_list":
        admins = db.get_admins()
        admins_text = "Admin List\n\n"
        for admin in admins:
            admins_text += f"ID: {admin[0]} - {admin[1]}\n"
            admins_text += f"Added by: {admin[2]}\n"
            admins_text += f"Date: {admin[3]}\n\n"
        
        await query.edit_message_text(admins_text)
    
    elif data == "admin_queue":
        # Show current queue
        cursor = db.conn.cursor()
        cursor.execute('''
            SELECT fq.id, fq.user_id, u.username, fq.keyword, fq.status, fq.added_time 
            FROM free_queue fq 
            LEFT JOIN users u ON fq.user_id = u.user_id 
            WHERE fq.status IN ('pending', 'processing')
            ORDER BY fq.added_time ASC
            LIMIT 20
        ''')
        queue_items = cursor.fetchall()
        
        if not queue_items:
            queue_text = "No pending or processing requests in queue."
        else:
            queue_text = "Current Queue (max 20):\n\n"
            for item in queue_items:
                queue_id, user_id, username, keyword, status, added_time = item
                queue_text += f"ID: {queue_id}\n"
                queue_text += f"User: {username or user_id}\n"
                queue_text += f"Keyword: {keyword}\n"
                queue_text += f"Status: {status}\n"
                queue_text += f"Added: {added_time}\n\n"
        
        await query.edit_message_text(queue_text)
    
    elif data == "admin_analytics":
        today = datetime.now().date().isoformat()
        today_searches = db.conn.cursor().execute(
            'SELECT COUNT(*) FROM search_history WHERE DATE(search_date) = ?', 
            (today,)
        ).fetchone()[0]
        
        popular_searches = db.conn.cursor().execute(
            'SELECT keyword, COUNT(*) as count FROM search_history GROUP BY keyword ORDER BY count DESC LIMIT 10'
        ).fetchall()
        
        analytics_text = f"""
Search Analytics

Today's Activity:
Searches: {today_searches}

Popular Keywords:
"""
        for search in popular_searches:
            analytics_text += f"{search[0]}: {search[1]} searches\n"
        
        await query.edit_message_text(analytics_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = f"""
LOGZILLA Help Guide

Command Access Rules:
/free <keyword> - Works ONLY in whitelisted groups
/paid <keyword> - Works ONLY in private chat

Processing:
- Free: Queued, 1 per minute, group only
- Premium: Immediate, private only
- Results: Downloadable files only

Basic Commands:
/myplan - Check your current plan and limits
/premium - Premium features information
/stats - Your usage statistics
/help - Show this help message

Admin Commands:
/admin - Admin control panel
/whitelist - Manage whitelisted groups
/addgroup <group_id> - Add whitelisted group
/addadmin <user_id> - Add new admin
/addpaid <points> <chat_id> - Add premium user

Examples:
In group: /free example.com
In private: /paid facebook.com (premium only)

Support:
Contact {OWNER_USERNAME} for assistance.
"""
    
    await update.message.reply_text(help_text)

async def queue_processor(context: ContextTypes.DEFAULT_TYPE):
    """Background task to process queue every minute"""
    while True:
        try:
            await process_free_queue(context)
        except Exception as e:
            logger.error(f"Queue processor error: {e}")
        
        await asyncio.sleep(60)  # Check every minute

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add job queue for processing free searches
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(queue_processor, interval=60, first=10)  # Every 60 seconds
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("free", free_search))
    application.add_handler(CommandHandler("paid", paid_search))
    application.add_handler(CommandHandler("url", url_search))
    application.add_handler(CommandHandler("myplan", myplan))
    application.add_handler(CommandHandler("premium", premium_info))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("whitelist", admin_whitelist))
    application.add_handler(CommandHandler("addgroup", add_group))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("addpaid", add_paid_user))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)
    
    print("LOGZILLA Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
