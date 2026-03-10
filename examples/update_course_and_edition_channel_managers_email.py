#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Update managers_emails for course channels AND their edition sub-channels,
based on kanaal_emails.csv.

Channel structure assumed:
  Level 1 (primary): Faculty channels
  Level 2 (sub):     Course channels — title starts with the course code
  Level 3 (sub-sub): Edition channels — children of course channels

For each course channel the first space-separated word of the title is matched
against the CURSUS column in kanaal_emails.csv.  When a match is found, the
email from E_MAIL_ADRES is applied to both the course channel and all of its
edition sub-channels.

Two API calls are made per updated channel:
  1. channels/edit/                      — sets managers_emails (channel-specific value)
  2. settings/defaults/publishing/edit/  — sets channel_managers_emails (the default value)
                                           and unchecks "Use default" by omitting the _null flag

A report CSV is written with one row per channel processed (course + editions).
'''
import argparse
import csv
import os
import sys


def update_channel(msc, oid, new_email, dry_run):
    '''Apply the email update to a single channel. Returns error string or None.'''
    if dry_run:
        return None
    try:
        msc.api(
            'channels/edit/',
            method='post',
            data={
                'oid': oid,
                'managers_emails': new_email,
            },
        )
        msc.api(
            'settings/defaults/publishing/edit/',
            method='post',
            data={
                'channel_oid': oid,
                'channel_managers_emails': new_email,
            },
        )
    except Exception as e:
        return str(e)
    return None


def get_old_email(msc, oid):
    try:
        info = msc.api('channels/get/', params={'oid': oid, 'full': 'yes'})['info']
        return info.get('managers_emails') or ''
    except Exception as e:
        return f'(error fetching: {e})'


if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ms_client.client import MediaServerClient

    parser = argparse.ArgumentParser(
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        default='acc.json',
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
        default='update_managers_email_with_editions_report.csv',
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
    # Build lookup: parent_oid -> list of child channels
    # -------------------------------------------------------------------------
    children_of = {}
    for ch in all_channels:
        parent = ch.get('parent_oid')
        if parent:
            children_of.setdefault(parent, []).append(ch)

    # -------------------------------------------------------------------------
    # Identify level-1 (faculty) and level-2 (course) channels
    # -------------------------------------------------------------------------
    top_level_oids = {ch['oid'] for ch in all_channels if not ch.get('parent_oid')}
    course_channels = [ch for ch in all_channels if ch.get('parent_oid') in top_level_oids]

    print(f'Faculty channels (level 1): {len(top_level_oids)}')
    print(f'Course channels  (level 2): {len(course_channels)}')
    print()

    # -------------------------------------------------------------------------
    # Process course channels and their edition sub-channels
    # -------------------------------------------------------------------------
    report_rows = []
    courses_matched = 0
    courses_unmatched = 0
    editions_updated = 0

    for channel in course_channels:
        oid = channel['oid']
        title = channel.get('title', '').strip()
        words = title.split()
        course_code = words[0] if words else ''

        old_email = get_old_email(msc, oid)

        if not course_code or course_code not in cursus_email:
            report_rows.append({
                'Match': 'no',
                'Level': 'course',
                'Parent course': '',
                'oid': oid,
                'code': course_code,
                'Channel name': title,
                'old email': old_email,
                'new email': '',
            })
            courses_unmatched += 1
            continue

        # --- Matched course channel ---
        new_email = cursus_email[course_code]
        error = update_channel(msc, oid, new_email, args.dry_run)
        match_value = 'error' if error else 'yes'

        msg = f'[{"ERROR" if error else "UPDATED"}] course | {course_code} | "{title}" | {old_email} -> {new_email}'
        if error:
            msg += f' | {error}'
        print(msg)

        report_rows.append({
            'Match': match_value,
            'Level': 'course',
            'Parent course': '',
            'oid': oid,
            'code': course_code,
            'Channel name': title,
            'old email': old_email,
            'new email': new_email,
        })
        courses_matched += 1

        # --- Edition sub-channels (level 3) ---
        for edition in children_of.get(oid, []):
            ed_oid = edition['oid']
            ed_title = edition.get('title', '').strip()
            ed_old_email = get_old_email(msc, ed_oid)

            ed_error = update_channel(msc, ed_oid, new_email, args.dry_run)
            ed_match = 'error' if ed_error else 'yes'

            msg = f'  [{"ERROR" if ed_error else "UPDATED"}] edition | "{ed_title}" | {ed_old_email} -> {new_email}'
            if ed_error:
                msg += f' | {ed_error}'
            print(msg)

            report_rows.append({
                'Match': ed_match,
                'Level': 'edition',
                'Parent course': title,
                'oid': ed_oid,
                'code': course_code,
                'Channel name': ed_title,
                'old email': ed_old_email,
                'new email': new_email,
            })
            editions_updated += 1

    # -------------------------------------------------------------------------
    # Write report CSV
    # -------------------------------------------------------------------------
    fieldnames = ['Match', 'Level', 'Parent course', 'oid', 'code', 'Channel name', 'old email', 'new email']
    with open(args.report, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    print()
    print(f'Course channels  — matched: {courses_matched}, unmatched: {courses_unmatched}')
    print(f'Edition channels — updated: {editions_updated}')
    print(f'Report written to: {args.report}')
    if args.dry_run:
        print('(dry run — no changes were made)')
