#!/usr/bin/env python3
"""
TaskBot API для Mini App.
Принимает запросы от веб-приложения (index.html) внутри Telegram.

Эндпоинты (все через query-параметр action):
  GET  /api.py?action=list&initData=...                  -> список задач пользователя
  POST /api.py?action=create   body: {initData, task}     -> создать задачу
  POST /api.py?action=update   body: {initData, id, task} -> изменить задачу
  POST /api.py?action=delete   body: {initData, id}       -> удалить задачу
  POST /api.py?action=status   body: {initData, id, status} -> изменить статус
  POST /api.py?action=duplicate body: {initData, id}      -> дублировать задачу
"""
import sys
import os
import json
import hashlib
import hmac
import logging
from urllib.parse import parse_qsl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BOT_DIR)


def load_env():
    env_path = os.path.join(BOT_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ[key.strip()] = val.strip()


load_env()
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')


# ── Telegram initData verification ──────────────────────────────────────────
def verify_init_data(init_data: str, bot_token: str) -> dict:
    """
    Проверяет подпись initData от Telegram WebApp.
    Возвращает словарь с данными пользователя, либо None если подпись неверна.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop('hash', None)
        if not received_hash:
            return None

        data_check_arr = sorted(f"{k}={v}" for k, v in parsed.items())
        data_check_string = "\n".join(data_check_arr)

        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user_raw = parsed.get('user')
        if not user_raw:
            return None

        user = json.loads(user_raw)
        return user

    except Exception as e:
        logger.error(f"initData verification error: {e}")
        return None


# ── Dev fallback (для тестирования без Telegram) ────────────────────────────
def dev_user_fallback(parsed_body: dict):
    """
    Позволяет тестировать API напрямую через ?debug_user_id=123,
    ТОЛЬКО если включен DEBUG_MODE в .env. Никогда не использовать в проде
    без TaskBot токена.
    """
    if os.environ.get('DEBUG_MODE') == '1':
        uid = parsed_body.get('debug_user_id')
        if uid:
            return {'id': int(uid), 'first_name': 'Debug'}
    return None


def get_authenticated_user(parsed_body: dict):
    init_data = parsed_body.get('initData', '')
    user = verify_init_data(init_data, BOT_TOKEN)
    if user:
        return user
    return dev_user_fallback(parsed_body)


# ── Serializers ──────────────────────────────────────────────────────────────
def task_to_json(t: dict) -> dict:
    return {
        'id': t['id'],
        'title': t['title'],
        'note': t.get('note') or '',
        'scheduled_at': t['scheduled_at'],
        'deadline': t.get('deadline'),
        'priority': t.get('priority', 'medium'),
        'status': t.get('status', 'pending'),
        'link': t.get('link'),
    }


# ── Handlers ─────────────────────────────────────────────────────────────────
def handle_list(db, user, body):
    tasks = db.get_user_tasks(user['id'])
    return {'ok': True, 'tasks': [task_to_json(t) for t in tasks]}


def handle_create(db, user, body):
    task = body.get('task', {})
    title = (task.get('title') or '').strip()
    if not title:
        return {'ok': False, 'error': 'title_required'}

    db.ensure_user(user['id'], user.get('first_name', ''))

    new_id = db.add_task(
        user_id=user['id'],
        title=title,
        note=task.get('note', '') or '',
        scheduled_at=task.get('scheduled_at'),
        deadline=task.get('deadline'),
        priority=task.get('priority', 'medium'),
        link=task.get('link'),
    )
    created = db.get_task(new_id)
    return {'ok': True, 'task': task_to_json(created)}


def handle_update(db, user, body):
    task_id = body.get('id')
    task = body.get('task', {})
    existing = db.get_task(task_id)
    if not existing or existing['user_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}

    fields_map = {
        'title': task.get('title'),
        'note': task.get('note'),
        'scheduled_at': task.get('scheduled_at'),
        'deadline': task.get('deadline'),
        'priority': task.get('priority'),
        'link': task.get('link'),
    }
    for field, value in fields_map.items():
        if value is not None:
            db.update_task_field(task_id, field, value)

    updated = db.get_task(task_id)
    return {'ok': True, 'task': task_to_json(updated)}


def handle_delete(db, user, body):
    task_id = body.get('id')
    existing = db.get_task(task_id)
    if not existing or existing['user_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}
    db.delete_task(task_id)
    return {'ok': True}


def handle_status(db, user, body):
    task_id = body.get('id')
    status = body.get('status')
    if status not in ('pending', 'done', 'irrelevant'):
        return {'ok': False, 'error': 'invalid_status'}
    existing = db.get_task(task_id)
    if not existing or existing['user_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}
    db.update_task_status(task_id, status)
    updated = db.get_task(task_id)
    return {'ok': True, 'task': task_to_json(updated)}


def handle_duplicate(db, user, body):
    task_id = body.get('id')
    existing = db.get_task(task_id)
    if not existing or existing['user_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}
    new_id = db.duplicate_task(task_id)
    created = db.get_task(new_id)
    return {'ok': True, 'task': task_to_json(created)}


# ── Team API ─────────────────────────────────────────────────────────────────

def member_to_json(m: dict) -> dict:
    return {
        'id':       m['id'],
        'username': m['username'],
        'name':     m.get('name') or '',
        'role':     m.get('role') or '',
        'user_id':  m.get('user_id'),
        'active':   m.get('user_id') is not None,
        'created_at': m.get('created_at', ''),
    }


def assignment_to_json(a: dict) -> dict:
    return {
        'id':             a['id'],
        'member_id':      a['member_id'],
        'member_name':    a.get('member_name') or '',
        'member_role':    a.get('member_role') or '',
        'member_username': a.get('member_username') or '',
        'title':          a['title'],
        'note':           a.get('note') or '',
        'scheduled_at':   a['scheduled_at'],
        'status':         a.get('status', 'pending'),
        'decline_reason': a.get('decline_reason'),
        'created_at':     a.get('created_at', ''),
    }


def handle_team_list(db, user, body):
    members = db.get_members(user['id'])
    assignments = db.get_assignments_for_owner(user['id'])
    return {
        'ok': True,
        'members': [member_to_json(m) for m in members],
        'assignments': [assignment_to_json(a) for a in assignments],
    }


def handle_team_add_member(db, user, body):
    username = (body.get('username') or '').strip().lstrip('@')
    name = (body.get('name') or '').strip()
    role = (body.get('role') or '').strip()
    if not username:
        return {'ok': False, 'error': 'username_required'}
    mid = db.add_member(user['id'], username, name, role)
    if not mid:
        return {'ok': False, 'error': 'already_exists'}
    member = db.get_member(mid)
    return {'ok': True, 'member': member_to_json(member)}


def handle_team_update_member(db, user, body):
    member_id = body.get('member_id')
    name = (body.get('name') or '').strip()
    role = (body.get('role') or '').strip()
    member = db.get_member(member_id)
    if not member or member['owner_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}
    db.update_member(member_id, name, role)
    updated = db.get_member(member_id)
    return {'ok': True, 'member': member_to_json(updated)}


def handle_team_delete_member(db, user, body):
    member_id = body.get('member_id')
    member = db.get_member(member_id)
    if not member or member['owner_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}
    db.delete_member(member_id)
    return {'ok': True}


def handle_team_assign(db, user, body):
    member_id = body.get('member_id')
    title = (body.get('title') or '').strip()
    note = (body.get('note') or '').strip()
    scheduled_at = body.get('scheduled_at', '')
    if not title:
        return {'ok': False, 'error': 'title_required'}
    member = db.get_member(member_id)
    if not member or member['owner_id'] != user['id']:
        return {'ok': False, 'error': 'member_not_found'}
    aid = db.add_assignment(user['id'], member_id, title, note, scheduled_at)
    assignment = db.get_assignment(aid)
    return {'ok': True, 'assignment': assignment_to_json(assignment)}


def handle_team_delete_assignment(db, user, body):
    assignment_id = body.get('assignment_id')
    assignment = db.get_assignment(assignment_id)
    if not assignment or assignment['owner_id'] != user['id']:
        return {'ok': False, 'error': 'not_found'}
    db.delete_assignment(assignment_id)
    return {'ok': True}


HANDLERS = {
    'list': handle_list,
    'create': handle_create,
    'update': handle_update,
    'delete': handle_delete,
    'status': handle_status,
    'duplicate': handle_duplicate,
    # Team
    'team_list':             handle_team_list,
    'team_add_member':       handle_team_add_member,
    'team_update_member':    handle_team_update_member,
    'team_delete_member':    handle_team_delete_member,
    'team_assign':           handle_team_assign,
    'team_delete_assignment': handle_team_delete_assignment,
}


# ── WSGI application ─────────────────────────────────────────────────────────
def application(environ, start_response):
    def respond(status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = [
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Access-Control-Allow-Origin', '*'),
            ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type'),
        ]
        start_response(status_code, headers)
        return [body]

    method = environ.get('REQUEST_METHOD', 'GET')

    if method == 'OPTIONS':
        return respond('200 OK', {'ok': True})

    query_string = environ.get('QUERY_STRING', '')
    query = dict(parse_qsl(query_string))
    action = query.get('action', '')

    try:
        content_length = int(environ.get('CONTENT_LENGTH', 0) or 0)
        raw_body = environ['wsgi.input'].read(content_length) if content_length > 0 else b''
    except Exception:
        raw_body = b''

    if method == 'GET':
        body = dict(query)
    else:
        try:
            body = json.loads(raw_body.decode('utf-8')) if raw_body else {}
        except Exception as e:
            return respond('400 Bad Request', {'ok': False, 'error': 'invalid_json'})

    if action not in HANDLERS:
        return respond('404 Not Found', {'ok': False, 'error': 'unknown_action'})

    user = get_authenticated_user(body)
    if not user:
        return respond('401 Unauthorized', {'ok': False, 'error': 'unauthorized'})

    try:
        from database import Database
        db = Database(os.path.join(BOT_DIR, 'tasks.db'))
        result = HANDLERS[action](db, user, body)
        return respond('200 OK', result)
    except Exception as e:
        logger.error(f"API error in action={action}: {e}", exc_info=True)
        return respond('500 Internal Server Error', {'ok': False, 'error': 'internal_error'})


app = application
