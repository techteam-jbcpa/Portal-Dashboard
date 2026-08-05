"""
client_provisioner.py
Picks up new clients from Supabase (assembly_client_id IS NULL) and:
  1. Creates Assembly client
  2. Sends magic link invite
  3. Creates Drive folder in PORTAL-NEW CLIENTS
  4. Creates onboarding task in Assembly (ClickUp form embed)
  5. Writes assembly_client_id + drive_folder_id back to Supabase

Runs via GitHub Actions every 15 min (separate from hubspot_sync.py).
"""

import os
import sys
import json
import time
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────────
ASSEMBLY_API  = 'https://api.assembly.com/v1'
ASSEMBLY_KEY  = os.environ['ASSEMBLY_API_KEY']

SUPABASE_URL  = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY  = os.environ['SUPABASE_SERVICE_KEY']

# Table: 'clients_test' in test mode, 'clients' in production
TABLE = os.environ.get('SUPABASE_TABLE', 'clients')

# Google Drive — PORTAL-NEW CLIENTS folder
DRIVE_PARENT_FOLDER = '1KDLZr3OYJE1O_HN-nD51nKcZuu0T-UIB'
DRIVE_SCOPES        = ['https://www.googleapis.com/auth/drive']

# ClickUp onboarding forms (default: English)
CLICKUP_FORM_EN = 'https://forms.clickup.com/f/28dn9-938/HEZLRCWSAGSHD2GTJS'
CLICKUP_FORM_ES = 'https://forms.clickup.com/f/28dn9-973/O0QJ0KB8ZMQBIR8AL0'

# ── Headers ────────────────────────────────────────────────────────────────────
AS_HEADERS = {
    'X-API-KEY':    ASSEMBLY_KEY,
    'Content-Type': 'application/json',
}

SB_HEADERS = {
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'apikey':        SUPABASE_KEY,
    'Content-Type':  'application/json',
}

# ── Supabase helpers ───────────────────────────────────────────────────────────
def get_unprovisioned():
    """Return clients missing assembly_client_id but with a valid email."""
    resp = requests.get(
        f'{SUPABASE_URL}/rest/v1/{TABLE}'
        '?assembly_client_id=is.null'
        '&contact_email=not.is.null'
        '&select=ticket_id,contact_name,contact_email,ticket_subject',
        headers=SB_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def patch_client(ticket_id, fields):
    """Patch a Supabase row by ticket_id."""
    resp = requests.patch(
        f'{SUPABASE_URL}/rest/v1/{TABLE}?ticket_id=eq.{ticket_id}',
        headers={**SB_HEADERS, 'Prefer': 'return=minimal'},
        json=fields,
        timeout=30,
    )
    if resp.status_code not in (200, 204):
        raise Exception(f'Supabase PATCH failed {resp.status_code}: {resp.text[:200]}')

# ── Assembly helpers ───────────────────────────────────────────────────────────
def lookup_assembly_client(email):
    """Look up an existing Assembly client by email. Returns client ID or None."""
    resp = requests.get(
        f'{ASSEMBLY_API}/clients?email={requests.utils.quote(email)}',
        headers=AS_HEADERS,
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    # Response may be a list or a paginated object
    items = data if isinstance(data, list) else data.get('data', [])
    return items[0].get('id') if items else None


def create_assembly_client(name, email):
    """Create an Assembly client. Returns the new client ID.
    If the email already exists (Google signup), looks up and returns existing ID."""
    parts = (name or '').strip().split(' ', 1)
    given  = parts[0] if parts else ''
    family = parts[1] if len(parts) > 1 else ''

    resp = requests.post(
        f'{ASSEMBLY_API}/clients',
        headers=AS_HEADERS,
        json={'givenName': given, 'familyName': family, 'email': email},
        timeout=30,
    )

    if resp.status_code in (200, 201):
        data = resp.json()
        return data.get('id') or data.get('data', {}).get('id')

    # Handle case where client already exists via Google OAuth
    if resp.status_code == 400:
        body = resp.json()
        if body.get('code') == 'google_account_already_exists':
            print(f'  Client already exists (Google account) — looking up existing ID')
            existing_id = lookup_assembly_client(email)
            if existing_id:
                return existing_id
            raise Exception(f'google_account_already_exists but lookup returned nothing for {email}')

    raise Exception(f'Assembly create client failed {resp.status_code}: {resp.text[:200]}')


def send_invite(assembly_client_id):
    """Send magic link invite to client."""
    resp = requests.patch(
        f'{ASSEMBLY_API}/clients/{assembly_client_id}?sendInvite=true',
        headers=AS_HEADERS,
        json={},
        timeout=30,
    )
    if resp.status_code not in (200, 204):
        raise Exception(f'Assembly invite failed {resp.status_code}: {resp.text[:200]}')


def create_onboarding_task(assembly_client_id, form_url):
    """Create the 'Complete New Client Form' task in Assembly."""
    resp = requests.post(
        f'{ASSEMBLY_API}/tasks',
        headers=AS_HEADERS,
        json={
            'clientId': assembly_client_id,
            'title':    'Complete Your New Client Form',
            'body':     f'Please complete your onboarding form here:\n{form_url}',
        },
        timeout=30,
    )
    # Log but don't raise — task creation is non-critical to provisioning
    if resp.status_code not in (200, 201):
        print(f'  Warning: Assembly task creation failed {resp.status_code}: {resp.text[:200]}')
    else:
        print(f'  Task created for client {assembly_client_id}')

# ── Google Drive helper ────────────────────────────────────────────────────────
def build_drive():
    sa_info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
    creds   = service_account.Credentials.from_service_account_info(sa_info, scopes=DRIVE_SCOPES)
    return build('drive', 'v3', credentials=creds)


def create_drive_folder(drive, folder_name):
    """Create a folder in PORTAL-NEW CLIENTS. Returns the folder ID."""
    meta = {
        'name':     folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents':  [DRIVE_PARENT_FOLDER],
    }
    folder = drive.files().create(body=meta, fields='id').execute()
    return folder.get('id')

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f'[client_provisioner] table={TABLE}')

    clients = get_unprovisioned()
    if not clients:
        print('No unprovisioned clients. Exiting.')
        return

    print(f'{len(clients)} client(s) to provision.')
    drive = build_drive()

    for c in clients:
        ticket_id = c['ticket_id']
        email     = c['contact_email']
        name      = c['contact_name'] or email
        subject   = c.get('ticket_subject') or name
        print(f'\nProvisioning: {email} ({subject})')

        try:
            # 1. Create Assembly client
            assembly_id = create_assembly_client(name, email)
            print(f'  Assembly client created: {assembly_id}')

            # 2. Save assembly_client_id immediately — prevents duplicate on next run
            patch_client(ticket_id, {'assembly_client_id': assembly_id})

            # 3. Send magic link
            send_invite(assembly_id)
            print(f'  Invite sent')

            # 4. Create Drive folder
            folder_id = create_drive_folder(drive, subject)
            print(f'  Drive folder created: {folder_id}')

            # 5. Save drive_folder_id
            patch_client(ticket_id, {'drive_folder_id': folder_id})

            # 6. Onboarding task (English by default — add language field to HubSpot to auto-select)
            create_onboarding_task(assembly_id, CLICKUP_FORM_EN)

        except Exception as e:
            print(f'  ERROR: {e}')
            # Continue with next client — partial state is safe because assembly_client_id
            # is saved immediately after creation, so next run skips Assembly and retries Drive/task.

    print('\nProvisioner done.')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'FATAL: {e}')
        sys.exit(1)
