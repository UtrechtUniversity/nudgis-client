#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Update managers_email for course channels based on kanaal_emails.csv.

Channel structure assumed:
  Level 1 (primary): Faculty channels
  Level 2 (sub):     Course channels — title starts with the course code

For each course channel the first space-separated word of the title is matched
against the CURSUS column in kanaal_emails.csv.  When a match is found the
channel's managers_email is set to the corresponding E_MAIL_ADRES and the
"Use default value" flag is disabled.

NOTE: The exact API field name for the "Use default value" toggle may differ
from 'managers_email_is_default' depending on your MediaServer version.
Inspect the channels/edit/ API docs or a browser network trace to confirm.
'''
import argparse
import csv
import os
import sys


if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ms_client.client import MediaServerClient

    parser = argparse.ArgumentParser(
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        default='myconfig.json',
        help='Path to the configuration file.',
        type=str,
    )
    parser.add_argument(
        '--csv',
        default='kanaal_emails.csv',
        help='Path to the kanaal_emails CSV file.',
        type=str,
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print what would be changed without making any API calls.',
    )
    args = parser.parse_args()

    # -------------------------------------------------------------------------
    # Load CSV: build a lookup dict  CURSUS -> E_MAIL_ADRES
    # -------------------------------------------------------------------------
    cursus_email = {}
    with open(args.csv, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cursus = row.get('CURSUS', '').strip()
            email = row.get('E_MAIL_ADRES', '').strip()
            if cursus and email:
                cursus_email[cursus] = email

    print(f'Loaded {len(cursus_email)} CURSUS->email mapping(s) from {args.csv}')
    if not cursus_email:
        print('No usable rows found in CSV. Exiting.')
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Connect and fetch the full channel catalog
    # -------------------------------------------------------------------------
    msc = MediaServerClient(args.conf)
    print(msc.api('/'))

    print('Fetching channel catalog...')
    catalog = msc.get_catalog(fmt='flat')
    all_channels = catalog.get('channels', [])
    print(f'Total channels in catalog: {len(all_channels)}')

    # -------------------------------------------------------------------------
    # Identify level-1 (faculty) and level-2 (course) channels
    # -------------------------------------------------------------------------
    top_level_oids = {ch['oid'] for ch in all_channels if not ch.get('parent_oid')}
    course_channels = [ch for ch in all_channels if ch.get('parent_oid') in top_level_oids]

    print(f'Faculty channels (level 1): {len(top_level_oids)}')
    print(f'Course channels  (level 2): {len(course_channels)}')
    print()

    # -------------------------------------------------------------------------
    # Match each course channel against the CSV and update
    # -------------------------------------------------------------------------
    matched = 0
    unmatched = 0

    for channel in course_channels:
        oid = channel['oid']
        title = channel.get('title', '').strip()
        words = title.split()
        course_code = words[0] if words else ''

        if not course_code:
            print(f'SKIP (empty title): oid={oid}')
            unmatched += 1
            continue

        if course_code in cursus_email:
            email = cursus_email[course_code]
            print(f'MATCH  [{course_code}] "{title}"  ->  {email}  (oid={oid})')
            if not args.dry_run:
                try:
                    msc.api(
                        'channels/edit/',
                        method='post',
                        data={
                            'oid': oid,
                            'managers_email': email,
                            # Disable "Use default value" for managers_email.
                            # Adjust field name below if the API uses a different key.
                            'managers_email_is_default': 'false',
                        },
                    )
                    print(f'       Updated successfully.')
                except Exception as e:
                    print(f'       ERROR: {e}')
            matched += 1
        else:
            print(f'NO MATCH [{course_code}] "{title}"  (oid={oid})')
            unmatched += 1

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print()
    print(f'Done: {matched} channel(s) matched, {unmatched} channel(s) unmatched.')
    if args.dry_run:
        print('(dry run — no changes were made)')
