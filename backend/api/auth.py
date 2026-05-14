"""GitHub OAuth login.

Flow:
  1. Frontend opens /api/auth/github/login → 302 to GitHub authorize page.
  2. GitHub redirects user back to /api/auth/github/callback?code=...&state=...
  3. We exchange the code for an access token, fetch the user profile,
     upsert a row in `users`, and issue a JWT.
  4. We redirect to FRONTEND_URL with the JWT in the URL fragment so the
     frontend can pick it up and stash it in localStorage.

The legacy username/password endpoints are gone — there is no register or
login form anymore. Admin status is derived at runtime from
`github_login == ADMIN_GITHUB_LOGIN`.
"""
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import jwt
import requests
from flask import Blueprint, request, jsonify, redirect

from db.connection import db
from db.models import User

auth_bp = Blueprint('auth', __name__)

SECRET_KEY          = os.getenv('SECRET_KEY', 'wildfire-secret-key-change-in-production')
GH_CLIENT_ID        = os.getenv('GITHUB_OAUTH_CLIENT_ID', '')
GH_CLIENT_SECRET    = os.getenv('GITHUB_OAUTH_CLIENT_SECRET', '')
ADMIN_GITHUB_LOGIN  = os.getenv('ADMIN_GITHUB_LOGIN', 'geo-raypan')
FRONTEND_URL        = os.getenv('FRONTEND_URL', 'https://wildfire-ai.com')

GH_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
GH_TOKEN_URL     = 'https://github.com/login/oauth/access_token'
GH_USER_URL      = 'https://api.github.com/user'

# In-memory CSRF state store. Single-process deploy is fine for this scale;
# if we ever go multi-worker we'd swap this for Redis or a signed cookie.
_oauth_states: set[str] = set()


def _is_admin(github_login: str) -> bool:
    return github_login.lower() == ADMIN_GITHUB_LOGIN.lower()


def _issue_jwt(user: User) -> str:
    return jwt.encode({
        'user_id':      user.id,
        'github_id':    user.github_id,
        'github_login': user.github_login,
        'avatar_url':   user.avatar_url,
        'is_admin':     _is_admin(user.github_login),
        'exp':          datetime.utcnow() + timedelta(hours=24),
    }, SECRET_KEY, algorithm='HS256')


@auth_bp.route('/github/login', methods=['GET'])
def github_login():
    if not GH_CLIENT_ID:
        return jsonify({'message': 'GitHub OAuth not configured.'}), 500
    state = secrets.token_urlsafe(24)
    _oauth_states.add(state)
    # Build callback from FRONTEND_URL so the scheme/host match GitHub's OAuth
    # App registration even when Flask sits behind a TLS-terminating proxy
    # (Cloudflare/Caddy) that strips https → http on the way to the origin.
    redirect_uri = f"{FRONTEND_URL.rstrip('/')}/api/auth/github/callback"
    params = {
        'client_id':    GH_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'scope':        'read:user user:email',
        'state':        state,
    }
    return redirect(f"{GH_AUTHORIZE_URL}?{urlencode(params)}")


@auth_bp.route('/github/callback', methods=['GET'])
def github_callback():
    code  = request.args.get('code')
    state = request.args.get('state')
    if not code or not state or state not in _oauth_states:
        return jsonify({'message': 'Invalid OAuth callback.'}), 400
    _oauth_states.discard(state)

    # Exchange code for access token
    token_resp = requests.post(
        GH_TOKEN_URL,
        headers={'Accept': 'application/json'},
        data={
            'client_id':     GH_CLIENT_ID,
            'client_secret': GH_CLIENT_SECRET,
            'code':          code,
        },
        timeout=10,
    )
    if not token_resp.ok:
        return jsonify({'message': 'Failed to exchange code with GitHub.'}), 502
    access_token = token_resp.json().get('access_token')
    if not access_token:
        return jsonify({'message': 'GitHub did not return an access token.'}), 502

    # Fetch user profile
    profile = requests.get(
        GH_USER_URL,
        headers={'Authorization': f'token {access_token}', 'Accept': 'application/json'},
        timeout=10,
    )
    if not profile.ok:
        return jsonify({'message': 'Failed to fetch GitHub profile.'}), 502
    p = profile.json()

    github_id    = p.get('id')
    github_login = p.get('login')
    if not github_id or not github_login:
        return jsonify({'message': 'GitHub profile missing required fields.'}), 502

    # Upsert user
    user = User.query.filter_by(github_id=github_id).first()
    if not user:
        user = User(
            github_id    = github_id,
            github_login = github_login,
            avatar_url   = p.get('avatar_url'),
            email        = p.get('email'),
        )
        db.session.add(user)
    else:
        user.github_login = github_login
        user.avatar_url   = p.get('avatar_url') or user.avatar_url
        user.email        = p.get('email') or user.email
    db.session.commit()

    token = _issue_jwt(user)
    # Hand the token back via URL fragment so it never hits server logs.
    return redirect(f"{FRONTEND_URL}/#token={token}")


@auth_bp.route('/verify', methods=['GET'])
def verify():
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'message': 'No token provided.'}), 401
    token = auth_header.split(' ', 1)[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return jsonify({
            'valid':        True,
            'github_login': payload.get('github_login'),
            'avatar_url':   payload.get('avatar_url'),
            'is_admin':     bool(payload.get('is_admin', False)),
        }), 200
    except jwt.ExpiredSignatureError:
        return jsonify({'message': 'Token expired.'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'message': 'Invalid token.'}), 401
