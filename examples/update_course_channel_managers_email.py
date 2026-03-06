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

A report is written to a CSV file showing, for every course channel, the
old managers_email and what it was changed to (or that no CSV match was found).

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
        '--report',
        default='update_managers_email_report.csv',
        help='Path to the output report CSV file.',
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
    # Match each course channel against the CSV, update, and collect report rows
    # -------------------------------------------------------------------------
    report_rows = []
    matched = 0
    unmatched = 0

    for channel in course_channels:
        oid = channel['oid']
        title = channel.get('title', '').strip()
        words = title.split()
        course_code = words[0] if words else ''

        # Fetch the current managers_email from the API
        try:
            info = msc.api('channels/get/', params={'oid': oid, 'full': 'yes'})['info']
            old_email = info.get('managers_email') or ''
        except Exception as e:
            old_email = f'(error fetching: {e})'

        if not course_code:
            report_rows.append({
                'Match': 'no',
                'oid': oid,
                'code': '',
                'Course name': title,
                'old email': old_email,
                'new email': '',
            })
            unmatched += 1
            continue

        if course_code in cursus_email:
            new_email = cursus_email[course_code]
            match_value = 'yes'
            error = None
            if not args.dry_run:
                try:
                    msc.api(
                        'channels/edit/',
                        method='post',
                        data={
                            'oid': oid,
                            'managers_email': new_email,
                            # Disable "Use default value" for managers_email.
                            # Adjust field name below if the API uses a different key.
                            'managers_email_is_default': 'false',
                        },
                    )
                except Exception as e:
                    match_value = 'error'
                    error = str(e)

            msg = f'[{"ERROR" if error else "UPDATED"}] {course_code} | "{title}" | {old_email} -> {new_email}'
            if error:
                msg += f' | {error}'
            print(msg)
            report_rows.append({
                'Match': match_value,
                'oid': oid,
                'code': course_code,
                'Course name': title,
                'old email': old_email,
                'new email': new_email,
            })
            matched += 1
        else:
            report_rows.append({
                'Match': 'no',
                'oid': oid,
                'code': course_code,
                'Course name': title,
                'old email': old_email,
                'new email': '',
            })
            unmatched += 1

    # -------------------------------------------------------------------------
    # Write report CSV
    # -------------------------------------------------------------------------
    fieldnames = ['Match', 'oid', 'code', 'Course name', 'old email', 'new email']
    with open(args.report, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    print()
    print(f'Done: {matched} channel(s) matched, {unmatched} channel(s) unmatched.')
    print(f'Report written to: {args.report}')
    if args.dry_run:
        print('(dry run — no changes were made)')
