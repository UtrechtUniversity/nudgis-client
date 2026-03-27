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

Sets managers_emails on each matched channel via channels/edit/.

A report CSV is written with one row per channel processed (course + editions).
Unmatched channels are recorded with an empty old/new email (no API call made).
'''
import argparse
import csv
import os
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


# Thread-local storage so each worker thread has its own MediaServerClient
# (requests.Session is not safe to share across threads).
_thread_local = threading.local()


def _get_thread_client(conf_path):
    if not hasattr(_thread_local, 'msc'):
        _thread_local.msc = MediaServerClient(conf_path, setup_logging=False)
    return _thread_local.msc


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
    except Exception as e:
        return str(e)
    return None


def _catalog_email(channel):
    '''Return the raw managers_emails string from a catalog channel dict.
    The catalog provides two fields: managers_emails (a resolved list of matched
    user objects) and managers_emails_raw (the plain string as stored via
    channels/edit/). We compare against the raw value to detect actual changes.'''
    return channel.get('managers_emails_raw') or ''


def _process_course_channel(channel, cursus_email, children_of, conf_path, dry_run):
    '''
    Process one course channel and its edition sub-channels in a worker thread.
    Returns (report_rows, course_updated: bool, course_already_correct: bool,
             editions_updated: int, editions_already_correct: int).
    Old emails are read directly from the catalog data already in memory.
    '''
    oid = channel['oid']
    title = channel.get('title', '').strip()
    words = title.split()
    course_code = words[0] if words else ''

    rows = []

    if not course_code or course_code not in cursus_email:
        rows.append({
            'Match': 'no',
            'Level': 'course',
            'Parent course': '',
            'oid': oid,
            'code': course_code,
            'Channel name': title,
            'old email': _catalog_email(channel),
            'new email': '',
        })
        return rows, False, False, 0, 0

    new_email = cursus_email[course_code]
    old_email = _catalog_email(channel)
    if old_email != new_email:
        if not dry_run:
            msc = _get_thread_client(conf_path)
            error = update_channel(msc, oid, new_email, dry_run)
            match_value = 'error' if error else 'yes'
        else:
            match_value = 'yes'
        course_updated = True
        course_already_correct = False
    else:
        match_value = 'already correct'
        course_updated = False
        course_already_correct = True
    rows.append({
        'Match': match_value,
        'Level': 'course',
        'Parent course': '',
        'oid': oid,
        'code': course_code,
        'Channel name': title,
        'old email': old_email,
        'new email': new_email,
    })

    editions_updated = 0
    editions_already_correct = 0
    for edition in children_of.get(oid, []):
        ed_oid = edition['oid']
        ed_title = edition.get('title', '').strip()
        ed_old_email = _catalog_email(edition)
        if ed_old_email != new_email:
            if not dry_run:
                msc = _get_thread_client(conf_path)
                ed_error = update_channel(msc, ed_oid, new_email, dry_run)
                ed_match = 'error' if ed_error else 'yes'
            else:
                ed_match = 'yes'
            editions_updated += 1
        else:
            ed_match = 'already correct'
            editions_already_correct += 1
        rows.append({
            'Match': ed_match,
            'Level': 'edition',
            'Parent course': title,
            'oid': ed_oid,
            'code': course_code,
            'Channel name': ed_title,
            'old email': ed_old_email,
            'new email': new_email,
        })

    return rows, course_updated, course_already_correct, editions_updated, editions_already_correct


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
    parser.add_argument(
        '--workers',
        default=5,
        type=int,
        help='Number of parallel worker threads for API calls (default: 5).',
    )
    args = parser.parse_args()

    # Append the conf file stem to the report filename, e.g.
    # update_managers_email_with_editions_report_acc.csv
    conf_stem = Path(args.conf).stem
    report_path = Path(args.report)
    args.report = str(report_path.with_stem(report_path.stem + '_' + conf_stem))

    # -------------------------------------------------------------------------
    # Load CSV: build a lookup dict  CURSUS -> E_MAIL_ADRES
    # -------------------------------------------------------------------------
    cursus_email = {}
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            with open(args.csv, newline='', encoding=encoding) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cursus = row.get('CURSUS', '').strip()
                    email = row.get('E_MAIL_ADRES', '').strip()
                    if cursus and email:
                        cursus_email[cursus] = email
            print(f'Read CSV with encoding: {encoding}')
            break
        except UnicodeDecodeError:
            cursus_email = {}
            continue
    else:
        print(f'Error: could not decode {args.csv} with any supported encoding.')
        sys.exit(1)

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
    # Process course channels and their edition sub-channels (parallel)
    # -------------------------------------------------------------------------
    # Each future processes one course channel + its edition sub-channels.
    # Unmatched channels generate no API calls at all.
    # -------------------------------------------------------------------------
    report_rows_by_idx = {}   # idx -> rows, to preserve catalog order in report
    courses_updated = 0
    courses_already_correct = 0
    courses_unmatched = 0
    editions_updated = 0
    editions_already_correct = 0
    completed = 0
    total = len(course_channels)
    lock = threading.Lock()

    print(f'Processing {total} course channels with {args.workers} workers...')

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _process_course_channel,
                channel,
                cursus_email,
                children_of,
                args.conf,
                args.dry_run,
            ): idx
            for idx, channel in enumerate(course_channels)
        }
        for future in as_completed(futures):
            idx = futures[future]
            rows, c_updated, c_correct, ed_updated, ed_correct = future.result()
            with lock:
                report_rows_by_idx[idx] = rows
                if c_updated:
                    courses_updated += 1
                elif c_correct:
                    courses_already_correct += 1
                else:
                    courses_unmatched += 1
                editions_updated += ed_updated
                editions_already_correct += ed_correct
                completed += 1
                print(f'\r[{completed}/{total}]', end='', flush=True)

    print()  # end progress line

    # Restore catalog order for the report
    report_rows = []
    for idx in range(total):
        report_rows.extend(report_rows_by_idx[idx])

    # -------------------------------------------------------------------------
    # Write report CSV
    # -------------------------------------------------------------------------
    fieldnames = ['Match', 'Level', 'Parent course', 'oid', 'code', 'Channel name', 'old email', 'new email']
    with open(args.report, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    print()
    print(f'Course channels  — updated: {courses_updated}, already correct: {courses_already_correct}, unmatched: {courses_unmatched}')
    print(f'Edition channels — updated: {editions_updated}, already correct: {editions_already_correct}')
    print(f'Report written to: {args.report}')
    if args.dry_run:
        print('(dry run — no changes were made)')
