"""
hubspot_sync.py
Polls HubSpot Expansion Pipeline tickets → upserts to Supabase clients table.
Only manages: ticket_id, contact_email, contact_name, company_name, ticket_status.
Does NOT overwrite: assembly_client_id, drive_folder_id, file_channel_id (managed separately).

Run via GitHub Actions every 15 minutes.
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
HUBSPOT_TOKEN      = os.environ['HUBSPOT_TOKEN']
SUPABASE_URL       = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY       = os.environ['SUPABASE_SERVICE_KEY']

EXPANSION_PIPELINE = '0'   # Expansion Pipeline internal ID
MAX_TICKETS        = int(os.environ.get('MAX_TICKETS', 0))  # 0 = no limit (production)

# HubSpot stage ID → human-readable name stored in Supabase
STAGE_MAP = {
    '1':          'New',
    '2':          'Onboarding/Info Request',
    '154532707':  'Waiting for Client',
    '3':          'Work in Process in CU',
    '132020383':  'Step Two Financial Accounts',
    '1035426362': 'Waiting For ITIN',
    '154512785':  'Unresponsive',
    '1030108297': 'Delinquent/Missing Payment',
    '4':          'Closed',
    '1408595291': 'On Hold by Client',
}

# ── HTTP helpers ───────────────────────────────────────────────────────────────
HS_HEADERS = {
    'Authorization': f'Bearer {HUBSPOT_TOKEN}',
    'Content-Type':  'application/json',
}

SB_HEADERS = {
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'apikey':        SUPABASE_KEY,
    'Content-Type':  'application/json',
}

def hs_post(path, body, retries=4):
    """POST to HubSpot with automatic retry on 429 rate limit."""
    url = f'https://api.hubapi.com{path}'
    for attempt in range(retries):
        resp = requests.post(url, headers=HS_HEADERS, json=body, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get('Retry-After', 10)) + 2
            print(f'  Rate limited — waiting {wait}s (attempt {attempt+1}/{retries})')
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise Exception(f'HubSpot rate limit exceeded after {retries} retries: {path}')

# ── Step 1: fetch tickets in Expansion Pipeline ───────────────────────────────
# Fetches all tickets in the pipeline, then filters in Python:
#   - Open tickets: always included
#   - Closed tickets: only last 90 days (old ones not needed in portal)
CLOSED_STAGE_ID = '4'
CLOSED_CUTOFF   = datetime.now(timezone.utc) - timedelta(days=90)

def get_all_tickets():
    all_tickets = []
    after       = None

    while True:
        body = {
            'filterGroups': [{
                'filters': [{
                    'propertyName': 'hs_pipeline',
                    'operator':     'EQ',
                    'value':        EXPANSION_PIPELINE,
                }]
            }],
            'properties': ['subject', 'hs_pipeline_stage', 'createdate'],
            'sorts': [{'propertyName': 'createdate', 'direction': 'DESCENDING'}],
            'limit': 100,
        }
        if after:
            body['after'] = after

        data    = hs_post('/crm/v3/objects/tickets/search', body)
        results = data.get('results', [])
        all_tickets.extend(results)
        print(f'  Fetched {len(results)} tickets (total so far: {len(all_tickets)})')

        after = data.get('paging', {}).get('next', {}).get('after')
        if not after:
            break

        time.sleep(1)   # 1s pause between pages to stay under rate limit

        # In test mode, one page is enough — stop after fetching first batch
        if MAX_TICKETS:
            break

    # Filter out old closed tickets in Python
    tickets = []
    skipped = 0
    for t in all_tickets:
        props    = t.get('properties', {})
        stage_id = str(props.get('hs_pipeline_stage', ''))
        if stage_id == CLOSED_STAGE_ID:
            created_str = props.get('createdate', '') or ''
            created     = datetime.fromisoformat(created_str.replace('Z', '+00:00')) if created_str else CLOSED_CUTOFF
            if created < CLOSED_CUTOFF:
                skipped += 1
                continue
        tickets.append(t)

    if skipped:
        print(f'  Skipped {skipped} old closed tickets (>90 days)')

    # Apply MAX_TICKETS limit AFTER filtering so we always get real tickets
    if MAX_TICKETS and len(tickets) > MAX_TICKETS:
        tickets = tickets[:MAX_TICKETS]
        print(f'  Limited to {MAX_TICKETS} tickets (test mode)')

    return tickets

# ── Step 2: ticket → contact associations (batch) ────────────────────────────
def get_contact_associations(ticket_ids):
    """Returns dict: ticket_id -> first_contact_id"""
    if not ticket_ids:
        return {}

    data   = hs_post('/crm/v3/associations/tickets/contacts/batch/read',
                     {'inputs': [{'id': tid} for tid in ticket_ids]})
    result = {}
    for item in data.get('results', []):
        tid         = item['from']['id']
        contact_ids = [t['id'] for t in item.get('to', [])]
        if contact_ids:
            result[tid] = contact_ids[0]   # take first associated contact
    return result

# ── Step 3: contact details (batch) ──────────────────────────────────────────
def get_contacts_batch(contact_ids):
    """Returns dict: contact_id -> {email, name, company}"""
    if not contact_ids:
        return {}

    data   = hs_post('/crm/v3/objects/contacts/batch/read', {
        'properties': ['email', 'firstname', 'lastname', 'company'],
        'inputs':     [{'id': cid} for cid in contact_ids],
    })
    result = {}
    for c in data.get('results', []):
        p = c.get('properties', {})
        result[c['id']] = {
            'email':   p.get('email', '') or '',
            'name':    f"{p.get('firstname','') or ''} {p.get('lastname','') or ''}".strip(),
            'company': p.get('company', '') or '',
        }
    return result

# ── Step 4: upsert to Supabase ────────────────────────────────────────────────
def upsert_batch(records):
    resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/clients?on_conflict=ticket_id',
        headers={**SB_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
        json=records,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f'  Supabase error {resp.status_code}: {resp.text[:300]}')
        resp.raise_for_status()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f'[{now}] HubSpot → Supabase sync starting...')

    # 1. Tickets
    tickets = get_all_tickets()
    if not tickets:
        print('No tickets found. Exiting.')
        return
    print(f'Total tickets: {len(tickets)}')

    ticket_ids = [t['id'] for t in tickets]

    # 2. Associations (batch of 100)
    associations = {}
    for i in range(0, len(ticket_ids), 100):
        associations.update(get_contact_associations(ticket_ids[i:i+100]))
    print(f'Tickets with contact: {len(associations)}')

    # 3. Contacts (batch of 100)
    unique_contact_ids = list(set(associations.values()))
    contacts = {}
    for i in range(0, len(unique_contact_ids), 100):
        contacts.update(get_contacts_batch(unique_contact_ids[i:i+100]))
    print(f'Contacts fetched: {len(contacts)}')

    # 4. Build Supabase records
    records     = []
    no_email    = []

    for ticket in tickets:
        tid       = ticket['id']
        props     = ticket.get('properties', {})
        stage_id  = str(props.get('hs_pipeline_stage', ''))
        stage     = STAGE_MAP.get(stage_id, f'Unknown ({stage_id})')

        contact_id = associations.get(tid)
        contact    = contacts.get(contact_id, {}) if contact_id else {}
        email      = contact.get('email', '')

        if not email:
            no_email.append(tid)

        records.append({
            'ticket_id':     tid,
            'contact_email': email,
            'contact_name':  contact.get('name', ''),
            'company_name':  contact.get('company', ''),
            'ticket_status': stage,
        })

    if no_email:
        print(f'Warning: {len(no_email)} tickets have no contact email: {no_email[:10]}')

    # 5. Upsert in batches of 50
    for i in range(0, len(records), 50):
        batch = records[i:i+50]
        upsert_batch(batch)
        print(f'  Upserted records {i+1}–{i+len(batch)}')

    print(f'Sync complete. {len(records)} tickets processed.')

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'FATAL: {e}')
        sys.exit(1)
