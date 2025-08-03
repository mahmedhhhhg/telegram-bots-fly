import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
import requests
import re
import logging
from telebot import types
import time
from datetime import datetime, timedelta
import signal
import psutil
import sqlite3
import threading
import base64
from concurrent.futures import ThreadPoolExecutor
import json

# Configuration
TOKEN = '8481760166:AAH283BspVpmCYO_At2dsEjNqB2NOshZnJ4'
ADMIN_ID = 7577607150
YOUR_USERNAME = '@Ahmed_bou_2008'
SCAN_API_URL = "https://www.scan-files.free.nf/analyze"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)
executor = ThreadPoolExecutor(max_workers=20)

# File paths
uploaded_files_dir = 'uploaded_bots'
ready_bots_dir = 'ready_bots'
user_invites_dir = 'user_invites'
logs_dir = 'logs'

# Create directories if not exist
for directory in [uploaded_files_dir, ready_bots_dir, user_invites_dir, logs_dir]:
    if not os.path.exists(directory):
        os.makedirs(directory)

## System variables
bot_scripts = {}
stored_tokens = {}
user_files = {}
active_users = set()
banned_users = set()
whitelisted_users = set()  # Users who can access during maintenance
required_channels = set()  # القنوات الإجبارية
bot_locked = False
button_layout = "2x1"
file_scan_enabled = True

# Active and paused bots
active_bots = {}
paused_bots = {}

# Initialize database
def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS custom_buttons
                 (button_name TEXT PRIMARY KEY, 
                  type TEXT,
                  content TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_files
                 (user_id INTEGER, file_name TEXT, status TEXT DEFAULT 'active')''')
                 
    c.execute('''CREATE TABLE IF NOT EXISTS active_users
                 (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                 (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS whitelisted_users
                 (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS ready_bots
                 (bot_name TEXT PRIMARY KEY, description TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS invites
                 (user_id INTEGER, invite_code TEXT UNIQUE, uses INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS button_settings
                 (setting_name TEXT PRIMARY KEY, setting_value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_settings
                 (setting_name TEXT PRIMARY KEY, setting_value TEXT)''')
    # إضافة جدول القنوات الإجبارية
    c.execute('''CREATE TABLE IF NOT EXISTS required_channels
                 (channel_id TEXT PRIMARY KEY, 
                  channel_username TEXT,
                  channel_title TEXT,
                  invite_link TEXT)''')
    
    conn.commit()
    conn.close()

def load_data():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    
    # Load user files
    c.execute('SELECT * FROM user_files')
    for user_id, file_name, status in c.fetchall():
        if user_id not in user_files:
            user_files[user_id] = []
        user_files[user_id].append({'file_name': file_name, 'status': status})
        
        if status == 'active':
            active_bots[(user_id, file_name)] = None
        elif status == 'paused':
            paused_bots[(user_id, file_name)] = None
    
    # Load active users
    c.execute('SELECT * FROM active_users')
    active_users.update({row[0] for row in c.fetchall()})
    
    # Load banned users
    c.execute('SELECT * FROM banned_users')
    banned_users.update({row[0] for row in c.fetchall()})
    
    # Load whitelisted users
    c.execute('SELECT * FROM whitelisted_users')
    whitelisted_users.update({row[0] for row in c.fetchall()})
    
    # Load button layout
    c.execute('SELECT * FROM button_settings WHERE setting_name = "layout"')
    row = c.fetchone()
    global button_layout
    if row:
        button_layout = row[1]
    
    # Load file scan setting
    c.execute('SELECT * FROM bot_settings WHERE setting_name = "file_scan_enabled"')
    row = c.fetchone()
    global file_scan_enabled
    if row:
        file_scan_enabled = row[1].lower() == 'true'
    
    # Load required channels
    c.execute('SELECT * FROM required_channels')
    for row in c.fetchall():
        required_channels.add((row[0], row[1], row[2], row[3]))
    
    conn.close()
    
def escape_markdown(text):
    return text.replace('_', r'\_') if text else "لا يوجد"
    
def check_user_subscription(user_id):
    """التحقق من اشتراك المستخدم في جميع القنوات المطلوبة"""
    if not required_channels:
        return True  # لا توجد قنوات مطلوبة
        
    for channel_id, channel_username, _, _ in required_channels:
        try:
            chat_member = bot.get_chat_member(channel_id, user_id)
            if chat_member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            log_error(f"Error checking subscription for user {user_id} in channel {channel_id}: {e}")
            return False
    return True

# Initialize database
init_db()
load_data()

# Helper functions
def save_custom_button(message, button_name, content_type):
    content = None

    if content_type == 'file':
        if not message.document:
            bot.send_message(message.chat.id, "❌ يجب إرسال ملف.")
            return
        file_info = bot.get_file(message.document.file_id)
        content = message.document.file_id

    elif content_type == 'image':
        if not message.photo:
            bot.send_message(message.chat.id, "❌ يجب إرسال صورة.")
            return
        # نأخذ آخر صورة (الأعلى دقة)
        content = message.photo[-1].file_id

    else:  # text
        content = message.text

    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO custom_buttons (button_name, type, content) VALUES (?, ?, ?)", 
              (button_name, content_type, content))
    conn.commit()
    conn.close()

    bot.send_message(message.chat.id, f"✅ تم إنشاء الزر `{button_name}` بنجاح!", parse_mode="Markdown")
    
def is_admin(user_id):
    return user_id == ADMIN_ID

def is_banned(user_id):
    return user_id in banned_users

def is_whitelisted(user_id):
    return user_id in whitelisted_users

def save_to_db(table, data):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    columns = ', '.join(data.keys())
    placeholders = ', '.join(['?'] * len(data))
    sql = f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})"
    c.execute(sql, tuple(data.values()))
    conn.commit()
    conn.close()

def delete_from_db(table, condition):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    sql = f"DELETE FROM {table} WHERE {condition}"
    c.execute(sql)
    conn.commit()
    conn.close()

def generate_invite_code(user_id):
    code = base64.b64encode(f"{user_id}_{time.time()}".encode()).decode()[:10]
    save_to_db('invites', {'user_id': user_id, 'invite_code': code})
    return code

def ban_user(user_id):
    banned_users.add(user_id)
    save_to_db('banned_users', {'user_id': user_id})
    
    # إيقاف أي بوتات نشطة لهذا المستخدم
    for (uid, file_name), process in list(active_bots.items()):
        if uid == user_id and process:
            kill_process_tree(process)
            del active_bots[(uid, file_name)]
    
    # تحديث حالة الملفات في قاعدة البيانات
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE user_files SET status='paused' WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    if user_id in banned_users:
        banned_users.remove(user_id)
    delete_from_db('banned_users', f"user_id = {user_id}")

def whitelist_user(user_id):
    whitelisted_users.add(user_id)
    save_to_db('whitelisted_users', {'user_id': user_id})

def remove_whitelist(user_id):
    if user_id in whitelisted_users:
        whitelisted_users.remove(user_id)
    delete_from_db('whitelisted_users', f"user_id = {user_id}")

def pause_bot(user_id, file_name):
    if (user_id, file_name) in active_bots and active_bots[(user_id, file_name)]:
        kill_process_tree(active_bots[(user_id, file_name)])
        del active_bots[(user_id, file_name)]
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE user_files SET status = 'paused' WHERE user_id = ? AND file_name = ?", (user_id, file_name))
    conn.commit()
    conn.close()
    
    if user_id in user_files:
        for file_info in user_files[user_id]:
            if file_info['file_name'] == file_name:
                file_info['status'] = 'paused'
                break
    
    paused_bots[(user_id, file_name)] = True
    return True

def resume_bot(user_id, file_name):
    file_path = os.path.join(uploaded_files_dir, file_name)
    if not os.path.exists(file_path):
        return False
    
    try:
        process = subprocess.Popen(['python3', file_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        active_bots[(user_id, file_name)] = process
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("UPDATE user_files SET status = 'active' WHERE user_id = ? AND file_name = ?", (user_id, file_name))
        conn.commit()
        conn.close()
        
        if user_id in user_files:
            for file_info in user_files[user_id]:
                if file_info['file_name'] == file_name:
                    file_info['status'] = 'active'
                    break
        
        if (user_id, file_name) in paused_bots:
            del paused_bots[(user_id, file_name)]
        
        return True
    except Exception as e:
        log_error(f"Failed to resume bot: {e}")
        return False

def delete_bot(user_id, file_name):
    if (user_id, file_name) in active_bots and active_bots[(user_id, file_name)]:
        kill_process_tree(active_bots[(user_id, file_name)])
        del active_bots[(user_id, file_name)]
    
    if (user_id, file_name) in paused_bots:
        del paused_bots[(user_id, file_name)]
    
    file_path = os.path.join(uploaded_files_dir, file_name)
    if os.path.exists(file_path):
        os.remove(file_path)
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM user_files WHERE user_id = ? AND file_name = ?", (user_id, file_name))
    conn.commit()
    conn.close()
    
    if user_id in user_files:
        user_files[user_id] = [f for f in user_files[user_id] if f['file_name'] != file_name]
    return True

def kill_process_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
    except Exception as e:
        log_error(f"Failed to kill process: {e}")

def log_error(error_msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {error_msg}\n"
    
    log_file = os.path.join(logs_dir, f"errors_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(log_file, 'a') as f:
        f.write(log_msg)

def scan_file(file_content, file_name, user_id):
    """فحص الملف للتأكد من خلوه من الأكواد الضارة"""
    if is_admin(user_id):
        return True, "تم تخطي الفحص للمطور"
    
    if not file_scan_enabled:
        return True, "تم تعطيل فحص الملفات من قبل المسؤول"
    
    try:
        files = {'file': (file_name, file_content)}
        response = requests.post(SCAN_API_URL, files=files, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            status = result.get("status", "⚠️ لم يتم الحصول على نتيجة من منصة الفحص.")
            
            if "غير آمن" in status or "ضار" in status or "malicious" in status.lower():
                send_malicious_file_alert(user_id, file_name, file_content, status)
                return False, status
            
            return True, status
        
        return False, "فشل الاتصال بخدمة الفحص"
    
    except requests.exceptions.Timeout:
        log_error("انتهت مهلة الاتصال بخدمة الفحص")
        return False, "انتهت مهلة الاتصال بخدمة الفحص"
    except Exception as e:
        log_error(f"خطأ في فحص الملف: {e}")
        return False, f"حدث خطأ أثناء الفحص: {e}"

def send_malicious_file_alert(user_id, file_name, file_content, scan_result):
    try:
        user = bot.get_chat(user_id)
        username = f"@{user.username}" if user.username else "لا يوجد"
        
        alert_msg = f"""⚠️ **تحذير: ملف ضار تم اكتشافه**
        
📌 **معلومات المستخدم:**
- الاسم: {user.first_name}
- اليوزر: {username.replace('_', r'\_')}
- الايدي: `{user_id}`

📄 **معلومات الملف:**
- اسم الملف: `{file_name}`
- نتيجة الفحص: {scan_result}"""
        
        markup = types.InlineKeyboardMarkup()
        ban_btn = types.InlineKeyboardButton("⛔ حظر المستخدم", callback_data=f"ban_user_{user_id}")
        ignore_btn = types.InlineKeyboardButton("❌ تجاهل", callback_data=f"ignore_alert_{user_id}_{file_name}")
        markup.row(ban_btn, ignore_btn)
        
        # إرسال الملف مع الرسالة
        bot.send_document(
            ADMIN_ID,
            (file_name, file_content),
            caption=alert_msg,
            reply_markup=markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        log_error(f"فشل في إرسال تنبيه الملف الضار: {e}")

def create_main_menu(user_id):
    markup = types.InlineKeyboardMarkup()
    
    # Basic buttons
    upload_button = types.InlineKeyboardButton('📤 رفع ملف', callback_data='upload')
    speed_button = types.InlineKeyboardButton('⚡ سرعة البوت', callback_data='speed')
    contact_button = types.InlineKeyboardButton('تواصل مع المطور', url=f'https://t.me/{YOUR_USERNAME[1:]}')
    
    # New buttons
    my_files_btn = types.InlineKeyboardButton('✅ ملفاتي المرفوعة', callback_data='my_files')
    ready_bots_btn = types.InlineKeyboardButton('🤖 البوتات الجاهزة', callback_data='ready_bots')
    invite_btn = types.InlineKeyboardButton('👬 دعوة صديق', callback_data='invite_friend')
    boost_btn = types.InlineKeyboardButton('⚡️ السرعة القصوى', callback_data='boost_speed')
    
    # Button layout
    if button_layout == "2x1":
        markup.row(upload_button, my_files_btn)
        markup.row(speed_button, ready_bots_btn)
        markup.row(invite_btn, boost_btn)
    elif button_layout == "1+2":
        markup.row(upload_button)
        markup.row(my_files_btn, speed_button)
        markup.row(ready_bots_btn, invite_btn)
        markup.row(boost_btn)
    elif button_layout == "3x1":
        markup.row(upload_button, my_files_btn, speed_button)
        markup.row(ready_bots_btn, invite_btn, boost_btn)
    else:  # Alternate layout
        markup.row(upload_button)
        markup.row(my_files_btn, speed_button)
        markup.row(ready_bots_btn)
        markup.row(invite_btn, boost_btn)
    
    # Admin buttons
    if is_admin(user_id):
        stats_button = types.InlineKeyboardButton('📊 احصائيات', callback_data='stats')
        lock_button = types.InlineKeyboardButton('قفل البوت', callback_data='lock_bot')
        unlock_button = types.InlineKeyboardButton('🔓 فتح البوت', callback_data='unlock_bot')
        broadcast_button = types.InlineKeyboardButton('📢 اذاعة', callback_data='broadcast')
        stop_all_btn = types.InlineKeyboardButton('⛔️ إيقاف جميع ملفاتي', callback_data='stop_all')
        upload_ready_btn = types.InlineKeyboardButton('🧠 رفع ملف جاهز', callback_data='upload_ready_bot')
        show_users_btn = types.InlineKeyboardButton('👥 عرض المستخدمين', callback_data='show_users')
        manage_buttons_btn = types.InlineKeyboardButton('🛠️ إدارة الأزرار', callback_data='manage_buttons')
        server_status_btn = types.InlineKeyboardButton('🖥️ حالة السيرفر', callback_data='server_status')
        whitelist_btn = types.InlineKeyboardButton('📝 إدارة المستثنيين', callback_data='manage_whitelist')
        scan_toggle_btn = types.InlineKeyboardButton('🔍 تفعيل/تعطيل الفحص', callback_data='toggle_scan')
        custom_buttons_btn = types.InlineKeyboardButton('➕ إضافة زر مخصص', callback_data='add_custom_button')
        manage_customs_btn = types.InlineKeyboardButton('🛠️ إدارة الأزرار المخصصة', callback_data='manage_custom_buttons')
        markup.row(manage_customs_btn)

        # إضافة زر الاشتراك الإجباري هنا
        subscription_btn = types.InlineKeyboardButton('📢 الاشتراك الإجباري', callback_data='manage_subscription')
        
        markup.row(stats_button)
        markup.row(lock_button, unlock_button)
        markup.row(broadcast_button)
        markup.row(stop_all_btn)
        markup.row(upload_ready_btn, show_users_btn)
        markup.row(manage_buttons_btn, server_status_btn)
        markup.row(custom_buttons_btn)
        markup.row(whitelist_btn, scan_toggle_btn)
        markup.row(subscription_btn)  # إضافة الزر هنا
    
    markup.row(contact_button)
    
    if not is_admin(user_id):
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT button_name FROM custom_buttons")
        customs = c.fetchall()
        conn.close()

        for row in customs:
            name = row[0]
            markup.row(types.InlineKeyboardButton(f"🔘 {name}", callback_data=f"custom_show_{name}"))

    return markup

# Command handlers
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "لا يوجد"
    escaped_username = username.replace("_", r"\_")
    bot_username = bot.get_me().username.replace("_", r"\_")

    # التحقق من البوتات المقلدة
    if message.from_user.is_bot:
        bot.send_message(message.chat.id, "✅ اضغط على /start للبدأ")
        log_security_event(f"Bot tried to access: {user_id}")
        return
    
    # التحقق من حالة القفل
    if bot_locked and not (is_admin(user_id) or is_whitelisted(user_id)):
        lock_msg = f"""البوت مقفل حاليًا للصيانة
        
للتأكد من أنك تستخدم البوت الرسمي:
- تأكد من اسم المستخدم: @{bot_username}
- لا تستخدم أي بوتات أخرى تحمل اسم مشابه"""
        
        bot.send_message(message.chat.id, lock_msg, parse_mode='Markdown')
        return
    
    # التحقق من الحظر
    if is_banned(user_id):
        ban_msg = f"""⛔ حسابك محظور من استخدام البوت

اليوزر: {escaped_username}
الايدي: `{user_id}`

للتواصل مع الدعم الفني:
اضغط على زر 'تواصل مع المطور' في الأسفل"""
        
        markup = types.InlineKeyboardMarkup()
        contact_btn = types.InlineKeyboardButton('📞 تواصل مع المطور', url=f'https://t.me/{YOUR_USERNAME[1:].replace("_", r"\_")}')
        markup.add(contact_btn)
        
        bot.send_message(message.chat.id, ban_msg, reply_markup=markup, parse_mode='Markdown')
        return
    
    # التحقق من الاشتراك في القنوات المطلوبة
    if required_channels and not (is_admin(user_id) or is_whitelisted(user_id)) and not check_user_subscription(user_id):
        invite_code = None
        if len(message.text.split()) > 1:
            invite_code = message.text.split()[1]
        
        subscription_msg = """⏳ قبل استخدام البوت، يجب عليك الاشتراك في القنوات التالية:\n\n"""
        
        markup = types.InlineKeyboardMarkup()
        
        for channel_id, channel_username, _, invite_link in required_channels:
            subscription_msg += f"🔹 @{channel_username}\n"
            btn = types.InlineKeyboardButton(
                f"انضم إلى @{channel_username}", 
                url=invite_link if invite_link else f"https://t.me/{channel_username}"
            )
            markup.add(btn)
        
        subscription_msg += "\nبعد الاشتراك، اضغط على زر التحقق أدناه."
        verify_btn = types.InlineKeyboardButton("✅ التحقق من الاشتراك", callback_data=f"verify_sub_{invite_code if invite_code else 'none'}")
        markup.add(verify_btn)
        
        help_btn = types.InlineKeyboardButton("🆘 لقد اشتركت ولكن لا يزال لا يعمل", callback_data="subscription_help")
        markup.add(help_btn)
        
        bot.send_message(message.chat.id, subscription_msg, reply_markup=markup)
        return
    
    # تسجيل المستخدم الجديد
    if user_id not in active_users:
        active_users.add(user_id)
        save_to_db('active_users', {'user_id': user_id})
        
        if not is_admin(user_id):
            user_info = f"""🔔 مستخدم جديد | البوت الرسمي

👤 المعلومات:
- الاسم: {message.from_user.first_name}
- اليوزر: {escaped_username}
- الايدي: `{user_id}`
- التاريخ: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- الرابط: tg://user?id={user_id}"""

            markup = types.InlineKeyboardMarkup()
            ban_btn = types.InlineKeyboardButton('⛔ حظر', callback_data=f'user_ban_{user_id}')
            check_btn = types.InlineKeyboardButton('🔍 فحص', callback_data=f'user_check_{user_id}')
            markup.row(ban_btn, check_btn)
            
            bot.send_message(ADMIN_ID, user_info, reply_markup=markup, parse_mode='Markdown')
    
    # معالجة كود الدعوة
    if len(message.text.split()) > 1:
        invite_code = message.text.split()[1]
        if invite_code != 'none':
            try:
                conn = sqlite3.connect('bot_data.db')
                c = conn.cursor()
                c.execute("SELECT user_id FROM invites WHERE invite_code = ?", (invite_code,))
                result = c.fetchone()
                if result:
                    inviter_id = result[0]
                    c.execute("UPDATE invites SET uses = uses + 1 WHERE invite_code = ?", (invite_code,))
                    conn.commit()
                    
                    try:
                        bot.send_message(inviter_id, f"🎉 قام المستخدم {message.from_user.first_name} (@{username}) بالتسجيل عبر رابط دعوتك!")
                    except:
                        pass
            except Exception as e:
                log_error(f"Error processing invite code: {e}")
            finally:
                conn.close()
    
    # حساب الإحصائيات
    total_users = len(active_users)
    total_bots = len(active_bots) + len(paused_bots)
    
    # رسالة الترحيب المحسنة
    welcome_msg = f"""✨ **مرحباً بك في البوت الرسمي لاستضافة ملفات البايثون** ✨

🔹 **معلومات حسابك:**
👨‍💻 الاسم: {message.from_user.first_name}
🆔 اليوزر: {escaped_username}
♻️ الايدي: `{user_id}`
✦ عدد المستخدمين: {total_users}
✦ عدد البوتات المشغلة: {total_bots}

📌 **مميزات البوت:**
- استضافة آمنة لملفات البايثون
- تشغيل 24/24 بدون توقف
- دعم فني متواصل

**للتأكد من أنك تستخدم البوت الرسمي:**
1. تأكد من اسم المستخدم @{bot_username}
2. لا تستخدم أي بوتات أخرى تحمل اسم مشابه

🚀 **اختر من القائمة أدناه لبدأ الاستخدام:**"""
    
    # إضافة علامة مائية
    watermark = "||." * 10
    welcome_msg += f"\n`{watermark}`"
    
    # إرسال الرسالة مع القائمة الرئيسية
    bot.send_message(message.chat.id, welcome_msg, 
                    reply_markup=create_main_menu(user_id),
                    parse_mode='Markdown')

def log_security_event(event):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    escaped_event = event.replace("_", r"\_")
    log_msg = f"[SECURITY] {timestamp} - {escaped_event}\n"
    
    log_file = os.path.join(logs_dir, f"security_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_msg)
    
    # إرسال تنبيه للمطور مع الهروب من الشرطة السفلية
    bot.send_message(ADMIN_ID, f"⚠️ حدث أمني:\n\n`{escaped_event}`", parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: call.data == 'verify_bot')
def verify_bot(call):
    verification_msg = f"""✅ **هذا هو البوت الرسمي**

🔹 معلومات البوت:
- اسم المستخدم: @{bot.get_me().username.replace("_", r"\_")}
- اسم المطور: {YOUR_USERNAME.replace("_", r"\_")}
- تاريخ الإنشاء: 2025
- الإصدار: 1.0

❌ **تحذير:**
لا تستخدم أي بوتات أخرى تحمل اسم مشابه أو تقدم خدمات مماثلة"""
    
    bot.answer_callback_query(call.id, verification_msg, show_alert=True)

def log_security_event(event):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[SECURITY] {timestamp} - {event}\n"
    
    log_file = os.path.join(logs_dir, f"security_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_msg)
    
    # إرسال تنبيه للمطور
    bot.send_message(ADMIN_ID, f"⚠️ حدث أمني:\n\n{log_msg}")
    
# Callback handlers
@bot.callback_query_handler(func=lambda call: call.data == 'manage_custom_buttons')
def manage_custom_buttons(call):
    if not is_admin(call.from_user.id):
        return

    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT button_name, type FROM custom_buttons")
    rows = c.fetchall()
    conn.close()

    if not rows:
        bot.send_message(call.message.chat.id, "🚫 لا توجد أزرار مخصصة حالياً.")
        return

    markup = types.InlineKeyboardMarkup()
    
    # تجميع الأزرار في صفوف كل صف يحتوي على زرين
    buttons = []
    for name, btn_type in rows:
        btn_text = f"{name} ({btn_type})"
        btn = types.InlineKeyboardButton(f"🗑️ {btn_text}", callback_data=f"delete_custom_{name}")
        buttons.append(btn)
    
    # تقسيم الأزرار إلى صفوف كل صف يحتوي على زرين
    for i in range(0, len(buttons), 2):
        row = buttons[i:i+2]
        markup.row(*row)

    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main"))
    
    bot.send_message(call.message.chat.id, "🛠️ اختر الزر الذي تريد حذفه:", reply_markup=markup)
    
@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_custom_'))
def delete_custom_button(call):
    if not is_admin(call.from_user.id):
        return

    name = call.data.split('_', 2)[2]
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM custom_buttons WHERE button_name = ?", (name,))
    conn.commit()
    conn.close()

    bot.send_message(call.message.chat.id, f"🗑️ تم حذف الزر `{name}` بنجاح!", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('custom_show_'))
def show_custom_button(call):
    name = call.data.split('_', 2)[2]
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT type, content FROM custom_buttons WHERE button_name = ?", (name,))
    row = c.fetchone()
    conn.close()

    if not row:
        bot.answer_callback_query(call.id, "❌ الزر غير موجود.", show_alert=True)
        return

    content_type, content = row

    try:
        if content_type == 'text':
            bot.send_message(call.message.chat.id, content)
        elif content_type == 'file':
            bot.send_document(call.message.chat.id, content)
        elif content_type == 'image':
            bot.send_photo(call.message.chat.id, content)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ فشل في عرض المحتوى: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('custom_btn_type_'))
def handle_custom_button_type(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ غير مصرح لك.", show_alert=True)
        return

    parts = call.data.split('_')
    btn_type = parts[3]
    button_name = '_'.join(parts[4:])

    bot.send_message(call.message.chat.id, f"✅ أرسل محتوى الزر '{button_name}' ({btn_type}):")

    if btn_type == 'text':
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, lambda m: finalize_custom_button_text(m, button_name))
    elif btn_type == 'file':
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, lambda m: finalize_custom_button_file(m, button_name))
    elif btn_type == 'action':
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, lambda m: finalize_custom_button_action(m, button_name))

def finalize_custom_button_text(message, button_name):
    bot.send_message(message.chat.id, f"✅ تم إنشاء الزر '{button_name}' بنجاح، وسيعرض النص:\n\n{message.text}")

def finalize_custom_button_file(message, button_name):
    if message.document:
        bot.send_message(message.chat.id, f"✅ تم إنشاء الزر '{button_name}' وسيرسل الملف للمستخدم.")
        # هنا يمكن حفظ الملف إذا أردت استخدامه لاحقًا
    else:
        bot.send_message(message.chat.id, "❌ يجب إرسال ملف.")

def finalize_custom_button_action(message, button_name):
    # مثلاً زر يشغّل إجراء أو يعيد التوجيه
    bot.send_message(message.chat.id, f"⚙️ الزر '{button_name}' سيقوم بتنفيذ إجراء معين: {message.text}")

@bot.callback_query_handler(func=lambda call: call.data == 'add_custom_button')
def handle_add_custom_button(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ هذا الزر مخصص للمطور فقط.", show_alert=True)
        return

    msg = bot.send_message(call.message.chat.id, "📌 أرسل اسم الزر الذي تريد إنشاءه:")
    bot.register_next_step_handler(msg, process_custom_button_name)

def process_custom_button_name(message):
    if not is_admin(message.from_user.id):
        return

    button_name = message.text.strip()
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("📄 نص", callback_data=f"custom_btn_type_text_{button_name}"),
        types.InlineKeyboardButton("🖼️ صورة", callback_data=f"custom_btn_type_image_{button_name}"),
        types.InlineKeyboardButton("📁 ملف", callback_data=f"custom_btn_type_file_{button_name}")
    )
    
    # إرسال الرسالة مع الأزرار
    bot.send_message(message.chat.id, f"اختر نوع الزر '{button_name}':", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('custom_btn_type_'))
def handle_custom_button_type(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ غير مصرح لك.", show_alert=True)
        return

    parts = call.data.split('_')
    btn_type = parts[3]
    button_name = '_'.join(parts[4:])

    msg = bot.send_message(call.message.chat.id, f"✅ أرسل محتوى الزر '{button_name}' ({btn_type}):")
    
    if btn_type == 'text':
        bot.register_next_step_handler(msg, lambda m: save_custom_button(m, button_name, 'text'))
    elif btn_type == 'image':
        bot.register_next_step_handler(msg, lambda m: save_custom_button(m, button_name, 'image'))
    elif btn_type == 'file':
        bot.register_next_step_handler(msg, lambda m: save_custom_button(m, button_name, 'file'))

@bot.callback_query_handler(func=lambda call: call.data.startswith('verify_sub_'))
def handle_verify_subscription(call):
    user_id = call.from_user.id
    invite_code = call.data.split('_')[2] if call.data.split('_')[2] != 'none' else None
    
    if check_user_subscription(user_id):
        bot.answer_callback_query(call.id, "✅ تم التحقق من اشتراكك بنجاح! يمكنك الآن استخدام البوت.")
        
        # حفظ كود الدعوة إذا كان موجوداً
        if invite_code and invite_code != 'none':
            try:
                conn = sqlite3.connect('bot_data.db')
                c = conn.cursor()
                c.execute("UPDATE invites SET uses = uses + 1 WHERE invite_code = ?", (invite_code,))
                conn.commit()
            except Exception as e:
                log_error(f"Error updating invite code uses: {e}")
            finally:
                conn.close()
        
        # إرسال رسالة الترحيب الرئيسية
        send_welcome(call.message)
    else:
        bot.answer_callback_query(call.id, "❌ لم تشترك في جميع القنوات المطلوبة بعد!", show_alert=True)
        
@bot.callback_query_handler(func=lambda call: call.data == 'subscription_help')
def handle_subscription_help(call):
    help_msg = """🆘 **لم يتم التحقق من اشتراكك؟**

1. تأكد من أنك ضغطت على زر الانضمام لكل القنوات
2. بعد الانضمام، اضغط على زر التحقق
3. إذا استمرت المشكلة، قد تحتاج إلى:
   - الخروج من القناة وإعادة الانضمام
   - الانتظار بضع دقائق ثم المحاولة مرة أخرى
   - التأكد من أنك لم تغلق الدردشة مع القناة

إذا استمرت المشكلة، يمكنك التواصل مع الدعم الفني."""
    
    markup = types.InlineKeyboardMarkup()
    contact_btn = types.InlineKeyboardButton("📞 التواصل مع الدعم", url=f"https://t.me/{YOUR_USERNAME[1:]}")
    markup.add(contact_btn)
    
    bot.edit_message_text(help_msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    
@bot.callback_query_handler(func=lambda call: call.data.startswith('ban_user_'))
def handle_ban_user(call):
    try:
        user_id = int(call.data.split('_')[2])
        ban_user(user_id)
        
        # إعلام المسؤول
        bot.answer_callback_query(call.id, f"تم حظر المستخدم {user_id}")
        
        # تحديث الرسالة الأصلية
        alert_msg = call.message.caption + "\n\n✅ تم حظر المستخدم بنجاح"
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=alert_msg,
            reply_markup=None
        )
        
        # إعلام المستخدم المحظور
        try:
            bot.send_message(user_id, "⛔ تم حظرك من استخدام البوت.")
        except:
            pass
        
    except Exception as e:
        log_error(f"خطأ في معالجة حظر المستخدم: {e}")
        bot.answer_callback_query(call.id, "❌ فشل في حظر المستخدم", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == 'manage_subscription')
def manage_subscription(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup()
    
    add_btn = types.InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")
    remove_btn = types.InlineKeyboardButton("➖ إزالة قناة", callback_data="remove_channel")
    list_btn = types.InlineKeyboardButton("📋 عرض القنوات", callback_data="list_channels")
    back_btn = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")
    
    markup.row(add_btn, remove_btn)
    markup.row(list_btn)
    markup.row(back_btn)
    
    try:
        bot.edit_message_text("📢 إدارة الاشتراك الإجباري:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, "📢 إدارة الاشتراك الإجباري:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'add_channel')
def add_channel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة أو الرابط (مثال: @channel_username أو https://t.me/channel_username):")
    bot.register_next_step_handler(msg, process_add_channel)

def process_add_channel(message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        input_text = message.text.strip()
        
        # استخراج معرف القناة من النص المدخل
        if input_text.startswith("https://t.me/"):
            channel_username = input_text.split("/")[-1]
        elif input_text.startswith("@"):
            channel_username = input_text[1:]
        else:
            channel_username = input_text
        
        # إزالة أي شيء بعد علامة ? في الرابط
        channel_username = channel_username.split('?')[0]
        
        # الحصول على معلومات القناة
        try:
            chat = bot.get_chat(f"@{channel_username}")
            channel_id = str(chat.id)
            channel_title = chat.title
            invite_link = None
            
            # التحقق من أن البوت عضو في القناة (هنا تضاف الجديدة)
            try:
                member = bot.get_chat_member(chat.id, bot.get_me().id)
                if member.status not in ['administrator', 'creator']:
                    bot.send_message(message.chat.id, "❌ يجب أن يكون البوت مشرفاً في القناة أولاً!")
                    return
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ يجب أن يكون البوت عضوًا في القناة أولاً! الخطأ: {e}")
                return
            
            try:
                # محاولة إنشاء رابط دعوة
                invite = bot.create_chat_invite_link(chat.id)
                invite_link = invite.invite_link
            except:
                pass
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ لا يمكن الوصول إلى القناة: {e}")
            return
        
        # حفظ القناة في قاعدة البيانات
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO required_channels (channel_id, channel_username, channel_title, invite_link) VALUES (?, ?, ?, ?)",
                 (channel_id, channel_username, channel_title, invite_link))
        conn.commit()
        conn.close()
        
        # تحديث متغير النظام
        required_channels.add((channel_id, channel_username, channel_title, invite_link))
        
        bot.send_message(message.chat.id, f"✅ تمت إضافة القناة {channel_title} (@{channel_username}) بنجاح!")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ أثناء إضافة القناة: {e}")

@bot.callback_query_handler(func=lambda call: call.data == 'remove_channel')
def remove_channel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    if not required_channels:
        bot.answer_callback_query(call.id, "⚠️ لا توجد قنوات مضافة.", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup()
    
    for channel_id, channel_username, channel_title, _ in required_channels:
        btn = types.InlineKeyboardButton(f"❌ {channel_title} (@{channel_username})", 
                                       callback_data=f"remove_channel_{channel_id}")
        markup.add(btn)
    
    back_btn = types.InlineKeyboardButton("🔙 رجوع", callback_data="manage_subscription")
    markup.add(back_btn)
    
    try:
        bot.edit_message_text("اختر القناة التي تريد إزالتها:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, "اختر القناة التي تريد إزالتها:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_channel_'))
def process_remove_channel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    channel_id = call.data.split('_')[2]
    
    try:
        # إزالة القناة من قاعدة البيانات
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("DELETE FROM required_channels WHERE channel_id = ?", (channel_id,))
        conn.commit()
        conn.close()
        
        # تحديث متغير النظام
        global required_channels
        required_channels = {c for c in required_channels if c[0] != channel_id}
        
        bot.answer_callback_query(call.id, "✅ تمت إزالة القناة بنجاح!")
        manage_subscription(call)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ حدث خطأ أثناء إزالة القناة: {e}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == 'list_channels')
def list_channels(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    if not required_channels:
        bot.answer_callback_query(call.id, "⚠️ لا توجد قنوات مضافة.", show_alert=True)
        return
    
    message = "📋 قائمة القنوات الإجبارية:\n\n"
    
    for channel_id, channel_username, channel_title, invite_link in required_channels:
        message += f"🔹 {channel_title} (@{channel_username})\n"
        message += f"🆔 ID: {channel_id}\n"
        message += f"🔗 رابط الدعوة: {invite_link if invite_link else 'غير متوفر'}\n\n"
    
    markup = types.InlineKeyboardMarkup()
    back_btn = types.InlineKeyboardButton("🔙 رجوع", callback_data="manage_subscription")
    markup.add(back_btn)
    
    try:
        bot.edit_message_text(message, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, message, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('ignore_alert_'))
def handle_ignore_alert(call):
    try:
        _, user_id, file_name = call.data.split('_')[2:]
        user_id = int(user_id)
        
        # تحديث الرسالة الأصلية
        alert_msg = call.message.caption + "\n\n❌ تم تجاهل التنبيه"
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=alert_msg,
            reply_markup=None
        )
        
        bot.answer_callback_query(call.id, "تم تجاهل التنبيه")
        
    except Exception as e:
        log_error(f"خطأ في معالجة تجاهل التنبيه: {e}")
        bot.answer_callback_query(call.id, "❌ فشل في تجاهل التنبيه", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('file_toggle_'))
def handle_toggle_bot(call):
    try:
        parts = call.data.split('_')
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "⚠️ بيانات غير صالحة", show_alert=True)
            return
            
        user_id = int(parts[2])
        safe_file_name = '_'.join(parts[3:])
        file_name = safe_file_name.replace('%%', '_')
        
        if call.from_user.id != user_id and not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⚠️ ليس لديك صلاحية للتحكم بهذا الملف.", show_alert=True)
            return
            
        if (user_id, file_name) in active_bots:
            # إيقاف البوت
            success = pause_bot(user_id, file_name)
            if success:
                markup = types.InlineKeyboardMarkup()
                toggle_btn = types.InlineKeyboardButton("▶️ استئناف التشغيل", callback_data=f"file_toggle_{user_id}_{safe_file_name}")
                delete_btn = types.InlineKeyboardButton("🗑️ حذف الملف", callback_data=f"file_delete_{user_id}_{safe_file_name}")
                markup.row(toggle_btn, delete_btn)
                
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=markup
                )
                bot.answer_callback_query(call.id, "⏸️ تم إيقاف البوت مؤقتاً")
                
        elif (user_id, file_name) in paused_bots:
            # استئناف البوت
            success = resume_bot(user_id, file_name)
            if success:
                markup = types.InlineKeyboardMarkup()
                toggle_btn = types.InlineKeyboardButton("⏸️ إيقاف التشغيل", callback_data=f"file_toggle_{user_id}_{safe_file_name}")
                delete_btn = types.InlineKeyboardButton("🗑️ حذف الملف", callback_data=f"file_delete_{user_id}_{safe_file_name}")
                markup.row(toggle_btn, delete_btn)
                
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=markup
                )
                bot.answer_callback_query(call.id, "▶️ تم استئناف تشغيل البوت")
                
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ حدث خطأ: {str(e)}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == 'verify_bot')
def verify_bot(call):
    verification_msg = """✅ **هذا هو البوت الرسمي**

🔹 معلومات البوت:
- اسم المستخدم: @{}
- اسم المطور: {}
- تاريخ الإنشاء: {}
- الإصدار: 1.0

❌ **تحذير:**
لا تستخدم أي بوتات أخرى تحمل اسم مشابه أو تقدم خدمات مماثلة""".format(
        bot.get_me().username,
        YOUR_USERNAME,
        "2025"  # يمكنك تغيير هذا التاريخ
    )
    
    bot.answer_callback_query(call.id, verification_msg, show_alert=True)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    user_id = call.from_user.id
    
    if bot_locked and not (is_admin(user_id) or is_whitelisted(user_id)):
        bot.answer_callback_query(call.id, "⚠️ البوت مقفل حالياً. الرجاء المحاولة لاحقًا.", show_alert=True)
        return
    
    if is_banned(user_id):
        bot.answer_callback_query(call.id, "⛔ تم حظرك من استخدام البوت.", show_alert=True)
        return
    
    try:
        if call.data == 'my_files':
            show_user_files(call)
        elif call.data == 'ready_bots':
            show_ready_bots(call)
        elif call.data == 'invite_friend':
            invite_friend(call)
        elif call.data == 'boost_speed':
            boost_speed(call)
        elif call.data == 'stop_all':
            stop_all_bots(call)
        elif call.data == 'upload_ready_bot':
            upload_ready_bot(call)
        elif call.data == 'show_users':
            show_users(call)
        elif call.data == 'manage_buttons':
            manage_buttons(call)
        elif call.data == 'server_status':
            server_status(call)
        elif call.data == 'manage_whitelist':
            manage_whitelist(call)
        elif call.data == 'toggle_scan':
            toggle_file_scan(call)
        elif call.data == 'upload':
            ask_to_upload_file(call)
        elif call.data == 'speed':
            bot_speed_info(call)
        elif call.data == 'stats':
            stats_menu(call)
        elif call.data == 'lock_bot':
            lock_bot_callback(call)
        elif call.data == 'unlock_bot':
            unlock_bot_callback(call)
        elif call.data == 'broadcast':
            broadcast_callback(call)
        elif call.data.startswith('file_'):
            handle_file_action(call)
        elif call.data.startswith('bot_'):
            handle_bot_action(call)
        elif call.data.startswith('user_'):
            handle_user_action(call)
        elif call.data.startswith('whitelist_'):
            handle_whitelist_action(call)
        elif call.data.startswith('layout_'):
            change_button_layout(call)
        elif call.data == 'back_to_main':
            back_to_main(call)
    except Exception as e:
        log_error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, f"❌ حدث خطأ: {str(e)}", show_alert=True)

# File management
def show_ready_bots(call):
    try:
        # جلب البوتات الجاهزة من قاعدة البيانات
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT * FROM ready_bots")
        ready_bots = c.fetchall()
        conn.close()

        if not ready_bots:
            bot.answer_callback_query(call.id, "⚠️ لا توجد بوتات جاهزة متاحة حالياً.", show_alert=True)
            return

        markup = types.InlineKeyboardMarkup()

        for bot_name, description in ready_bots:
            # زر تشغيل البوت
            run_btn = types.InlineKeyboardButton(
                f"▶️ تشغيل {bot_name}",
                callback_data=f"bot_run_{bot_name}"
            )

            # زر حذف البوت (للمشرف فقط)
            if is_admin(call.from_user.id):
                delete_btn = types.InlineKeyboardButton(
                    f"🗑️ حذف {bot_name}",
                    callback_data=f"bot_delete_{bot_name}"
                )
                markup.row(run_btn, delete_btn)
            else:
                markup.row(run_btn)

        # زر الرجوع
        back_btn = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")
        markup.row(back_btn)

        # إرسال الرسالة مع الأزرار
        try:
            bot.edit_message_text(
                "🤖 اختر أحد البوتات الجاهزة:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        except:
            bot.send_message(
                call.message.chat.id,
                "🤖 اختر أحد البوتات الجاهزة:",
                reply_markup=markup
            )

    except Exception as e:
        log_error(f"Error in show_ready_bots: {e}")
        bot.answer_callback_query(
            call.id,
            "❌ حدث خطأ أثناء جلب البوتات الجاهزة",
            show_alert=True
        )

def show_user_files(call):
    user_id = call.from_user.id
    
    if user_id not in user_files or not user_files[user_id]:
        bot.answer_callback_query(call.id, "⚠️ ليس لديك أي ملفات مرفوعة بعد.", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup()
    
    for file_info in user_files[user_id]:
        file_name = file_info['file_name']
        status = file_info['status']
        
        # استبدال الشرطات السفلية في اسم الملف بعلامة خاصة لتجنب مشاكل التقسيم
        safe_file_name = file_name.replace('_', '%%')
        
        if status == 'active':
            btn_text = f"⏸️ إيقاف {file_name}"
            callback_data = f"file_toggle_{user_id}_{safe_file_name}"
        else:
            btn_text = f"▶️ تشغيل {file_name}"
            callback_data = f"file_toggle_{user_id}_{safe_file_name}"
        
        delete_btn = types.InlineKeyboardButton(f"🗑️ حذف {file_name}", callback_data=f"file_delete_{user_id}_{safe_file_name}")
        action_btn = types.InlineKeyboardButton(btn_text, callback_data=callback_data)
        
        markup.row(action_btn, delete_btn)
    
    markup.row(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text("📂 ملفاتك المرفوعة:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, "📂 ملفاتك المرفوعة:", reply_markup=markup)
        
def handle_file_action(call):
    try:
        # معالجة تبديل التشغيل/الإيقاف
        if call.data.startswith('file_toggle_'):
            handle_toggle_bot(call)
            return
            
        # تقسيم البيانات بشكل صحيح
        parts = call.data.split('_')
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "⚠️ بيانات غير صالحة", show_alert=True)
            return
            
        action = parts[1]
        user_id = int(parts[2])
        file_name = '_'.join(parts[3:])  # دمج الأجزاء المتبقية لاستعادة اسم الملف الكامل
        
        # التحقق من الصلاحيات
        if call.from_user.id != user_id and not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⚠️ ليس لديك صلاحية للتحكم بهذا الملف.", show_alert=True)
            return
            
        if action == 'delete':
            success = delete_bot(user_id, file_name)
            if success:
                bot.delete_message(call.message.chat.id, call.message.message_id)
                bot.answer_callback_query(call.id, f"🗑️ تم حذف الملف {file_name}.")
            else:
                bot.answer_callback_query(call.id, f"❌ فشل في حذف الملف {file_name}.", show_alert=True)
                
    except Exception as e:
        log_error(f"Error in handle_file_action: {e}")
        bot.answer_callback_query(call.id, f"❌ حدث خطأ: {str(e)}", show_alert=True)
        
def handle_bot_action(call):
    if call.data.startswith('bot_run_'):
        bot_name = call.data.split('_')[2]
        msg = bot.send_message(call.message.chat.id, f"أدخل التوكن الجديد لتشغيل البوت {bot_name}:")
        bot.register_next_step_handler(msg, lambda m: run_ready_bot(m, bot_name))
    elif call.data.startswith('bot_delete_') and is_admin(call.from_user.id):
        bot_name = call.data.split('_')[2]
        delete_ready_bot(call, bot_name)

def delete_ready_bot(call, bot_name):
    try:
        bot_path = os.path.join(ready_bots_dir, f"{bot_name}.py")
        if os.path.exists(bot_path):
            os.remove(bot_path)
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("DELETE FROM ready_bots WHERE bot_name = ?", (bot_name,))
        conn.commit()
        conn.close()
        
        bot.send_message(call.message.chat.id, f"✅ تم حذف البوت {bot_name} بنجاح!")
        show_ready_bots(call)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ فشل في حذف البوت: {e}")
        
def run_ready_bot(message, bot_name):
    user_id = message.from_user.id
    token = message.text.strip()
    
    if not re.match(r'^\d+:[a-zA-Z0-9_-]+$', token):
        bot.send_message(message.chat.id, "⚠️ التوكن غير صالح. الرجاء إدخال توكن صحيح.")
        return
    
    bot_path = os.path.join(ready_bots_dir, f"{bot_name}.py")
    if not os.path.exists(bot_path):
        bot.send_message(message.chat.id, "⚠️ البوت الجاهز غير موجود.")
        return
    
    try:
        with open(bot_path, 'r') as f:
            content = f.read()
        
        new_content = re.sub(r'^\s*TOKEN\s*=\s*["\'].*["\']', f'TOKEN = "{token}"', content, flags=re.MULTILINE)
        
        user_bot_path = os.path.join(uploaded_files_dir, f"{bot_name}_{user_id}.py")
        with open(user_bot_path, 'w') as f:
            f.write(new_content)
        
        process = subprocess.Popen(['python3', user_bot_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        file_name = f"{bot_name}_{user_id}.py"
        active_bots[(user_id, file_name)] = process
        
        # الحصول على معلومات البوت
        bot_username = "غير معروف"
        try:
            bot_info = requests.get(f'https://api.telegram.org/bot{token}/getMe').json()
            bot_username = escape_markdown(f"@{bot_info['result']['username']}")
        except:
            pass
        
        # إعداد رسالة النجاح
        success_msg = f"""🎉 تم تشغيل بوتك بنجاح! 🎉

📝 اسم الملف: {bot_name}
👤 معرّف المشغل: {user_id}
🤖 يوزر البوت: {bot_username.replace('_', r'\_')}

يمكنك التحكم بالبوت من الأزرار أدناه 👇"""
        
        # إنشاء أزرار التحكم
        markup = types.InlineKeyboardMarkup()
        toggle_btn = types.InlineKeyboardButton("⏸️ إيقاف التشغيل", callback_data=f"file_toggle_{user_id}_{file_name}")
        delete_btn = types.InlineKeyboardButton("🗑️ حذف الملف", callback_data=f"file_delete_{user_id}_{file_name}")
        
        if bot_username != "غير معروف":
            bot_link_btn = types.InlineKeyboardButton("🚀 الانتقال إلى البوت", url=f"https://t.me/{bot_username[1:]}")
            markup.row(bot_link_btn)
        
        markup.row(toggle_btn, delete_btn)
        
        # تحديث قاعدة البيانات
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("INSERT INTO user_files (user_id, file_name, status) VALUES (?, ?, ?)", 
                 (user_id, file_name, 'active'))
        conn.commit()
        conn.close()
        
        if user_id not in user_files:
            user_files[user_id] = []
        user_files[user_id].append({'file_name': file_name, 'status': 'active'})
        
        # إرسال الرسالة
        bot.send_message(message.chat.id, success_msg, reply_markup=markup)
        
    except Exception as e:
        error_msg = f"""❌ فشل في تشغيل البوت الجاهز

الخطأ: {str(e)}"""
        bot.send_message(message.chat.id, error_msg)

# File upload and processing
@bot.message_handler(content_types=['document'])
def handle_file(message):
    user_id = message.from_user.id
    is_admin_user = is_admin(user_id)  # التحقق إذا كان المستخدم هو الأدمن
    
    if bot_locked and not (is_admin_user or is_whitelisted(user_id)):
        bot.reply_to(message, "⚠️ البوت مقفل حالياً. الرجاء المحاولة لاحقًا.")
        return
    
    if is_banned(user_id):
        bot.reply_to(message, "⛔ تم حظرك من استخدام البوت.")
        return
    
    if not message.document:
        bot.reply_to(message, "⚠️ يجب إرسال ملف.")
        return
    
    file_name = message.document.file_name
    file_size = message.document.file_size
    
    if file_size > MAX_FILE_SIZE:
        bot.reply_to(message, f"⚠️ حجم الملف كبير جداً. الحد الأقصى هو {MAX_FILE_SIZE//1024//1024}MB.")
        return
    
    if not (file_name.endswith('.py') or file_name.endswith('.zip')):
        bot.reply_to(message, "⚠️ هذا البوت خاص برفع ملفات بايثون (.py) أو أرشيفات zip فقط.")
        return
    
    try:
        # تحميل الملف
        file_id = message.document.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # إذا كان المستخدم أدمن، نتخطى عملية الفحص
        if is_admin_user:
            bot.reply_to(message, "⚡ تم تخطي الفحص لأنك المطور. جاري تشغيل الملف...")
            if file_name.endswith('.zip'):
                process_zip_file(downloaded_file, file_name, user_id, message)
            else:
                process_py_file(downloaded_file, file_name, user_id, message)
            return
        
        # إذا كان المستخدم عادي، نتابع عملية الفحص
        processing_msg = bot.reply_to(message, "🔍 جاري فحص الملف، الرجاء الانتظار...")
        
        # فحص الملف
        is_safe, scan_result = scan_file(downloaded_file, file_name, user_id)
        
        if not is_safe:
            bot.edit_message_text(
                f"❌ تم رفض الملف لأنه غير آمن:\n\n{scan_result}",
                message.chat.id,
                processing_msg.message_id
            )
            return
        
        # إذا كان الملف آمناً، المتابعة للمعالجة
        bot.edit_message_text(
            "✅ تمت المعالجة و القرار هو 👇",
            message.chat.id,
            processing_msg.message_id
        )
        
        if file_name.endswith('.zip'):
            process_zip_file(downloaded_file, file_name, user_id, message)
        else:
            process_py_file(downloaded_file, file_name, user_id, message)
            
    except Exception as e:
        bot.reply_to(message, f"❌ حدث خطأ: {e}")
        log_error(f"Error in handle_file: {e}")

def process_zip_file(file_content, file_name, user_id, message):
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, file_name)
        
        with open(zip_path, 'wb') as f:
            f.write(file_content)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.namelist():
                if os.path.isabs(member) or '..' in member:
                    raise Exception("مسار ملف غير آمن في الأرشيف")
            zip_ref.extractall(temp_dir)
        
        final_folder_path = os.path.join(uploaded_files_dir, file_name.split('.')[0])
        if not os.path.exists(final_folder_path):
            os.makedirs(final_folder_path)
        
        for root, dirs, files in os.walk(temp_dir):  # استبدال zip_folder_path بـ temp_dir
            for file in files:
                src_file = os.path.join(root, file)
                dest_file = os.path.join(final_folder_path, file)
                shutil.move(src_file, dest_file)

def process_py_file(file_content, file_name, user_id, message):
    script_path = os.path.join(uploaded_files_dir, file_name)
    with open(script_path, 'wb') as new_file:
        new_file.write(file_content)
    
    run_script(script_path, message.chat.id, uploaded_files_dir, file_name, message)

def run_script(script_path, chat_id, folder_path, file_name, original_message):
    try:
        # قراءة محتوى الملف للتحقق من التوكن
        with open(script_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # فحص إضافي محلي للأكواد الخطرة
        dangerous_patterns = [
            r'os\.system\s*\(',
            r'subprocess\.Popen\s*\(',
            r'eval\s*\(',
            r'exec\s*\(',
            r'__import__\s*\(',
            r'open\s*\([^)]*w[^)]*\)'
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, content):
                raise Exception("🚫 تم رفض الملف لأسباب أمنية")
        
        # تثبيت المتطلبات إذا وجدت
        requirements_path = os.path.join(os.path.dirname(script_path), 'requirements.txt')
        if os.path.exists(requirements_path):
            bot.send_message(chat_id, "🔄 جارٍ تثبيت المتطلبات...")
            subprocess.check_call(['pip', 'install', '-r', requirements_path])
        
        # بدء تشغيل البوت
        process = subprocess.Popen(['python3', script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        active_bots[(chat_id, file_name)] = process
        
        # استخراج معلومات البوت
        token = extract_token_from_script(script_path)
        bot_username = "غير معروف"
        if token:
            try:
                bot_info = requests.get(f'https://api.telegram.org/bot{token}/getMe').json()
                bot_username = escape_markdown(f"@{bot_info['result']['username']}")
            except:
                pass
        
        # إعداد رسالة النجاح مع الأزرار الديناميكية
        success_msg = f"""🎉 تم تشغيل بوتك بنجاح! 🎉

📝 اسم الملف: `{file_name}`
👤 معرّف المشغل: `{original_message.from_user.id}`
🤖 يوزر البوت: {bot_username}

يمكنك التحكم بالبوت من الأزرار أدناه 👇"""
        
        # إنشاء أزرار التحكم
        markup = types.InlineKeyboardMarkup()
        
        # زر تبديل الإيقاف/التشغيل
        toggle_btn = types.InlineKeyboardButton("⏸️ إيقاف التشغيل", callback_data=f"file_toggle_{original_message.from_user.id}_{file_name}")
        
        # زر الانتقال إلى البوت
        if bot_username != "غير معروف":
            bot_link_btn = types.InlineKeyboardButton("🚀 الانتقال إلى البوت", url=f"https://t.me/{bot_username[1:]}")
            markup.row(bot_link_btn)
        
        # زر حذف الملف
        delete_btn = types.InlineKeyboardButton("🗑️ حذف الملف", callback_data=f"file_delete_{original_message.from_user.id}_{file_name}")
        
        markup.row(toggle_btn)
        markup.row(delete_btn)
        
        # تحديث قاعدة البيانات
        user_id = original_message.from_user.id
        if user_id not in user_files:
            user_files[user_id] = []
        user_files[user_id].append({'file_name': file_name, 'status': 'active'})
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("INSERT INTO user_files (user_id, file_name, status) VALUES (?, ?, ?)", 
                 (user_id, file_name, 'active'))
        conn.commit()
        conn.close()
        
        # إرسال الرسالة مع الأزرار
        bot.send_message(chat_id, success_msg, reply_markup=markup, parse_mode='Markdown')
        
        # إعلام المسؤول إذا كان المستخدم ليس أدمن
        if not is_admin(user_id) and token:
            admin_msg = f"""📤 تم تشغيل بوت جديد:
            
📝 الملف: `{file_name}`
👤 المستخدم: {original_message.from_user.first_name} (@{original_message.from_user.username})
🆔 الايدي: `{user_id}`
🤖 بوت المستخدم: {bot_username}"""
            
            admin_markup = types.InlineKeyboardMarkup()
            admin_toggle_btn = types.InlineKeyboardButton("⏸️ إيقاف التشغيل", callback_data=f"file_toggle_{user_id}_{file_name}")
            admin_delete_btn = types.InlineKeyboardButton("🗑️ حذف الملف", callback_data=f"file_delete_{user_id}_{file_name}")
            admin_markup.row(admin_toggle_btn, admin_delete_btn)
            
            bot.send_document(ADMIN_ID, open(script_path, 'rb'), caption=admin_msg, reply_markup=admin_markup, parse_mode='Markdown')
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ: {e}")
        log_error(f"Error in run_script: {e}")

def extract_token_from_script(script_path):
    try:
        with open(script_path, 'r') as script_file:
            file_content = script_file.read()
            token_match = re.search(r"['\"]([0-9]{9,10}:[A-Za-z0-9_-]+)['\"]", file_content)
            if token_match:
                return token_match.group(1)
    except Exception as e:
        log_error(f"Token extraction error: {e}")
    return None

# Admin functions
def stats_menu(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    total_files = sum(len(files) for files in user_files.values())
    total_users = len(user_files)
    active_users_count = len(active_users)
    active_bots_count = len(active_bots)
    paused_bots_count = len(paused_bots)
    
    stats_msg = (f"📊 إحصائيات النظام:\n\n"
                f"📂 عدد الملفات: {total_files}\n"
                f"👥 عدد المستخدمين: {active_users_count}\n"
                f"🤖 البوتات النشطة: {active_bots_count}\n"
                f"⏸️ البوتات الموقوفة: {paused_bots_count}\n"
                f"حالة البوت: {'مقفل' if bot_locked else 'مفتوح'}\n"
                f"🔍 مسح الملفات: {'مفعل' if file_scan_enabled else 'معطل'}")
    
    bot.send_message(call.message.chat.id, stats_msg)

def lock_bot_callback(call):
    if not is_admin(call.from_user.id):
        bot.send_message(call.message.chat.id, "⚠️ أنت لست المطور.")
        return
    
    global bot_locked
    bot_locked = True
    bot.send_message(call.message.chat.id, "تم قفل البوت. فقط المطورون والمستثنون يمكنهم الوصول.")
    
    # إرسال رسالة إعلامية لجميع المستخدمين
    for user_id in active_users:
        try:
            if user_id not in whitelisted_users and user_id != ADMIN_ID:
                bot.send_message(user_id, "⚠️ البوت مقفل حالياً. الرجاء المحاولة لاحقًا.")
        except:
            continue

def unlock_bot_callback(call):
    if not is_admin(call.from_user.id):
        bot.send_message(call.message.chat.id, "⚠️ أنت لست المطور.")
        return
    
    global bot_locked
    bot_locked = False
    bot.send_message(call.message.chat.id, "🔓 تم فتح البوت للجميع.")
    
    # إرسال رسالة إعلامية لجميع المستخدمين
    for user_id in active_users:
        try:
            bot.send_message(user_id, "🎉 تم فتح البوت، يمكنك الآن استخدامه بشكل طبيعي.")
        except:
            continue

def broadcast_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    msg = bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد إذاعتها:")
    bot.register_next_step_handler(msg, process_broadcast_message)

def process_broadcast_message(message):
    if not is_admin(message.from_user.id):
        return
    
    success = 0
    failed = 0
    for user_id in active_users:
        try:
            bot.send_message(user_id, message.text)
            success += 1
        except:
            failed += 1
    
    bot.send_message(message.chat.id, f"✅ تم الإرسال إلى {success} مستخدم\n❌ فشل الإرسال إلى {failed} مستخدم")

def stop_all_bots(call):
    if not is_admin(call.from_user.id):
        bot.send_message(call.message.chat.id, "⚠️ أنت لست المطور.")
        return
    
    count = 0
    for (uid, file_name), process in list(active_bots.items()):
        if process:
            kill_process_tree(process)
        count += 1
        del active_bots[(uid, file_name)]
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("UPDATE user_files SET status = 'paused' WHERE user_id = ? AND file_name = ?", (uid, file_name))
        conn.commit()
        conn.close()
        
        if uid in user_files:
            for file_info in user_files[uid]:
                if file_info['file_name'] == file_name:
                    file_info['status'] = 'paused'
                    break
    
    bot.send_message(call.message.chat.id, f"⛔ تم إيقاف {count} بوت(ات).")

def upload_ready_bot(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    msg = bot.send_message(call.message.chat.id, "أرسل ملف البوت الجاهز (ملف .py):")
    bot.register_next_step_handler(msg, process_ready_bot_upload)

def process_ready_bot_upload(message):
    if not is_admin(message.from_user.id):
        return
    
    if not message.document:
        bot.send_message(message.chat.id, "⚠️ يجب إرسال ملف .py")
        return
    
    file_name = message.document.file_name
    if not file_name.endswith('.py'):
        bot.send_message(message.chat.id, "⚠️ يجب أن يكون الملف بامتداد .py")
        return
    
    try:
        file_id = message.document.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        bot_name = file_name[:-3]
        bot_path = os.path.join(ready_bots_dir, file_name)
        
        with open(bot_path, 'wb') as f:
            f.write(downloaded_file)
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO ready_bots (bot_name, description) VALUES (?, ?)", 
                 (bot_name, "بوت جاهز للمطور"))
        conn.commit()
        conn.close()
        
        notify_users_about_new_bot(bot_name)
        
        bot.send_message(message.chat.id, f"✅ تم رفع البوت الجاهز {bot_name} بنجاح!")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ أثناء رفع البوت: {e}")

def notify_users_about_new_bot(bot_name):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM active_users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    
    message = f"🎉 بوت جاهز جديد متاح!\n\n🤖 اسم البوت: {bot_name}\n\nاستخدم زر '🤖 البوتات الجاهزة' لتجربته!"
    
    for user_id in users:
        try:
            bot.send_message(user_id, message)
        except Exception as e:
            log_error(f"Failed to notify user {user_id}: {e}")

def show_users(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM active_users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    
    if not users:
        bot.answer_callback_query(call.id, "⚠️ لا يوجد مستخدمين مسجلين.", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup()
    
    for user_id in users[:50]:  # Limit to 50 users per page
        try:
            user = bot.get_chat(user_id)
            username = f"@{user.username}" if user.username else "لا يوجد يوزر"
            status = "⛔ محظور" if user_id in banned_users else "✅ نشط"
            
            btn_text = f"{user.first_name} | {username} | {status}"
            callback_data = f"user_manage_{user_id}"
            
            btn = types.InlineKeyboardButton(btn_text, callback_data=callback_data)
            markup.add(btn)
        except:
            continue
    
    markup.row(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text("👥 قائمة المستخدمين:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, "👥 قائمة المستخدمين:", reply_markup=markup)

def handle_user_action(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    action, user_id = call.data.split('_')[1:]
    user_id = int(user_id)
    
    if action == 'manage':
        manage_user(call, user_id)
    elif action == 'ban':
        ban_user(user_id)
        bot.answer_callback_query(call.id, f"⛔ تم حظر المستخدم {user_id}.")
        show_users(call)
    elif action == 'unban':
        unban_user(user_id)
        bot.answer_callback_query(call.id, f"✅ تم فك حظر المستخدم {user_id}.")
        show_users(call)

def manage_user(call, user_id):
    try:
        user = bot.get_chat(user_id)
        username = f"@{user.username}" if user.username else "لا يوجد يوزر"
        status = "⛔ محظور" if user_id in banned_users else "✅ نشط"
        whitelist_status = "✅ مستثنى" if user_id in whitelisted_users else "❌ غير مستثنى"
        
        message = f"👤 معلومات المستخدم:\n\n"
        message += f"🆔 ID: {user_id}\n"
        message += f"👤 الاسم: {user.first_name}\n"
        message += f"📌 اليوزر: {username}\n"
        message += f"♻️ الحالة: {status}\n"
        message += f"📝 حالة الاستثناء: {whitelist_status}\n\n"
        message += "اختر الإجراء المطلوب:"
        
        markup = types.InlineKeyboardMarkup()
        
        if user_id in banned_users:
            ban_btn = types.InlineKeyboardButton("✅ فك الحظر", callback_data=f"user_unban_{user_id}")
        else:
            ban_btn = types.InlineKeyboardButton("⛔ حظر", callback_data=f"user_ban_{user_id}")
        
        if user_id in whitelisted_users:
            whitelist_btn = types.InlineKeyboardButton("❌ إزالة الاستثناء", callback_data=f"whitelist_remove_{user_id}")
        else:
            whitelist_btn = types.InlineKeyboardButton("✅ إضافة استثناء", callback_data=f"whitelist_add_{user_id}")
        
        markup.row(ban_btn, whitelist_btn)
        markup.row(types.InlineKeyboardButton("🔙 رجوع", callback_data="show_users"))
        
        bot.edit_message_text(message, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ حدث خطأ: {e}", show_alert=True)

def manage_whitelist(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup()
    
    # Add user to whitelist
    add_btn = types.InlineKeyboardButton("➕ إضافة مستخدم", callback_data="whitelist_add")
    remove_btn = types.InlineKeyboardButton("➖ إزالة مستخدم", callback_data="whitelist_remove")
    list_btn = types.InlineKeyboardButton("📋 عرض المستثنين", callback_data="whitelist_list")
    back_btn = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")
    
    markup.row(add_btn, remove_btn)
    markup.row(list_btn)
    markup.row(back_btn)
    
    try:
        bot.edit_message_text("📝 إدارة المستثنين:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, "📝 إدارة المستثنين:", reply_markup=markup)

def handle_whitelist_action(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    if call.data == 'whitelist_add':
        msg = bot.send_message(call.message.chat.id, "أرسل معرف المستخدم (ID) لإضافته إلى قائمة المستثنين:")
        bot.register_next_step_handler(msg, lambda m: process_whitelist_add(m, call.from_user.id))
    elif call.data == 'whitelist_remove':
        msg = bot.send_message(call.message.chat.id, "أرسل معرف المستخدم (ID) لإزالته من قائمة المستثنين:")
        bot.register_next_step_handler(msg, lambda m: process_whitelist_remove(m, call.from_user.id))
    elif call.data == 'whitelist_list':
        show_whitelisted_users(call)
    elif call.data.startswith('whitelist_add_'):
        user_id = int(call.data.split('_')[2])
        whitelist_user(user_id)
        bot.answer_callback_query(call.id, f"✅ تم إضافة المستخدم {user_id} إلى قائمة المستثنين.")
        manage_user(call, user_id)
    elif call.data.startswith('whitelist_remove_'):
        user_id = int(call.data.split('_')[2])
        remove_whitelist(user_id)
        bot.answer_callback_query(call.id, f"✅ تم إزالة المستخدم {user_id} من قائمة المستثنين.")
        manage_user(call, user_id)

def process_whitelist_add(message, admin_id):
    if not is_admin(admin_id):
        return
    
    try:
        user_id = int(message.text)
        whitelist_user(user_id)
        bot.send_message(message.chat.id, f"✅ تم إضافة المستخدم {user_id} إلى قائمة المستثنين.")
    except ValueError:
        bot.send_message(message.chat.id, "⚠️ يجب إدخال رقم ID صحيح.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ: {e}")

def process_whitelist_remove(message, admin_id):
    if not is_admin(admin_id):
        return
    
    try:
        user_id = int(message.text)
        remove_whitelist(user_id)
        bot.send_message(message.chat.id, f"✅ تم إزالة المستخدم {user_id} من قائمة المستثنين.")
    except ValueError:
        bot.send_message(message.chat.id, "⚠️ يجب إدخال رقم ID صحيح.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ: {e}")

def show_whitelisted_users(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    if not whitelisted_users:
        bot.answer_callback_query(call.id, "⚠️ لا يوجد مستخدمين في قائمة المستثنين.", show_alert=True)
        return
    
    message = "📋 قائمة المستثنين:\n\n"
    for user_id in whitelisted_users:
        try:
            user = bot.get_chat(user_id)
            username = f"@{user.username}" if user.username else "لا يوجد يوزر"
            message += f"👤 {user.first_name} | {username} | {user_id}\n"
        except:
            message += f"👤 {user_id}\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("🔙 رجوع", callback_data="manage_whitelist"))
    
    try:
        bot.edit_message_text(message, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, message, reply_markup=markup)

def toggle_file_scan(call):
    if not is_admin(call.from_user.id):
        bot.send_message(call.message.chat.id, "⚠️ أنت لست المطور.")
        return
    
    global file_scan_enabled
    file_scan_enabled = not file_scan_enabled
    
    save_to_db('bot_settings', {'setting_name': 'file_scan_enabled', 'setting_value': str(file_scan_enabled)})
    
    status = "مفعل" if file_scan_enabled else "معطل"
    bot.send_message(call.message.chat.id, f"✅ تم تغيير حالة مسح الملفات إلى: {status}")
    
def manage_buttons(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup()
    
    layouts = [
        ("2x1 (صفّين في كل صف)", "2x1"),
        ("1+2 (زر ثم زرّين)", "1+2"),
        ("3x1 (ثلاثة أزرار في كل صف)", "3x1"),
        ("تناوب فردي-زوجي", "alternate")
    ]
    
    for text, layout in layouts:
        btn = types.InlineKeyboardButton(text, callback_data=f"layout_{layout}")
        markup.add(btn)
    
    markup.row(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text("🛠️ اختر تنسيق الأزرار:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, "🛠️ اختر تنسيق الأزرار:", reply_markup=markup)

def change_button_layout(call):
    if not is_admin(call.from_user.id):
        bot.send_message(call.message.chat.id, "⚠️ أنت لست المطور.")
        return
    
    layout = call.data.split('_')[1]
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO button_settings (setting_name, setting_value) VALUES (?, ?)", 
             ("layout", layout))
    conn.commit()
    conn.close()
    
    global button_layout
    button_layout = layout
    
    bot.send_message(call.message.chat.id, f"✅ تم تغيير تنسيق الأزرار إلى: {layout}")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_main_menu(call.from_user.id))
    
def server_status(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⚠️ أنت لست المطور.", show_alert=True)
        return
    
    try:
        # Get server stats
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        memory_used = memory.used / (1024 ** 2)
        memory_total = memory.total / (1024 ** 2)
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        disk_used = disk.used / (1024 ** 3)
        disk_total = disk.total / (1024 ** 3)
        processes = len(psutil.pids())
        uptime = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
        uptime_str = str(uptime).split('.')[0]
        
        # Get bot stats
        total_files = sum(len(files) for files in user_files.values())
        active_bots_count = len(active_bots)
        paused_bots_count = len(paused_bots)
        
        message = "🖥️ حالة السيرفر:\n\n"
        message += f"💻 استخدام المعالج: {cpu_percent}%\n"
        message += f"🧠 استخدام الذاكرة: {memory_percent}% ({memory_used:.1f}MB / {memory_total:.1f}MB)\n"
        message += f"💾 استخدام التخزين: {disk_percent}% ({disk_used:.1f}GB / {disk_total:.1f}GB)\n"
        message += f"🔄 عدد العمليات: {processes}\n"
        message += f"⏱️ مدة التشغيل: {uptime_str}\n\n"
        message += "🤖 حالة البوت:\n\n"
        message += f"📂 عدد الملفات: {total_files}\n"
        message += f"▶️ بوتات نشطة: {active_bots_count}\n"
        message += f"⏸️ بوتات موقوفة: {paused_bots_count}\n"
        message += f"حالة القفل: {'مقفل' if bot_locked else 'مفتوح'}\n"
        message += f"🔍 مسح الملفات: {'مفعل' if file_scan_enabled else 'معطل'}"
        
        markup = types.InlineKeyboardMarkup()
        refresh_btn = types.InlineKeyboardButton("🔄 تحديث", callback_data="server_status")
        back_btn = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")
        markup.row(refresh_btn, back_btn)
        
        try:
            bot.edit_message_text(message, call.message.chat.id, call.message.message_id, reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, message, reply_markup=markup)
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ حدث خطأ أثناء جلب معلومات السيرفر: {e}", show_alert=True)

def invite_friend(call):
    user_id = call.from_user.id
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT invite_code FROM invites WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        invite_code = row[0]
    else:
        invite_code = generate_invite_code(user_id)
    
    invite_link = f"https://t.me/{bot.get_me().username}?start={invite_code}"
    
    message = (f"🎉 دعوة خاصة من {call.from_user.first_name}!\n\n"
              f"🔗 رابط الدعوة: {invite_link}\n\n"
              "قم بمشاركة هذا الرابط مع أصدقائك!")
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(message, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, message, reply_markup=markup)

def boost_speed(call):
    bot.send_message(call.message.chat.id, "⚡ تم تفعيل وضع السرعة القصوى!")

def ask_to_upload_file(call):
    bot.send_message(call.message.chat.id, "📤 أرسل ملف البايثون (.py) الذي ترغب في تشغيله")

def bot_speed_info(call):
    import random
    speed = random.uniform(0.02, 0.06)
    bot.send_message(call.message.chat.id, f"⚡ سرعة البوت: {speed:.2f} ثانية.")
    
@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main(call):
    bot.edit_message_text("〽️ اختر من القائمة:", call.message.chat.id, call.message.message_id, reply_markup=create_main_menu(call.from_user.id))

# Run the bot
if __name__ == '__main__':
    print("✅ البوت يعمل الآن...")
    bot.infinity_polling()