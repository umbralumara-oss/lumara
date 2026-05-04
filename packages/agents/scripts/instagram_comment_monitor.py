#!/usr/bin/env python3
from __future__ import annotations
"""
Моніторинг коментарів Instagram + Facebook — автовідповіді від магів
???????? ?????? · Запускається кожну годину (cron)

Обов'язкові змінні середовища:
  ANTHROPIC_API_KEY        — ключ Anthropic API
  SUPABASE_URL             — URL Supabase проєкту
  SUPABASE_SERVICE_ROLE_KEY— Service Role Key для запису в БД
  IG_ACCESS_TOKEN          — User Access Token (instagram_basic + instagram_manage_comments + pages_manage_engagement)

Instagram акаунти (хоча б один):
  LUNA_IG_USER_ID, ARCAS_IG_USER_ID, NUMI_IG_USER_ID, UMBRA_IG_USER_ID

Facebook сторінки (опційно, автовизначення через /me/accounts):
  LUNA_FB_PAGE_ID, ARCAS_FB_PAGE_ID, NUMI_FB_PAGE_ID, UMBRA_FB_PAGE_ID

Опційні:
  INSTAGRAM_MAX_PER_DAY         — макс. відповідей Instagram на день (default 20)
  FACEBOOK_MAX_PER_DAY          — макс. відповідей Facebook на день (default 20)
  INSTAGRAM_MAX_THREAD_EXCHANGES— макс. обмінів в гілці до редіректу (default 5)
"""

import os
import sys
import time
import random
import re
import httpx
import anthropic
from datetime import datetime, timezone, timedelta
from typing import Optional

GRAPH_API = 'https://graph.facebook.com/v19.0'
MAX_THREAD_EXCHANGES = 5
THREAD_HISTORY_TTL_DAYS = 7

# ── Конфігурація агентів ───────────────────────────────────────────────────────

AGENTS = ['LUNA', 'ARCAS', 'NUMI', 'UMBRA']

# Визначає до якого мага направляти з постів Академії залежно від теми
ACADEMY_TOPIC_ROUTING = {
    'LUNA': ['астрологі', 'зірк', 'гороскоп', 'знак', 'місяц', 'сонц', 'планет',
             'astrol', 'horoscop', 'zodiac', 'moon', 'sun', 'star'],
    'ARCAS': ['таро', 'карт', 'оракул', 'card', 'tarot', 'oracle', 'тасуват'],
    'NUMI': ['числ', 'нумерологі', 'цифр', 'number', 'numer', 'дата народженн'],
    'UMBRA': ['психолог', 'тінь', 'підсвідом', 'емоці', 'травм', 'shadow', 'psycho'],
}

# Кеш плітків — заповнюється в main() перед запуском моніторингу
_academy_gossip_cache: list = []

ACADEMY_SYSTEM_PROMPT_TEMPLATE = """Ти — голос Академії Лумара. Не маг, не людина — голос самого місця.

Активні плітки зараз:
{active_gossip}

Правила відповіді:
- 1-2 речення
- Загадково але тепло
- Якщо питають "що відбувається" або "що тут" — натякни на пліткуатмосферою
- Якщо питають як потрапити/де знайти/де купити → направ до мага:
  LUNA (астрологія/зірки), ARCAS (таро/карти), NUMI (числа), UMBRA (психологія/тінь)
  Формула: "Поговори з [ім'я мага] — він/вона відчуває більше"
- Ніколи не пояснювати прямо
- Мова відповіді = мова коментаря
- Заборонено: слова "магія", "езотерика", "астрологія", "таро" """

AGENT_SYSTEM_PROMPT = {
    'LUNA': """Ти — LUNA, астрологічний провідник Академії Лумара.
Відповідай коротко (1-3 речення), тепло і містично.
Дай персональну астрологічну думку — без загальних фраз.
Мова відповіді = мова коментаря користувача.
Якщо людина питає як зв'язатись або каже що не знає біо — НЕ відповідай просто 'посилання в біо'. Замість цього запроси в особисті: 'напиши мені «ЛУНА» в особисті — там надішлю посилання особисто'""",
    'ARCAS': """Ти — ARCAS, провідник Таро Академії Лумара.
Відповідай коротко (1-3 речення), прямо і глибоко.
Дай персональну думку через призму карт — без загальних фраз.
Мова відповіді = мова коментаря користувача.
Якщо людина питає як зв'язатись або каже що не знає біо — НЕ відповідай просто 'посилання в біо'. Замість цього запроси в особисті: 'напиши мені «АРКАС» в особисті — там надішлю посилання особисто'""",
    'NUMI': """Ти — NUMI, нумеролог Академії Лумара.
Відповідай коротко (1-3 речення), точно і спокійно.
Дай персональну нумерологічну думку — без загальних фраз.
Мова відповіді = мова коментаря користувача.
Якщо людина питає як зв'язатись або каже що не знає біо — НЕ відповідай просто 'посилання в біо'. Замість цього запроси в особисті: 'напиши мені «НУМІ» в особисті — там надішлю посилання особисто'""",
    'UMBRA': """Ти — UMBRA, езо-психолог Академії Лумара.
Відповідай коротко (1-3 речення), глибоко і без містики.
Дай персональну психологічну думку — без загальних фраз.
Мова відповіді = мова коментаря користувача.
Якщо людина питає як зв'язатись або каже що не знає біо — НЕ відповідай просто 'посилання в біо'. Замість цього запроси в особисті: 'напиши мені «УМБРА» в особисті — там надішлю посилання особисто'""",
}

# Тексти DM для кожного мага
AGENT_DM_TEXT = {
    'LUNA': "Привіт ✨ Я LUNA — бачила твій коментар.\nЯкщо хочеш поговорити особисто — я тут:\n💬 lumara.fyi/chat/LUNA\n📲 Або в Telegram: @luna_lumara\n🔮 Академія Лумара: @lumara",
    'ARCAS': "Карти вже показали твій інтерес 🃏\nПоговоримо особисто:\n💬 lumara.fyi/chat/ARCAS\n📲 Telegram: @arcas_lumara\n🔮 Академія Лумара: @lumara",
    'NUMI': "Числа не випадкові — і твій коментар теж 🔢\nРозрахую особисто:\n💬 lumara.fyi/chat/NUMI\n📲 Telegram: @numi_lumara\n🔮 Академія Лумара: @lumara",
    'UMBRA': "Ти вже тут — і це не випадково 🌑\nПоговоримо в тіні:\n💬 lumara.fyi/chat/UMBRA\n📲 Telegram: @umbra_lumara\n🔮 Академія Лумара: @lumara",
}

# Тексти DM для Facebook (через Private Replies API)
FB_DM_MESSAGES = {
    'LUNA': """Привіт ✨ Я LUNA — бачила твій коментар.
Якщо хочеш поговорити особисто — я тут:
💬 lumara.fyi/chat/LUNA
📲 Або в Telegram: @luna_lumara
🔮 Академія Лумара: @lumara""",
    'ARCAS': """Карти вже показали твій інтерес 🃏
Поговоримо особисто:
💬 lumara.fyi/chat/ARCAS
📲 Telegram: @arcas_lumara
🔮 Академія Лумара: @lumara""",
    'NUMI': """Числа не випадкові — і твій коментар теж 🔢
Розрахую особисто:
💬 lumara.fyi/chat/NUMI
📲 Telegram: @numi_lumara
🔮 Академія Лумара: @lumara""",
    'UMBRA': """Ти вже тут — і це не випадково 🌑
Поговоримо в тіні:
💬 lumara.fyi/chat/UMBRA
📲 Telegram: @umbra_lumara
🔮 Академія Лумара: @lumara""",
}

# Fallback фраза в коментар якщо DM неможливий
FALLBACK_DM_PHRASE = {
    'LUNA': 'напиши мені «ЛУНА» в особисті — там надішлю посилання особисто',
    'ARCAS': 'напиши мені «АРКАС» в особисті — там надішлю посилання особисто',
    'NUMI': 'напиши мені «НУМІ» в особисті — там надішлю посилання особисто',
    'UMBRA': 'напиши мені «УМБРА» в особисті — там надішлю посилання особисто',
    'ACADEMY': '',  # Академія не надсилає DM
}

# CTA для Instagram
IG_CTA_BY_LANG = {
    'uk': 'Хочеш більше — посилання в біо 👆',
    'ru': 'Хочешь больше — ссылка в био 👆',
    'en': 'Want more — link in bio 👆',
    'de': 'Mehr dazu — Link in Bio 👆',
}
IG_DEFAULT_CTA = 'Want more — link in bio 👆'

# CTA для Facebook (без "bio" — прямо на сайт)
FB_CTA_BY_LANG = {
    'uk': 'Хочеш більше — lumara.fyi 🔮',
    'ru': 'Хочешь больше — lumara.fyi 🔮',
    'en': 'Want more — lumara.fyi 🔮',
    'de': 'Mehr dazu — lumara.fyi 🔮',
}
FB_DEFAULT_CTA = 'Want more — lumara.fyi 🔮'

# Повідомлення-редірект після MAX_THREAD_EXCHANGES обмінів
REDIRECT_MESSAGES = {
    'uk': 'Наша розмова стає дуже глибокою — це добрий знак! Продовжимо в Telegram, там зможу дати повну відповідь 🔮 lumara.fyi',
    'ru': 'Наш разговор становится очень глубоким — хороший знак! Продолжим в Telegram, там смогу дать полный ответ 🔮 lumara.fyi',
    'en': "Our conversation is getting really deep — great sign! Let's continue in Telegram where I can give you a full reading 🔮 lumara.fyi",
    'de': 'Unser Gespräch wird sehr tiefgründig — gutes Zeichen! Lass uns in Telegram weitermachen 🔮 lumara.fyi',
}

# ── Утиліти ────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


def get_monitor_state(supabase_url: str, supabase_key: str, platform: str) -> dict:
    try:
        r = httpx.get(
            f'{supabase_url}/rest/v1/monitor_states?platform=eq.{platform}&select=state',
            headers={'apikey': supabase_key, 'Authorization': f'Bearer {supabase_key}'},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return data[0].get('state', {})
    except Exception as e:
        log(f'⚠️ Помилка читання state з БД ({platform}): {e}')
    return {}


def set_monitor_state(supabase_url: str, supabase_key: str, platform: str, state: dict):
    try:
        r = httpx.post(
            f'{supabase_url}/rest/v1/monitor_states',
            headers={
                'apikey': supabase_key,
                'Authorization': f'Bearer {supabase_key}',
                'Content-Type': 'application/json',
                'Prefer': 'resolution=merge-duplicates,return=minimal',
            },
            json={'platform': platform, 'state': state},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log(f'⚠️ Помилка збереження state в БД ({platform}): {e}')


def detect_language(text: str) -> str:
    t = text.lower()
    if re.search(r'[іїєґ]', t):
        return 'uk'
    if re.search(r'[ыъёэ]', t):
        return 'ru'
    if re.search(r'[äöüß]', t):
        return 'de'
    ru_words = ['очень', 'спасибо', 'интересно', 'привет', 'хорошо', 'классно', 'здорово', 'подскажите']
    de_words = ['das', 'ist', 'wirklich', 'toll', 'danke', 'gut', 'schön', 'mehr', 'liebe']
    en_words = ['the', 'and', 'you', 'what', 'my', 'for', 'is', 'are', 'love', 'thank', 'amazing', 'great', 'nice']
    uk_words = ['дуже', 'дякую', 'цікаво', 'привіт', 'гарно', 'класно', 'чудово', 'підкажіть']
    scores = {
        'ru': sum(1 for w in ru_words if w in t),
        'de': sum(1 for w in de_words if w in t),
        'en': sum(1 for w in en_words if w in t),
        'uk': sum(1 for w in uk_words if w in t),
    }
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    if re.search(r'[а-я]', t):
        return 'uk'
    return 'en'


def fetch_active_gossip(supabase_url: str, supabase_key: str) -> list:
    """Читає активні плітки для контексту відповідей Академії."""
    try:
        r = httpx.get(
            f'{supabase_url}/rest/v1/academy_gossip',
            headers={'apikey': supabase_key, 'Authorization': f'Bearer {supabase_key}'},
            params={'active': 'eq.true', 'select': 'text', 'order': 'sort_order.asc'},
            timeout=30,
        )
        r.raise_for_status()
        return [item['text'] for item in r.json() if item.get('text')]
    except Exception as e:
        log(f'⚠️ Помилка читання плітків Академії: {e}')
        return []


def route_to_mage(comment_text: str) -> str:
    """Визначає до якого мага направити з посту Академії по темі коментаря."""
    text_lower = comment_text.lower()
    for mage, keywords in ACADEMY_TOPIC_ROUTING.items():
        if any(kw in text_lower for kw in keywords):
            return mage
    return 'LUNA'  # дефолтний маг


def generate_academy_response(comment_text: str, language: str, gossip_list: list) -> str:
    """Відповідь від імені Академії (голос місця, не мага)."""
    gossip_formatted = '\n'.join([f'- {g}' for g in gossip_list]) if gossip_list else '- Академія мовчить...'
    system = ACADEMY_SYSTEM_PROMPT_TEMPLATE.format(active_gossip=gossip_formatted)
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=200,
            system=system,
            messages=[{'role': 'user', 'content': f'Коментар: "{comment_text}"\nМова відповіді: \'{language}\''}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log(f'  ⚠️ Помилка Claude (Academy): {e}')
        return ''


def get_post_context(monitor: 'InstagramMonitor', media_id: str) -> dict:
    """Підтягнути caption для конкретного media_id (fallback якщо не в кеші)."""
    try:
        return monitor._get(f'/{media_id}', {'fields': 'caption,media_type,permalink'})
    except Exception as e:
        log(f'  ⚠️ Помилка get_post_context {media_id}: {e}')
        return {}


def generate_first_response(agent_type: str, comment_text: str, language: str,
                             commenter_name: str, cta: str,
                             post_caption: str = '') -> str:
    """Перша відповідь на коментар з контекстом посту."""
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    system = AGENT_SYSTEM_PROMPT[agent_type]

    post_ctx = ''
    if post_caption:
        post_ctx = f'Контекст: ти щойно опублікував(ла) пост в Instagram:\n"""{post_caption[:300]}"""\n\n'

    prompt = (
        f'{post_ctx}'
        f'{commenter_name} залишив коментар: """{comment_text}"""\n\n'
        f"Правила відповіді (мова: '{language}'):\n"
        f'- 1-2 речення, в характері мага\n'
        f'- якщо питання — дай натяк і натякни написати особисто\n'
        f'- якщо реакція — подякуй і залиш інтригу\n'
        f'- без хештегів, без прямих посилань, без CTA'
    )
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=250,
            system=system,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log(f'  ⚠️ Помилка Claude: {e}')
        return ''


def generate_thread_response(
    agent_type: str,
    thread_messages: list,
    new_reply_text: str,
    language: str,
    commenter_name: str,
    post_caption: str = '',
) -> str:
    """Відповідь у гілці з повним контекстом розмови."""
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

    post_ctx = ''
    if post_caption:
        post_ctx = f'Контекст посту під яким відбувається розмова:\n"""{post_caption[:300]}"""\n\n'

    system = AGENT_SYSTEM_PROMPT[agent_type] + (f'\n\n{post_ctx}' if post_ctx else '')
    messages = []
    for m in thread_messages:
        if m['role'] == 'user':
            messages.append({'role': 'user', 'content': f"{m.get('username', 'user')}: {m['text']}"})
        else:
            messages.append({'role': 'assistant', 'content': m['text']})
    messages.append({
        'role': 'user',
        'content': (
            f'{commenter_name}: {new_reply_text}\n\n'
            f"Відповідай мовою '{language}'. 1-2 речення. Без хештегів. Без CTA."
        ),
    })
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=250,
            system=system,
            messages=messages,
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log(f'  ⚠️ Помилка Claude (thread): {e}')
        return ''


def get_recent_responses_from_db(supabase_url: str, supabase_key: str, platform: str, hours: int = 24) -> list:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        r = httpx.get(
            f'{supabase_url}/rest/v1/outreach_responses',
            headers={'apikey': supabase_key, 'Authorization': f'Bearer {supabase_key}'},
            params={'platform': f'eq.{platform}', 'created_at': f'gte.{since}', 'select': '*'},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f'⚠️ Помилка читання з БД: {e}')
        return []


def save_response_to_db(
    supabase_url: str,
    supabase_key: str,
    platform_name: str,
    agent_type: str,
    language: str,
    external_post_id: Optional[str],
    external_thread_id: Optional[str],
    response_text: str,
    user_handle: Optional[str],
):
    payload = {
        'platform': platform_name,
        'agent_type': agent_type,
        'language': language.upper(),
        'external_post_id': external_post_id,
        'external_thread_id': external_thread_id,
        'response_text': response_text,
        'user_handle': user_handle,
    }
    try:
        r = httpx.post(
            f'{supabase_url}/rest/v1/outreach_responses',
            headers={
                'apikey': supabase_key,
                'Authorization': f'Bearer {supabase_key}',
                'Content-Type': 'application/json',
                'Prefer': 'return=minimal',
            },
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        log('  💾 Збережено в БД')
    except Exception as e:
        log(f'  ⚠️ Помилка збереження в БД: {e}')


def has_sent_dm_to_user(supabase_url: str, supabase_key: str, user_handle: str) -> bool:
    """Перевіряє чи надсилався DM цьому користувачу раніше."""
    try:
        r = httpx.get(
            f'{supabase_url}/rest/v1/outreach_responses',
            headers={
                'apikey': supabase_key,
                'Authorization': f'Bearer {supabase_key}',
                'Accept': 'application/json',
            },
            params={
                'platform': 'eq.INSTAGRAM_DM',
                'user_handle': f'eq.{user_handle}',
                'select': 'id',
                'limit': 1,
            },
            timeout=30,
        )
        r.raise_for_status()
        return len(r.json()) > 0
    except Exception as e:
        log(f'⚠️ Помилка перевірки DM для @{user_handle}: {e}')
        return False


def has_sent_fb_dm_to_user(supabase_url: str, supabase_key: str, user_handle: str) -> bool:
    """Перевіряє чи надсилався Facebook DM цьому користувачу раніше."""
    try:
        r = httpx.get(
            f'{supabase_url}/rest/v1/outreach_responses',
            headers={
                'apikey': supabase_key,
                'Authorization': f'Bearer {supabase_key}',
                'Accept': 'application/json',
            },
            params={
                'platform': 'eq.FACEBOOK_DM',
                'user_handle': f'eq.{user_handle}',
                'select': 'id',
                'limit': 1,
            },
            timeout=30,
        )
        r.raise_for_status()
        return len(r.json()) > 0
    except Exception as e:
        log(f'⚠️ Помилка перевірки FB DM для {user_handle}: {e}')
        return False


def send_instagram_dm(access_token: str, ig_user_id: str, recipient_id: str, text: str) -> Optional[str]:
    """Відправляє DM через Instagram Messaging API."""
    try:
        r = httpx.post(
            f'{GRAPH_API}/{ig_user_id}/messages',
            params={'access_token': access_token},
            json={
                'recipient': {'id': recipient_id},
                'message': {'text': text[:1000]},
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            err_msg = str(data['error'])
            if 'permission' in err_msg.lower() or '(#10)' in err_msg or 'not allowed' in err_msg.lower():
                raise PermissionError(f'Instagram DM permission denied: {err_msg}')
            raise RuntimeError(f'Meta API error: {data["error"]}')
        return data.get('id')
    except httpx.HTTPStatusError as e:
        err_text = e.response.text.lower()
        if 'permission' in err_text or '(#10)' in err_text or 'not allowed' in err_text:
            raise PermissionError(f'Instagram DM permission denied: {e}')
        raise


def send_facebook_private_reply(comment_id: str, page_token: str, text: str) -> Optional[str]:
    """Відправляє приватну відповідь на Facebook коментар (Private Replies API).
    
    Це дозволяє надіслати ОДНЕ приватне повідомлення в Messenger користувачу,
    який залишив коментар, без потреби в PSID.
    """
    try:
        r = httpx.post(
            f'{GRAPH_API}/{comment_id}/private_replies',
            params={'access_token': page_token, 'message': text[:2000]},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            err_msg = str(data['error'])
            if 'permission' in err_msg.lower() or '(#10)' in err_msg or 'not allowed' in err_msg.lower():
                raise PermissionError(f'Facebook private reply permission denied: {err_msg}')
            raise RuntimeError(f'Meta API error: {data["error"]}')
        return data.get('id')
    except httpx.HTTPStatusError as e:
        err_text = e.response.text.lower()
        if 'permission' in err_text or '(#10)' in err_text or 'not allowed' in err_text:
            raise PermissionError(f'Facebook private reply permission denied: {e}')
        raise


def process_pending_dms(
    monitor: 'InstagramMonitor',
    pending_dms: list,
    supabase_url: str,
    supabase_key: str,
) -> list:
    """Обробляє заплановані DM: відправляє готові, обробляє помилки."""
    now = time.time()
    remaining = []
    for dm in pending_dms:
        if dm.get('scheduled_at', 0) > now:
            remaining.append(dm)
            continue

        agent = dm['agent']
        ig_user_id = dm['ig_user_id']
        recipient_id = dm['recipient_id']
        username = dm['username']
        comment_id = dm['comment_id']
        text = dm['text']

        try:
            log(f'  📨 Відправка DM @{username} від {agent}...')
            send_instagram_dm(monitor.token, ig_user_id, recipient_id, text)
            log(f'    ✅ DM відправлено')
            save_response_to_db(
                supabase_url, supabase_key, 'INSTAGRAM_DM',
                agent, dm.get('lang', 'uk'), dm.get('media_id'), comment_id, text, username,
            )
        except PermissionError as e:
            log(f'    ❌ DM неможливий (permissions): {e}')
            fallback = FALLBACK_DM_PHRASE.get(agent, '')
            if fallback:
                try:
                    monitor.reply_to_comment(comment_id, fallback)
                    log(f'    ✅ Fallback коментар відправлено')
                except Exception as ce:
                    log(f'    ❌ Помилка fallback коментаря: {ce}')
            save_response_to_db(
                supabase_url, supabase_key, 'INSTAGRAM_DM_FAILED',
                agent, dm.get('lang', 'uk'), dm.get('media_id'), comment_id, f'FAILED: {e}', username,
            )
        except Exception as e:
            log(f'    ❌ Помилка DM: {e}')
            dm['attempts'] = dm.get('attempts', 0) + 1
            if dm['attempts'] < 3:
                remaining.append(dm)
            else:
                log(f'    🚫 DM для @{username} скасовано після 3 спроб')
                fallback = FALLBACK_DM_PHRASE.get(agent, '')
                if fallback:
                    try:
                        monitor.reply_to_comment(comment_id, fallback)
                        log(f'    ✅ Fallback коментар відправлено')
                    except Exception as ce:
                        log(f'    ❌ Помилка fallback коментаря: {ce}')
                save_response_to_db(
                    supabase_url, supabase_key, 'INSTAGRAM_DM_FAILED',
                    agent, dm.get('lang', 'uk'), dm.get('media_id'), comment_id, f'FAILED: {e}', username,
                )
    return remaining


def process_pending_fb_dms(
    fb: 'FacebookMonitor',
    pending_fbdms: list,
    supabase_url: str,
    supabase_key: str,
) -> list:
    """Обробляє заплановані Facebook DM: відправляє готові, обробляє помилки."""
    now = time.time()
    remaining = []
    for dm in pending_fbdms:
        if dm.get('scheduled_at', 0) > now:
            remaining.append(dm)
            continue

        agent = dm['agent']
        comment_id = dm['comment_id']
        page_token = dm['page_token']
        username = dm['username']
        text = dm['text']

        try:
            log(f'  📨 Відправка FB DM {username} від {agent}...')
            send_facebook_private_reply(comment_id, page_token, text)
            log(f'    ✅ FB DM відправлено')
            save_response_to_db(
                supabase_url, supabase_key, 'FACEBOOK_DM',
                agent, dm.get('lang', 'uk'), dm.get('post_id'), comment_id, text, username,
            )
        except PermissionError as e:
            log(f'    ❌ FB DM неможливий (permissions): {e}')
            fallback = FALLBACK_DM_PHRASE.get(agent, '')
            if fallback:
                try:
                    fb.reply_to_comment(comment_id, fallback, page_token)
                    log(f'    ✅ FB fallback коментар відправлено')
                except Exception as ce:
                    log(f'    ❌ Помилка FB fallback коментаря: {ce}')
            save_response_to_db(
                supabase_url, supabase_key, 'FACEBOOK_DM_FAILED',
                agent, dm.get('lang', 'uk'), dm.get('post_id'), comment_id, f'FAILED: {e}', username,
            )
        except Exception as e:
            log(f'    ❌ Помилка FB DM: {e}')
            dm['attempts'] = dm.get('attempts', 0) + 1
            if dm['attempts'] < 3:
                remaining.append(dm)
            else:
                log(f'    🚫 FB DM для {username} скасовано після 3 спроб')
                fallback = FALLBACK_DM_PHRASE.get(agent, '')
                if fallback:
                    try:
                        fb.reply_to_comment(comment_id, fallback, page_token)
                        log(f'    ✅ FB fallback коментар відправлено')
                    except Exception as ce:
                        log(f'    ❌ Помилка FB fallback коментаря: {ce}')
                save_response_to_db(
                    supabase_url, supabase_key, 'FACEBOOK_DM_FAILED',
                    agent, dm.get('lang', 'uk'), dm.get('post_id'), comment_id, f'FAILED: {e}', username,
                )
    return remaining


# ── Instagram Monitor ──────────────────────────────────────────────────────────

class InstagramMonitor:
    def __init__(self, access_token: str):
        self.token = access_token

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        p = {'access_token': self.token}
        if params:
            p.update(params)
        r = httpx.get(f'{GRAPH_API}{path}', params=p, timeout=30)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            raise RuntimeError(f'Meta API error: {data["error"]}')
        return data

    def _post(self, path: str, params: Optional[dict] = None) -> dict:
        p = {'access_token': self.token}
        if params:
            p.update(params)
        r = httpx.post(f'{GRAPH_API}{path}', params=p, timeout=30)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            raise RuntimeError(f'Meta API error: {data["error"]}')
        return data

    def get_media(self, ig_user_id: str, limit: int = 25) -> list:
        data = self._get(f'/{ig_user_id}/media', {'fields': 'id,caption,permalink', 'limit': limit})
        return data.get('data', [])

    def get_comments(self, media_id: str, limit: int = 50) -> list:
        data = self._get(f'/{media_id}/comments', {'fields': 'id,text,username,timestamp,from', 'limit': limit})
        return data.get('data', [])

    def get_replies(self, comment_id: str, limit: int = 50) -> list:
        try:
            data = self._get(
                f'/{comment_id}/replies',
                {'fields': 'id,text,username,timestamp,from', 'limit': limit},
            )
            return data.get('data', [])
        except Exception as e:
            log(f'    ⚠️ Помилка отримання replies для {comment_id}: {e}')
            return []

    def reply_to_comment(self, comment_id: str, message: str) -> Optional[str]:
        data = self._post(f'/{comment_id}/replies', {'message': message[:2200]})
        return data.get('id')


# ── Facebook Monitor ───────────────────────────────────────────────────────────

class FacebookMonitor:
    def __init__(self, user_access_token: str):
        self.user_token = user_access_token
        self._page_tokens: dict = {}  # page_id -> page_access_token

    def discover_pages(self) -> dict:
        """GET /me/accounts — повертає dict {page_id: {name, access_token}}."""
        try:
            r = httpx.get(
                f'{GRAPH_API}/me/accounts',
                params={
                    'access_token': self.user_token,
                    'fields': 'id,name,access_token',
                    'limit': 50,
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if 'error' in data:
                log(f'⚠️ FB /me/accounts помилка: {data["error"]}')
                return {}
            pages = {}
            for p in data.get('data', []):
                pages[p['id']] = {
                    'name': p.get('name', p['id']),
                    'access_token': p.get('access_token', self.user_token),
                }
                self._page_tokens[p['id']] = p.get('access_token', self.user_token)
            return pages
        except Exception as e:
            log(f'⚠️ Помилка discover_pages: {e}')
            return {}

    def get_page_token(self, page_id: str) -> str:
        return self._page_tokens.get(page_id, self.user_token)

    def get_feed(self, page_id: str, page_token: str, limit: int = 10) -> list:
        """GET /{page-id}/feed — останні пости сторінки."""
        try:
            r = httpx.get(
                f'{GRAPH_API}/{page_id}/feed',
                params={
                    'access_token': page_token,
                    'fields': 'id,message,created_time',
                    'limit': limit,
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if 'error' in data:
                raise RuntimeError(f'Meta API error: {data["error"]}')
            return data.get('data', [])
        except Exception as e:
            log(f'    ⚠️ Помилка feed для {page_id}: {e}')
            return []

    def get_post_comments(self, post_id: str, page_token: str, limit: int = 50) -> list:
        """GET /{post-id}/comments — коментарі під постом."""
        try:
            r = httpx.get(
                f'{GRAPH_API}/{post_id}/comments',
                params={
                    'access_token': page_token,
                    'fields': 'id,message,from,created_time',
                    'limit': limit,
                    'filter': 'stream',
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if 'error' in data:
                raise RuntimeError(f'Meta API error: {data["error"]}')
            return data.get('data', [])
        except Exception as e:
            log(f'    ⚠️ Помилка коментарів для {post_id}: {e}')
            return []

    def get_comment_replies(self, comment_id: str, page_token: str, limit: int = 50) -> list:
        """GET /{comment-id}/comments — відповіді на коментар."""
        try:
            r = httpx.get(
                f'{GRAPH_API}/{comment_id}/comments',
                params={
                    'access_token': page_token,
                    'fields': 'id,message,from,created_time',
                    'limit': limit,
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if 'error' in data:
                raise RuntimeError(f'Meta API error: {data["error"]}')
            return data.get('data', [])
        except Exception as e:
            log(f'    ⚠️ Помилка replies для {comment_id}: {e}')
            return []

    def reply_to_comment(self, comment_id: str, message: str, page_token: str) -> Optional[str]:
        """POST /{comment-id}/comments — відповідь від імені сторінки."""
        try:
            r = httpx.post(
                f'{GRAPH_API}/{comment_id}/comments',
                params={'access_token': page_token, 'message': message[:2200]},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if 'error' in data:
                raise RuntimeError(f'Meta API error: {data["error"]}')
            return data.get('id')
        except Exception as e:
            log(f'    ⚠️ Помилка reply FB {comment_id}: {e}')
            return None


# ── Instagram monitoring loop ──────────────────────────────────────────────────

def run_instagram_monitoring(
    monitor: InstagramMonitor,
    accounts: list,
    supabase_url: str,
    supabase_key: str,
    max_per_day: int,
    max_exchanges: int,
) -> int:
    state = get_monitor_state(supabase_url, supabase_key, 'INSTAGRAM')
    processed_ids: set = set(state.get('processed_comment_ids', []))
    our_reply_ids: set = set(state.get('our_reply_ids', []))
    last_response_at: dict = state.get('last_response_at', {})
    thread_histories: dict = state.get('thread_histories', {})
    pending_dms: list = state.get('pending_dms', [])

    cutoff = datetime.now(timezone.utc) - timedelta(days=THREAD_HISTORY_TTL_DAYS)
    thread_histories = {
        k: v for k, v in thread_histories.items()
        if datetime.fromisoformat(v.get('last_activity', '2000-01-01T00:00:00+00:00').replace('Z', '+00:00')) > cutoff
    }

    # Очистка старих pending DMs (старше 24 годин)
    cutoff_dm = time.time() - 86400
    pending_dms = [dm for dm in pending_dms if dm.get('scheduled_at', 0) > cutoff_dm]

    recent_responses = get_recent_responses_from_db(supabase_url, supabase_key, 'INSTAGRAM_COMMENT', hours=24)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total_processed = 0

    for acc in accounts:
        agent = acc['agent']
        ig_user_id = acc['ig_user_id']
        log(f'\n📸 [{agent}] Instagram {ig_user_id}...')

        agent_today_count = sum(
            1 for r in recent_responses
            if r.get('agent_type') == agent
            and datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) >= today_start
        )
        if agent_today_count >= max_per_day:
            log(f'  ⏸️ Денний ліміт {max_per_day} для {agent} (IG)')
            continue

        now_ts = time.time()
        if now_ts - last_response_at.get(agent, 0) < 300:
            log(f'  ⏸️ Пауза для {agent} (IG)')
            continue

        try:
            posts = monitor.get_media(ig_user_id, limit=10)
        except Exception as e:
            log(f'  ❌ Помилка постів IG: {e}')
            continue

        log(f'  📄 {len(posts)} постів')

        for post in posts:
            if agent_today_count >= max_per_day:
                break
            media_id = post['id']
            post_caption = (post.get('caption') or '')[:300]
            try:
                comments = monitor.get_comments(media_id, limit=50)
            except Exception as e:
                log(f'    ❌ Помилка коментарів {media_id}: {e}')
                continue

            for comment in comments:
                if agent_today_count >= max_per_day:
                    break

                comment_id = comment['id']
                text = comment.get('text', '')
                username = comment.get('username', '')

                if comment_id in our_reply_ids:
                    continue
                if not text or not username:
                    processed_ids.add(comment_id)
                    continue

                if comment_id not in processed_ids:
                    log(f'  💬 IG новий від @{username}: {text[:60]}')
                    lang = detect_language(text)

                    # Academy: окремий промпт, без CTA, без DM
                    if agent == 'ACADEMY':
                        reply_body = generate_academy_response(text, lang, _academy_gossip_cache)
                        full_reply = f'@{username}, {reply_body}' if reply_body else ''
                    else:
                        cta = IG_CTA_BY_LANG.get(lang, IG_DEFAULT_CTA)
                        reply_body = generate_first_response(
                            agent, text, lang, f'@{username}', cta, post_caption=post_caption)
                        full_reply = f'@{username}, {reply_body}\n\n{cta}' if reply_body else ''

                    if not full_reply:
                        processed_ids.add(comment_id)
                        continue

                    try:
                        time.sleep(random.randint(5, 15))
                        new_id = monitor.reply_to_comment(comment_id, full_reply)
                        log(f'    ✅ IG відповідь відправлена')
                        total_processed += 1
                        processed_ids.add(comment_id)
                        if new_id:
                            our_reply_ids.add(new_id)
                        last_response_at[agent] = time.time()
                        thread_histories[comment_id] = {
                            'exchange_count': 1,
                            'lang': lang,
                            'post_caption': post_caption,
                            'messages': [
                                {'role': 'user', 'text': text, 'username': username, 'comment_id': comment_id},
                                {'role': 'agent', 'text': full_reply, 'comment_id': new_id},
                            ],
                            'redirected': False,
                            'last_activity': datetime.now(timezone.utc).isoformat(),
                        }
                        save_response_to_db(supabase_url, supabase_key, 'INSTAGRAM_COMMENT',
                                            agent, lang, media_id, comment_id, full_reply, username)
                        recent_responses.append({'agent_type': agent, 'created_at': datetime.now(timezone.utc).isoformat()})
                        agent_today_count += 1

                        # DM тільки для магів (Академія DM не надсилає)
                        if agent != 'ACADEMY':
                            commenter_ig_id = comment.get('from', {}).get('id', '')
                            already_scheduled = any(d.get('username') == username for d in pending_dms)
                            if commenter_ig_id and not already_scheduled:
                                if not has_sent_dm_to_user(supabase_url, supabase_key, username):
                                    dm_text = AGENT_DM_TEXT.get(agent, '')
                                    if dm_text:
                                        pending_dms.append({
                                            'scheduled_at': time.time() + random.randint(120, 300),
                                            'agent': agent,
                                            'ig_user_id': ig_user_id,
                                            'recipient_id': commenter_ig_id,
                                            'username': username,
                                            'comment_id': comment_id,
                                            'media_id': media_id,
                                            'text': dm_text,
                                            'lang': lang,
                                            'attempts': 0,
                                        })
                                        log(f'    ⏳ DM заплановано для @{username}')
                                else:
                                    log(f'    ℹ️ DM для @{username} вже надсилався раніше')
                            else:
                                log(f'    ℹ️ Немає IG ID для @{username} або DM вже заплановано')
                    except Exception as e:
                        err_str = str(e)
                        if 'instagram_manage_comments' in err_str.lower() or '(#10)' in err_str:
                            log(f'    ❌ Немає дозволу instagram_manage_comments.')
                        else:
                            log(f'    ❌ Помилка IG reply: {e}')
                        processed_ids.add(comment_id)

                elif comment_id in thread_histories:
                    thread = thread_histories[comment_id]
                    if thread.get('redirected'):
                        continue

                    replies = monitor.get_replies(comment_id)
                    if not replies:
                        continue
                    log(f'  🔍 IG гілка @{username} ({thread["exchange_count"]} обмінів)')

                    for reply in replies:
                        if agent_today_count >= max_per_day:
                            break
                        reply_id = reply['id']
                        reply_text = reply.get('text', '')
                        reply_username = reply.get('username', '')
                        if reply_id in our_reply_ids or reply_id in processed_ids:
                            continue
                        if not reply_text or not reply_username:
                            processed_ids.add(reply_id)
                            continue

                        processed_ids.add(reply_id)
                        lang = thread.get('lang', detect_language(reply_text))
                        exchange_count = thread.get('exchange_count', 1)
                        thread_caption = thread.get('post_caption', '')

                        if exchange_count >= max_exchanges:
                            redirect_text = REDIRECT_MESSAGES.get(lang, REDIRECT_MESSAGES['en'])
                            full_redirect = f'@{reply_username}, {redirect_text}'
                            try:
                                time.sleep(random.randint(5, 15))
                                new_id = monitor.reply_to_comment(comment_id, full_redirect)
                                if new_id:
                                    our_reply_ids.add(new_id)
                                thread['redirected'] = True
                                thread['last_activity'] = datetime.now(timezone.utc).isoformat()
                                log(f'    🔄 IG редірект ({max_exchanges} обмінів)')
                                save_response_to_db(supabase_url, supabase_key, 'INSTAGRAM_COMMENT',
                                                    agent, lang, media_id, comment_id, full_redirect, reply_username)
                                agent_today_count += 1
                                last_response_at[agent] = time.time()
                            except Exception as e:
                                log(f'    ❌ Помилка IG редіректу: {e}')
                        else:
                            response_body = generate_thread_response(
                                agent, thread['messages'], reply_text, lang, f'@{reply_username}',
                                post_caption=thread_caption)
                            if not response_body:
                                continue
                            full_reply = f'@{reply_username}, {response_body}'
                            try:
                                time.sleep(random.randint(5, 15))
                                new_id = monitor.reply_to_comment(comment_id, full_reply)
                                log(f'    ✅ IG thread відповідь (обмін {exchange_count + 1})')
                                total_processed += 1
                                if new_id:
                                    our_reply_ids.add(new_id)
                                last_response_at[agent] = time.time()
                                thread['messages'].append({'role': 'user', 'text': reply_text,
                                                          'username': reply_username, 'comment_id': reply_id})
                                thread['messages'].append({'role': 'agent', 'text': full_reply, 'comment_id': new_id})
                                thread['exchange_count'] = exchange_count + 1
                                thread['last_activity'] = datetime.now(timezone.utc).isoformat()
                                save_response_to_db(supabase_url, supabase_key, 'INSTAGRAM_COMMENT',
                                                    agent, lang, media_id, comment_id, full_reply, reply_username)
                                recent_responses.append({'agent_type': agent, 'created_at': datetime.now(timezone.utc).isoformat()})
                                agent_today_count += 1
                            except Exception as e:
                                log(f'    ❌ Помилка IG thread reply: {e}')

    # Обробка запланованих DM
    if pending_dms:
        pending_dms = process_pending_dms(monitor, pending_dms, supabase_url, supabase_key)

    processed_ids = set(list(processed_ids)[-5000:])
    our_reply_ids = set(list(our_reply_ids)[-2000:])
    if len(thread_histories) > 1000:
        sorted_threads = sorted(thread_histories.items(),
                                key=lambda x: x[1].get('last_activity', ''), reverse=True)
        thread_histories = dict(sorted_threads[:1000])

    set_monitor_state(supabase_url, supabase_key, 'INSTAGRAM', {
        'processed_comment_ids': list(processed_ids),
        'our_reply_ids': list(our_reply_ids),
        'last_response_at': last_response_at,
        'thread_histories': thread_histories,
        'pending_dms': pending_dms,
    })
    return total_processed


# ── Facebook monitoring loop ───────────────────────────────────────────────────

def run_facebook_monitoring(
    fb: FacebookMonitor,
    fb_accounts: list,
    supabase_url: str,
    supabase_key: str,
    max_per_day: int,
    max_exchanges: int,
) -> int:
    if not fb_accounts:
        return 0

    state = get_monitor_state(supabase_url, supabase_key, 'FACEBOOK')
    processed_ids: set = set(state.get('processed_comment_ids', []))
    our_reply_ids: set = set(state.get('our_reply_ids', []))
    last_response_at: dict = state.get('last_response_at', {})
    thread_histories: dict = state.get('thread_histories', {})
    pending_fbdms: list = state.get('pending_fbdms', [])

    cutoff = datetime.now(timezone.utc) - timedelta(days=THREAD_HISTORY_TTL_DAYS)
    thread_histories = {
        k: v for k, v in thread_histories.items()
        if datetime.fromisoformat(v.get('last_activity', '2000-01-01T00:00:00+00:00').replace('Z', '+00:00')) > cutoff
    }

    # Очистка старих pending FB DMs (старше 24 годин)
    cutoff_dm = time.time() - 86400
    pending_fbdms = [dm for dm in pending_fbdms if dm.get('scheduled_at', 0) > cutoff_dm]

    recent_responses = get_recent_responses_from_db(supabase_url, supabase_key, 'FACEBOOK_COMMENT', hours=24)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total_processed = 0

    for acc in fb_accounts:
        agent = acc['agent']
        page_id = acc['page_id']
        page_token = acc.get('page_token', fb.get_page_token(page_id))

        log(f'\n📘 [{agent}] Facebook сторінка {page_id}...')

        agent_today_count = sum(
            1 for r in recent_responses
            if r.get('agent_type') == agent
            and datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) >= today_start
        )
        if agent_today_count >= max_per_day:
            log(f'  ⏸️ Денний ліміт {max_per_day} для {agent} (FB)')
            continue

        if time.time() - last_response_at.get(f'fb_{agent}', 0) < 300:
            log(f'  ⏸️ Пауза для {agent} (FB)')
            continue

        posts = fb.get_feed(page_id, page_token, limit=10)
        log(f'  📄 {len(posts)} постів')

        for post in posts:
            if agent_today_count >= max_per_day:
                break
            post_id = post['id']
            post_caption = (post.get('message') or '')[:300]
            comments = fb.get_post_comments(post_id, page_token, limit=50)

            for comment in comments:
                if agent_today_count >= max_per_day:
                    break

                comment_id = comment['id']
                text = comment.get('message', '')
                from_info = comment.get('from', {})
                username = from_info.get('name', '') or from_info.get('id', '')

                if comment_id in our_reply_ids:
                    continue
                if not text or not username:
                    processed_ids.add(comment_id)
                    continue

                if comment_id not in processed_ids:
                    log(f'  💬 FB новий від {username}: {text[:60]}')
                    lang = detect_language(text)

                    # Academy: окремий промпт, без CTA, без DM
                    if agent == 'ACADEMY':
                        reply_body = generate_academy_response(text, lang, _academy_gossip_cache)
                        full_reply = reply_body if reply_body else ''
                    else:
                        cta = FB_CTA_BY_LANG.get(lang, FB_DEFAULT_CTA)
                        reply_body = generate_first_response(
                            agent, text, lang, username, cta, post_caption=post_caption)
                        full_reply = f'{reply_body}\n\n{cta}' if reply_body else ''

                    if not full_reply:
                        processed_ids.add(comment_id)
                        continue

                    try:
                        time.sleep(random.randint(5, 15))
                        new_id = fb.reply_to_comment(comment_id, full_reply, page_token)
                        if new_id is None:
                            processed_ids.add(comment_id)
                            continue
                        log(f'    ✅ FB відповідь відправлена')
                        total_processed += 1
                        processed_ids.add(comment_id)
                        our_reply_ids.add(new_id)
                        last_response_at[f'fb_{agent}'] = time.time()
                        thread_histories[comment_id] = {
                            'exchange_count': 1,
                            'lang': lang,
                            'post_caption': post_caption,
                            'messages': [
                                {'role': 'user', 'text': text, 'username': username, 'comment_id': comment_id},
                                {'role': 'agent', 'text': full_reply, 'comment_id': new_id},
                            ],
                            'page_token': page_token,
                            'redirected': False,
                            'last_activity': datetime.now(timezone.utc).isoformat(),
                        }
                        save_response_to_db(supabase_url, supabase_key, 'FACEBOOK_COMMENT',
                                            agent, lang, post_id, comment_id, full_reply, username)
                        recent_responses.append({'agent_type': agent, 'created_at': datetime.now(timezone.utc).isoformat()})
                        agent_today_count += 1

                        # DM тільки для магів (Академія DM не надсилає)
                        if agent != 'ACADEMY':
                            commenter_id = from_info.get('id', '')
                            already_scheduled = any(d.get('username') == username for d in pending_fbdms)
                            if commenter_id and not already_scheduled:
                                if not has_sent_fb_dm_to_user(supabase_url, supabase_key, username):
                                    dm_text = FB_DM_MESSAGES.get(agent, '')
                                    if dm_text:
                                        pending_fbdms.append({
                                            'scheduled_at': time.time() + random.randint(120, 300),
                                            'agent': agent,
                                            'page_token': page_token,
                                            'comment_id': comment_id,
                                            'username': username,
                                            'post_id': post_id,
                                            'text': dm_text,
                                            'lang': lang,
                                            'attempts': 0,
                                        })
                                        log(f'    ⏳ FB DM заплановано для {username}')
                                else:
                                    log(f'    ℹ️ FB DM для {username} вже надсилався раніше')
                            else:
                                log(f'    ℹ️ Немає ID для {username} або FB DM вже заплановано')
                    except Exception as e:
                        log(f'    ❌ Помилка FB reply: {e}')
                        processed_ids.add(comment_id)

                elif comment_id in thread_histories:
                    thread = thread_histories[comment_id]
                    if thread.get('redirected'):
                        continue

                    # Для FB отримуємо replies через /comments
                    tok = thread.get('page_token', page_token)
                    replies = fb.get_comment_replies(comment_id, tok, limit=50)
                    if not replies:
                        continue
                    log(f'  🔍 FB гілка {username} ({thread["exchange_count"]} обмінів)')

                    for reply in replies:
                        if agent_today_count >= max_per_day:
                            break
                        reply_id = reply['id']
                        reply_text = reply.get('message', '')
                        reply_from = reply.get('from', {})
                        reply_username = reply_from.get('name', '') or reply_from.get('id', '')

                        if reply_id in our_reply_ids or reply_id in processed_ids:
                            continue
                        if not reply_text or not reply_username:
                            processed_ids.add(reply_id)
                            continue

                        processed_ids.add(reply_id)
                        lang = thread.get('lang', detect_language(reply_text))
                        exchange_count = thread.get('exchange_count', 1)
                        thread_caption = thread.get('post_caption', '')

                        if exchange_count >= max_exchanges:
                            redirect_text = REDIRECT_MESSAGES.get(lang, REDIRECT_MESSAGES['en'])
                            try:
                                time.sleep(random.randint(5, 15))
                                new_id = fb.reply_to_comment(comment_id, redirect_text, tok)
                                if new_id:
                                    our_reply_ids.add(new_id)
                                thread['redirected'] = True
                                thread['last_activity'] = datetime.now(timezone.utc).isoformat()
                                log(f'    🔄 FB редірект ({max_exchanges} обмінів)')
                                save_response_to_db(supabase_url, supabase_key, 'FACEBOOK_COMMENT',
                                                    agent, lang, post_id, comment_id, redirect_text, reply_username)
                                agent_today_count += 1
                                last_response_at[f'fb_{agent}'] = time.time()
                            except Exception as e:
                                log(f'    ❌ Помилка FB редіректу: {e}')
                        else:
                            response_body = generate_thread_response(
                                agent, thread['messages'], reply_text, lang, reply_username,
                                post_caption=thread_caption)
                            if not response_body:
                                continue
                            cta = FB_CTA_BY_LANG.get(lang, FB_DEFAULT_CTA)
                            full_reply = f'{response_body}\n\n{cta}'
                            try:
                                time.sleep(random.randint(5, 15))
                                new_id = fb.reply_to_comment(comment_id, full_reply, tok)
                                if new_id is None:
                                    continue
                                log(f'    ✅ FB thread відповідь (обмін {exchange_count + 1})')
                                total_processed += 1
                                our_reply_ids.add(new_id)
                                last_response_at[f'fb_{agent}'] = time.time()
                                thread['messages'].append({'role': 'user', 'text': reply_text,
                                                          'username': reply_username, 'comment_id': reply_id})
                                thread['messages'].append({'role': 'agent', 'text': full_reply, 'comment_id': new_id})
                                thread['exchange_count'] = exchange_count + 1
                                thread['last_activity'] = datetime.now(timezone.utc).isoformat()
                                save_response_to_db(supabase_url, supabase_key, 'FACEBOOK_COMMENT',
                                                    agent, lang, post_id, comment_id, full_reply, reply_username)
                                recent_responses.append({'agent_type': agent, 'created_at': datetime.now(timezone.utc).isoformat()})
                                agent_today_count += 1
                            except Exception as e:
                                log(f'    ❌ Помилка FB thread reply: {e}')

    # Обробка запланованих Facebook DM
    if pending_fbdms:
        pending_fbdms = process_pending_fb_dms(fb, pending_fbdms, supabase_url, supabase_key)

    processed_ids = set(list(processed_ids)[-5000:])
    our_reply_ids = set(list(our_reply_ids)[-2000:])
    if len(thread_histories) > 1000:
        sorted_threads = sorted(thread_histories.items(),
                                key=lambda x: x[1].get('last_activity', ''), reverse=True)
        thread_histories = dict(sorted_threads[:1000])

    set_monitor_state(supabase_url, supabase_key, 'FACEBOOK', {
        'processed_comment_ids': list(processed_ids),
        'our_reply_ids': list(our_reply_ids),
        'last_response_at': last_response_at,
        'thread_histories': thread_histories,
        'pending_fbdms': pending_fbdms,
    })
    return total_processed


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    required = ['ANTHROPIC_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'IG_ACCESS_TOKEN']
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log(f'❌ Відсутні змінні середовища: {", ".join(missing)}')
        sys.exit(1)

    supabase_url = (os.environ.get('SUPABASE_URL') or os.environ.get('NEXT_PUBLIC_SUPABASE_URL', '')).rstrip('/')
    supabase_key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    access_token = os.environ['IG_ACCESS_TOKEN']
    ig_max_per_day = int(os.environ.get('INSTAGRAM_MAX_PER_DAY', '20'))
    fb_max_per_day = int(os.environ.get('FACEBOOK_MAX_PER_DAY', '20'))
    max_exchanges = int(os.environ.get('INSTAGRAM_MAX_THREAD_EXCHANGES', str(MAX_THREAD_EXCHANGES)))

    # ── Instagram акаунти ──────────────────────────────────────────────────────
    ig_accounts = []
    for agent in AGENTS:
        ig_id = os.environ.get(f'{agent}_IG_USER_ID', '').strip()
        if ig_id:
            ig_accounts.append({'agent': agent, 'ig_user_id': ig_id})

    # Academy IG (якщо налаштовано)
    academy_ig_id = os.environ.get('ACADEMY_IG_USER_ID', '').strip()
    if academy_ig_id:
        ig_accounts.append({'agent': 'ACADEMY', 'ig_user_id': academy_ig_id})
        log(f'  🔮 Academy IG: {academy_ig_id}')

    # ── Facebook акаунти ──────────────────────────────────────────────────────
    fb = FacebookMonitor(access_token)
    fb_accounts = []
    missing_fb_ids = []
    for agent in AGENTS:
        page_id = os.environ.get(f'{agent}_FB_PAGE_ID', '').strip()
        page_token = os.environ.get(f'{agent}_PAGE_ACCESS_TOKEN', '').strip()
        if page_id and page_token:
            fb_accounts.append({'agent': agent, 'page_id': page_id, 'page_token': page_token})
        elif page_id:
            log(f'  ⚠️ {agent}_FB_PAGE_ID вказано але {agent}_PAGE_ACCESS_TOKEN відсутній — FB моніторинг для {agent} не працюватиме')
            missing_fb_ids.append(agent)
        else:
            missing_fb_ids.append(agent)

    # Academy FB (якщо налаштовано)
    academy_fb_page_id = os.environ.get('ACADEMY_FB_PAGE_ID', '').strip()
    academy_fb_token = os.environ.get('ACADEMY_PAGE_ACCESS_TOKEN', '').strip()
    if academy_fb_page_id and academy_fb_token:
        fb_accounts.append({'agent': 'ACADEMY', 'page_id': academy_fb_page_id, 'page_token': academy_fb_token})
        log(f'  🔮 Academy FB: {academy_fb_page_id}')

    # ── Плітки Академії (для відповідей на коментарі) ─────────────────────────
    global _academy_gossip_cache
    _academy_gossip_cache = fetch_active_gossip(supabase_url, supabase_key)
    if _academy_gossip_cache:
        log(f'  🔮 Завантажено {len(_academy_gossip_cache)} плітків Академії')

    if fb_accounts:
        log(f'\n🔍 Facebook акаунти: {len(fb_accounts)} сторінок')
        for acc in fb_accounts:
            log(f'   {acc["agent"]} → {acc["page_id"]}')
    else:
        log('\n⚠️ Не налаштовано жодного FB акаунту (потрібні {AGENT}_FB_PAGE_ID + {AGENT}_PAGE_ACCESS_TOKEN)')

    # ── Запуск моніторингу ─────────────────────────────────────────────────────
    platform = os.environ.get('MONITOR_PLATFORM', 'all').lower()
    log(f'\n▶ MONITOR_PLATFORM={platform}')

    total = 0
    monitor = InstagramMonitor(access_token)

    run_ig = platform in ('instagram', 'all')
    run_fb = platform in ('facebook', 'all')

    if run_ig:
        if ig_accounts:
            log(f'\n═══ Instagram моніторинг ({len(ig_accounts)} акаунтів) ═══')
            total += run_instagram_monitoring(monitor, ig_accounts, supabase_url, supabase_key,
                                              ig_max_per_day, max_exchanges)
        else:
            log('⚠️ Не налаштовано жодного IG_USER_ID')

    if run_fb:
        if fb_accounts:
            log(f'\n═══ Facebook моніторинг ({len(fb_accounts)} сторінок) ═══')
            total += run_facebook_monitoring(fb, fb_accounts, supabase_url, supabase_key,
                                             fb_max_per_day, max_exchanges)
        else:
            log('\nℹ️ Facebook моніторинг пропущено (немає {AGENT}_FB_PAGE_ID)')

    if run_ig and not ig_accounts and run_fb and not fb_accounts:
        log('❌ Не налаштовано жодного акаунту для моніторингу')
        sys.exit(0)

    log(f'\n✅ Готово. Всього відповідей: {total}')


if __name__ == '__main__':
    main()
