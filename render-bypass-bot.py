import os,json,asyncio,random,logging,socket
from html import escape
from urllib.parse import urlparse,urlunparse,parse_qsl,urlencode
import aiohttp
from aiohttp import ClientTimeout
from telegram import Update,InlineKeyboardButton,InlineKeyboardMarkup
from telegram.ext import Application,CommandHandler,CallbackQueryHandler,MessageHandler,ContextTypes,filters

TOKEN='8653420763:AAE0QqZp99W7Bozq2L5hOYUDWQUMAfpzOSc'
ADMIN_ID=7282835498
PING_INTERVAL=40
DATA_FILE='wlzbi_users.json'
SEP='━━━━━━━━━━━━━━━━━━━'
USER_AGENTS=['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36','Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 Chrome/140.0.0.0 Mobile Safari/537.36','Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 Version/18.6 Mobile/15E148 Safari/604.1','Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36']
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',level=logging.INFO)
logger=logging.getLogger(__name__)
data_lock=asyncio.Lock()
pending_add=set()

def load_data():
    if not os.path.exists(DATA_FILE): return {}
    try:
        with open(DATA_FILE,'r',encoding='utf-8') as f: data=json.load(f)
        if not isinstance(data,dict): return {}
        for uid,d in list(data.items()):
            if isinstance(d,list):
                data[uid]={'first_name':'','last_name':'','username':'','urls':d}
            elif isinstance(d,dict):
                d.setdefault('first_name','')
                d.setdefault('last_name','')
                d.setdefault('username','')
                d.setdefault('urls',[])
                d.setdefault('admin_urls',[])
        return data
    except Exception as e:
        logger.error('Failed to load data: %s',e)
        return {}

def save_data(data):
    tmp=DATA_FILE+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(data,f,indent=2,ensure_ascii=False)
    os.replace(tmp,DATA_FILE)

users=load_data()

async def save():
    async with data_lock: save_data(users)

def ensure_user(user):
    uid=str(user.id)
    if uid not in users:
        users[uid]={'first_name':'','last_name':'','username':'','urls':[],'admin_urls':[]}
    users[uid]['first_name']=user.first_name or ''
    users[uid]['last_name']=user.last_name or ''
    users[uid]['username']=user.username or ''
    users[uid].setdefault('urls',[])
    users[uid].setdefault('admin_urls',[])
    return users[uid]

def is_admin(uid): return uid==ADMIN_ID

def user_name(uid):
    d=users.get(str(uid),{})
    first=d.get('first_name','').strip()
    last=d.get('last_name','').strip()
    username=d.get('username','').strip()
    if first and last: return escape(f'{first} {last}')
    if first: return escape(first)
    if username: return escape(f'@{username}')
    return f'<a href="tg://openmessage?user_id={uid}">{uid}</a>'

def clickable_id(uid): return f'<a href="tg://openmessage?user_id={uid}">{uid}</a>'

def normalize_url(url):
    url=url.strip()
    if not url: return None
    try: parsed=urlparse(url)
    except: return None
    if parsed.scheme.lower()!='https' or not parsed.netloc: return None
    if not parsed.hostname: return None
    hostname=parsed.hostname.lower().rstrip('.')
    if not hostname.endswith('.onrender.com') or not hostname[:-len('.onrender.com')]: return None
    if parsed.username or parsed.password or parsed.port: return None
    return urlunparse(('https',parsed.netloc,parsed.path or '/',parsed.params,parsed.query,parsed.fragment))

async def resolve_ip(url):
    try:
        hostname=urlparse(url).hostname
        if not hostname: return 'Unable to resolve'
        loop=asyncio.get_running_loop()
        return await loop.run_in_executor(None,lambda:socket.gethostbyname(hostname))
    except: return 'Unable to resolve'

async def bypass_loader(message):
    frames=['▹─────','─▹────','──▹───','───▹──','────▹─','─────▹','[W]▹────','[W]─▹───','[W]──▹──','[W]───▹─','[W]─────▹','[W][L]▹────','[W][L]─▹───','[W][L]──▹──','[W][L]───▹─','[W][L]─────▹','[W][L][Z]▹────','[W][L][Z]─▹───','[W][L][Z]──▹──','[W][L][Z]───▹─','[W][L][Z]─────▹','[W][L][Z][B]▹────','[W][L][Z][B]─▹───','[W][L][Z][B]──▹──','[W][L][Z][B]───▹─','[W][L][Z][B]─────▹','[W][L][Z][B][I]▹────','[W][L][Z][B][I]─▹───','[W][L][Z][B][I]──▹──','[W][L][Z][B][I]───▹─','[W][L][Z][B][I]─────▹']
    for frame in frames:
        try:
            await message.edit_text(f'<b>I gotchu bro, lemme bypass it for you ♻️\nbot by @rejerks</b>\n{SEP}\n<code>{frame}</code>',parse_mode='HTML')
        except: pass
        await asyncio.sleep(.45)

def ping_url(url):
    try:
        parsed=urlparse(url)
        params=dict(parse_qsl(parsed.query))
        params['_t']=str(random.randint(100000,999999999))
        return urlunparse((parsed.scheme,parsed.netloc,parsed.path,parsed.params,urlencode(params),parsed.fragment))
    except: return url

async def ping_all_urls(application):
    timeout=ClientTimeout(total=15)
    admin_urls=[]
    normal_urls=[]
    for uid,d in users.items():
        if uid==str(ADMIN_ID):
            admin_urls.extend(d.get('admin_urls',[]))
            admin_urls.extend(d.get('urls',[]))
        else:
            normal_urls.extend(d.get('urls',[]))
    all_urls=admin_urls+normal_urls
    if not all_urls: return
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async def ping(url):
                try:
                    async with session.get(ping_url(url),headers={'User-Agent':random.choice(USER_AGENTS)},allow_redirects=True) as r:
                        await r.read()
                except: pass
            await asyncio.gather(*(ping(u) for u in all_urls),return_exceptions=True)
    except Exception as e:
        logger.error('Pinger error: %s',e)

async def ping_job(context):
    await ping_all_urls(context.application)

async def start(update,context):
    user=update.effective_user
    ensure_user(user)
    await save()
    if is_admin(user.id):
        await admin_panel(update,context)
        return
    keyboard=[[InlineKeyboardButton('➕ Add URL',callback_data='add'),InlineKeyboardButton('🗑️ Remove URL',callback_data='remove')],[InlineKeyboardButton('📋 My URLs',callback_data='my_urls'),InlineKeyboardButton('🧹 Clear All',callback_data='clear')]]
    text=f'<b>🖥️ Render Free Tier Bypasser</b>\n{SEP}\n📌 Submit your Render service URL to configure the bypass.\n\n<b>Supported URL</b>\n<code>https://your-service.onrender.com/</code>\n\n📡 <b>Status:</b> 🟢 Active\n🔒 <b>URL Privacy:</b> 🔇 Private\n{SEP}\nUse the options below to manage your services.\n\n— @rejerks | WLZBI'
    await update.message.reply_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel(update,context):
    total_users=len(users)
    admin_urls=len(users.get(str(ADMIN_ID),{}).get('admin_urls',[]))
    user_urls=sum(len(d.get('urls',[])) for uid,d in users.items() if uid!=str(ADMIN_ID))
    keyboard=[[InlineKeyboardButton('👤 Users',callback_data='admin_users'),InlineKeyboardButton('📋 All URLs',callback_data='admin_urls')],[InlineKeyboardButton('🔗 Add Admin URL',callback_data='admin_add'),InlineKeyboardButton('🗑️ Remove URLs',callback_data='admin_remove')]]
    text=f'<b>💳 Admin Panel</b>\n{SEP}\n📊 <b>System Overview</b>\nRegistered Users: <b>{total_users}</b>\nAdmin URLs: <b>{admin_urls}</b>\nUser URLs: <b>{user_urls}</b>\n📡 Bypass Interval: <b>{PING_INTERVAL} seconds</b>\n{SEP}\n🔒 <b>Priority:</b> Admin URLs are processed before user URLs.\n\n— @rejerks | WLZBI'
    if update.callback_query:
        await update.callback_query.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def add_url(update,context):
    user=update.effective_user
    ensure_user(user)
    pending_add.add(user.id)
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(f'<b>🖥️ Render Free Tier Bypasser</b>\n{SEP}\n📌 Submit your Render service URL to configure the bypass.\n\n<b>Supported URL</b>\n<code>https://your-service.onrender.com/</code>\n\n📡 <b>Status:</b> 🟢 Active\n🔒 <b>URL Privacy:</b> 🔇 Private\n{SEP}\nUse the options below to manage your services.\n\n— @rejerks | WLZBI.',parse_mode='HTML')

async def admin_add(update,context):
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer('Not allowed.',show_alert=True)
        return
    pending_add.add(update.effective_user.id)
    context.user_data['admin_add']=True
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(f'<b>💳 Admin URL Configuration</b>\n{SEP}\n🔗 Send the Render service URL you want to add to the priority list.\n\n<b>Accepted format</b>\n<code>https://your-service.onrender.com/</code>\n{SEP}\n📌 Admin URLs are processed before all user URLs.\n\nSend /cancel to stop.',parse_mode='HTML')

async def handle_url_message(update,context):
    user=update.effective_user
    if user.id not in pending_add: return
    pending_add.discard(user.id)
    url=normalize_url(update.message.text or '')
    admin_mode=context.user_data.pop('admin_add',False)
    if not url:
        await update.message.reply_text(f'<b>🚫 Invalid Render URL</b>\n{SEP}\n❌ Only HTTPS Render service URLs are accepted.\n\n<b>Example</b>\n<code>https://your-service.onrender.com/</code>',parse_mode='HTML')
        return
    d=ensure_user(user)
    target=d['admin_urls'] if admin_mode and is_admin(user.id) else d['urls']
    if url in target:
        await update.message.reply_text(f'<b>✅ URL Already Added</b>\n{SEP}\n📋 This service is already in the selected URL list.',parse_mode='HTML')
        return
    msg=await update.message.reply_text(f'<b>✅ URL Confirmed</b>\n{SEP}\n<code>{escape(url)}</code>\n\n🌐 Resolving service...',parse_mode='HTML')
    ip=await resolve_ip(url)
    await msg.edit_text(f'<b>🟢 URL Confirmed</b>\n{SEP}\n<code>{escape(url)}</code>\n\n🌐 <b>IP Address:</b> <code>{escape(ip)}</code>\n{SEP}\n📡 Preparing bypass...',parse_mode='HTML')
    await asyncio.sleep(.8)
    await bypass_loader(msg)
    target.append(url)
    await save()
    keyboard=[[InlineKeyboardButton('🔗 Bypass Another',callback_data='add'),InlineKeyboardButton('📋 My URLs',callback_data='my_urls')],[InlineKeyboardButton('⚙️ Admin Panel',callback_data='admin_home') if admin_mode else InlineKeyboardButton('↩️ Back',callback_data='user_home')]]
    title='Admin URL Added' if admin_mode else 'Bypassed Free Tier'
    status='🟢 Priority Active' if admin_mode else '🟢 Bypassed'
    await msg.edit_text(f'<b>^_^ Done, Ez bypass :)</b>\n{SEP}\n<b>🟢 {title}</b>\n{SEP}\n🔗 <b>Service URL</b>\n<code>{escape(url)}</code>\n\n🌐 <b>IP Address:</b> <code>{escape(ip)}</code>\n\n📡 <b>Your service will be always 24×7 online.</b>\n<b>Status:</b> {status}\n{SEP}\n— @rejerks | WLZBI',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def cancel(update,context):
    pending_add.discard(update.effective_user.id)
    context.user_data.pop('admin_add',None)
    await update.message.reply_text(f'<b>🚫 Operation Cancelled</b>\n{SEP}\nThe current URL operation has been cancelled.🔚',parse_mode='HTML')

async def my_urls(update,context):
    q=update.callback_query
    user=update.effective_user
    d=ensure_user(user)
    urls=d['urls']
    await q.answer()
    if not urls:
        text=f'<b>📓 My Bypassed Services</b>\n{SEP}\n📋 <b>Saved Services:</b> 0\n📡 <b>Status:</b> No bypassed services\n{SEP}\nNo Render services are currently stored.'
        keyboard=[[InlineKeyboardButton('🔗 Bypass URL',callback_data='add')],[InlineKeyboardButton('↩️ Back',callback_data='user_home')]]
    else:
        text=f'<b>🖥️ My Bypassed Services</b>\n{SEP}\n📋 <b>Total:</b> {len(urls)}\n📡 <b>Status:</b> 🟢 Bypassed\n{SEP}\n'+'\n'.join(f'{i}. <code>{escape(u)}</code>' for i,u in enumerate(urls,1))
        keyboard=[[InlineKeyboardButton('🔗 Bypass',callback_data='add'),InlineKeyboardButton('🗑️ Remove',callback_data='remove')],[InlineKeyboardButton('↩️ Back',callback_data='user_home')]]
    await q.edit_message_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def remove_menu(update,context):
    q=update.callback_query
    d=ensure_user(update.effective_user)
    urls=d['urls']
    await q.answer()
    if not urls:
        await q.edit_message_text(f'<b>🚮 Remove Bypassed URL</b>\n{SEP}\n📋 There are currently no bypassed services.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ Back',callback_data='user_home')]]))
        return
    keyboard=[[InlineKeyboardButton(f'🗑️ Remove {i+1}. {u[:35]}',callback_data=f'delete_{i}')] for i,u in enumerate(urls)]
    keyboard.append([InlineKeyboardButton('↩️ Back',callback_data='user_home')])
    await q.edit_message_text(f'<b>🚮 Remove Bypassed URL</b>\n{SEP}\n📋 <b>Total:</b> {len(urls)}\n{SEP}\nSelect the service you want to remove.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_url(update,context):
    q=update.callback_query
    d=ensure_user(update.effective_user)
    try: i=int(q.data.split('_',1)[1])
    except:
        await q.answer('Invalid URL.',show_alert=True); return
    if i<0 or i>=len(d['urls']):
        await q.answer('URL no longer exists.',show_alert=True); return
    removed=d['urls'].pop(i)
    await save()
    await q.answer('URL removed.')
    await q.edit_message_text(f'<b>🚮 Bypass Removed</b>\n{SEP}\n🗑️ <b>Removed URL</b>\n<code>{escape(removed)}</code>\n{SEP}\n📋 <b>Remaining:</b> {len(d["urls"])}',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗑️ Remove Another',callback_data='remove'),InlineKeyboardButton('📋 My URLs',callback_data='my_urls')],[InlineKeyboardButton('↩️ Back',callback_data='user_home')]]))

async def clear_urls(update,context):
    q=update.callback_query
    d=ensure_user(update.effective_user)
    d['urls'].clear()
    await save()
    await q.answer('All user URLs cleared.')
    await user_home_callback(update,context)

async def user_home_callback(update,context):
    q=update.callback_query
    keyboard=[[InlineKeyboardButton('🔗 Add URL',callback_data='add'),InlineKeyboardButton('🗑️ Remove URL',callback_data='remove')],[InlineKeyboardButton('📋 My URLs',callback_data='my_urls'),InlineKeyboardButton('🧹 Clear All',callback_data='clear')]]
    await q.edit_message_text(f'<b>🖥️ Render Free Tier Bypasser</b>\n{SEP}\n📌 Submit your Render service URL to configure the bypass.\n\n<b>Supported URL</b>\n<code>https://your-service.onrender.com/</code>\n\n📡 <b>Status:</b> 🟢 Active\n🔒 <b>URL Privacy:</b> 🔇 Private\n{SEP}\nUse the options below to manage your services.\n\n— @rejerks | WLZBI',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_users(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    await q.answer()
    keyboard=[]
    for uid,d in users.items():
        name=(f"{d.get('first_name','')} {d.get('last_name','')}").strip() or (f"@{d.get('username')}" if d.get('username') else uid)
        keyboard.append([InlineKeyboardButton(f'👤 {name[:28]} • {len(d.get("urls",[]))}',callback_data=f'admin_user_{uid}')])
    keyboard.append([InlineKeyboardButton('↩️ Back',callback_data='admin_home')])
    await q.edit_message_text(f'<b>🌐 Registered Users</b>\n{SEP}\n👤 <b>Total Users:</b> {len(users)}\n{SEP}\nSelect a user below.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_user_view(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    uid=q.data.replace('admin_user_','')
    if uid not in users:
        await q.answer('User not found.',show_alert=True); return
    d=users[uid]
    urls=d.get('urls',[])
    await q.answer()
    lines=[f'<b>{user_name(uid)}</b>',f'<b>👤 User ID:</b> {clickable_id(uid)}',SEP,'📊 <b>User Information</b>',f'<b>Bypassed URLs:</b> {len(urls)}',f'<b>Status:</b> {"🟢 Active" if urls else "Inactive"}',SEP,'🔗 <b>Bypassed Services</b>']
    lines.extend(f'{i}. <code>{escape(u)}</code>' for i,u in enumerate(urls,1))
    if not urls: lines.append('No URLs are currently stored.')
    await q.edit_message_text('\n'.join(lines),parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗑️ Remove URLs',callback_data=f'admin_remove_user_{uid}')],[InlineKeyboardButton('↩️ Back',callback_data='admin_users')]]))

async def admin_all_urls(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    await q.answer()
    ad=users.get(str(ADMIN_ID),{}).get('admin_urls',[])
    lines=[f'<b>📜 All Bypassed URLs</b>\n{SEP}','📌 <b>Priority Admin URLs</b>']
    lines.extend(f'• <code>{escape(u)}</code>' for u in ad)
    lines.extend([SEP,'👤 <b>User URLs</b>'])
    total=len(ad)
    for uid,d in users.items():
        if uid==str(ADMIN_ID): continue
        for u in d.get('urls',[]):
            total+=1
            lines.append(f'• <code>{escape(u)}</code>')
    lines.extend([SEP,f'📊 <b>Total URLs:</b> {total}'])
    await q.edit_message_text('\n'.join(lines),parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗑️ Remove URLs',callback_data='admin_remove')],[InlineKeyboardButton('↩️ Back',callback_data='admin_home')]]))

async def admin_remove(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    await q.answer()
    keyboard=[]
    admin_urls=users.get(str(ADMIN_ID),{}).get('admin_urls',[])
    if admin_urls: keyboard.append([InlineKeyboardButton('📌 Admin Priority URLs',callback_data='admin_remove_priority')])
    for uid,d in users.items():
        if d.get('urls'):
            name=(f"{d.get('first_name','')} {d.get('last_name','')}").strip() or (f"@{d.get('username')}" if d.get('username') else uid)
            keyboard.append([InlineKeyboardButton(f'👤 {name[:30]}',callback_data=f'admin_remove_user_{uid}')])
    keyboard.append([InlineKeyboardButton('↩️ Back',callback_data='admin_home')])
    await q.edit_message_text(f'<b>🚮 Remove Bypassed URLs</b>\n{SEP}\nSelect a URL group to manage.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_remove_priority(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    await q.answer()
    urls=users[str(ADMIN_ID)].get('admin_urls',[])
    keyboard=[[InlineKeyboardButton(f'🗑️ Remove {i+1}. {u[:35]}',callback_data=f'admin_priority_delete_{i}')] for i,u in enumerate(urls)]
    keyboard.append([InlineKeyboardButton('↩️ Back',callback_data='admin_remove')])
    await q.edit_message_text(f'<b>💳 Admin Priority URLs</b>\n{SEP}\n📌 These URLs are processed before user URLs.\n{SEP}\nSelect a URL to remove.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_remove_user(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    uid=q.data.replace('admin_remove_user_','')
    if uid not in users:
        await q.answer('User not found.',show_alert=True); return
    urls=users[uid].get('urls',[])
    await q.answer()
    keyboard=[[InlineKeyboardButton(f'🗑️ Remove {i+1}. {u[:35]}',callback_data=f'admin_delete_{uid}_{i}')] for i,u in enumerate(urls)]
    keyboard.append([InlineKeyboardButton('↩️ Back',callback_data='admin_remove')])
    await q.edit_message_text(f'<b>🚮 Remove User URLs</b>\n{SEP}\n👤 <b>User:</b> {user_name(uid)}\n📋 <b>Stored URLs:</b> {len(urls)}\n{SEP}\nSelect the URL you want to remove.',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_delete_url(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    parts=q.data.split('_')
    if len(parts)!=4:
        await q.answer('Invalid request.',show_alert=True); return
    uid=parts[2]
    try: i=int(parts[3])
    except:
        await q.answer('Invalid URL.',show_alert=True); return
    if uid not in users or i<0 or i>=len(users[uid].get('urls',[])):
        await q.answer('URL no longer exists.',show_alert=True); return
    removed=users[uid]['urls'].pop(i)
    await save()
    await q.answer('URL removed.')
    await admin_remove_user(update,context)

async def admin_delete_priority(update,context):
    q=update.callback_query
    if not is_admin(update.effective_user.id):
        await q.answer('Not allowed.',show_alert=True); return
    try: i=int(q.data.rsplit('_',1)[1])
    except:
        await q.answer('Invalid URL.',show_alert=True); return
    urls=users[str(ADMIN_ID)].get('admin_urls',[])
    if i<0 or i>=len(urls):
        await q.answer('URL no longer exists.',show_alert=True); return
    urls.pop(i)
    await save()
    await q.answer('Priority URL removed.')
    await admin_remove_priority(update,context)

async def callback_router(update,context):
    q=update.callback_query
    data=q.data
    user=update.effective_user
    ensure_user(user)
    if data=='user_home':
        await q.answer(); await user_home_callback(update,context)
    elif data=='add': await add_url(update,context)
    elif data=='my_urls': await my_urls(update,context)
    elif data=='remove': await remove_menu(update,context)
    elif data=='clear': await clear_urls(update,context)
    elif data.startswith('delete_'): await delete_url(update,context)
    elif data=='admin_home':
        if is_admin(user.id): await q.answer(); await admin_panel(update,context)
        else: await q.answer('Not allowed.',show_alert=True)
    elif data=='admin_add': await admin_add(update,context)
    elif data=='admin_users': await admin_users(update,context)
    elif data=='admin_urls': await admin_all_urls(update,context)
    elif data=='admin_remove': await admin_remove(update,context)
    elif data=='admin_remove_priority': await admin_remove_priority(update,context)
    elif data.startswith('admin_user_'): await admin_user_view(update,context)
    elif data.startswith('admin_remove_user_'): await admin_remove_user(update,context)
    elif data.startswith('admin_delete_'): await admin_delete_url(update,context)
    elif data.startswith('admin_priority_delete_'): await admin_delete_priority(update,context)
    else: await q.answer()

async def error_handler(update,context):
    logger.error('Update error: %s',context.error,exc_info=context.error)

def main():
    application=Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('start',start))
    application.add_handler(CommandHandler('cancel',cancel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,handle_url_message))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_error_handler(error_handler)
    application.job_queue.run_repeating(ping_job,interval=PING_INTERVAL,first=PING_INTERVAL,name='url-pinger')
    logger.info('Starting Render Free Tier Bypasser')
    application.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()