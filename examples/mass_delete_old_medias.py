#!/usr/bin/env python3
"""
Script which aims at freeing storage by deleting medias older than a
given number of days. Depending on the planned deletion date and the
execution date, the script will either delete the medias or email
speakers about the impending deletion, to give them time to protect
their medias by applying a category to them.
"""

import argparse
import csv
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import html
from itertools import zip_longest
import logging
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
from typing import Optional
from urllib.parse import urlparse

try:
    from nudgisclient.client import NudgisClient
except ModuleNotFoundError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nudgisclient.client import NudgisClient
from nudgisclient.lib.utils import format_bytes, format_timedelta


logger = logging.getLogger(__name__)
DEFAULT_PLAIN_EMAIL_TEMPLATE = (
    'The following {media_count} medias (total {media_size_pp}) hosted '
    'on the video platform {platform_hostname} should '
    'be deleted according to the University policy. You have until '
    '{delete_date} to review each media below and set the '
    '"{skip_categories}" category if you need to preserve this content. '
    'After this date, the media will be deleted. You can also delete it '
    'when you review it.\n\n'
    'List of media to review ({media_count}):\n'
    '{list_of_media}\n'
)
DEFAULT_HTML_EMAIL_TEMPLATE = (
    '<p>The following {media_count} medias (total {media_size_pp}) '
    'hosted on the video platform {platform_hostname} should be deleted '
    'according to the University policy. You have '
    'until {delete_date} to review each media below and set the '
    '"{skip_categories}" category if you need to preserve this content. '
    'After this date, the media will be deleted. You can also delete '
    'it when you review it.</p>\n'
    '<p>List of media to review ({media_count}):</p>\n'
    '<ul>{list_of_media}</ul>\n'
)
DUMMY_MEDIAS = [
    {
        'oid': 'oid_1',
        'title': 'Media #1',
        'add_date': '2024-01-25 12:00:00',
        'storage_used': 10 * 1000 ** 3,  # 10 GB
        'views_last_year': 10,
        'views_last_month': 1,
    },
    {
        'oid': 'oid_2',
        'title': 'Media #2',
        'add_date': '2023-04-25 12:00:00',
        'storage_used': 20 * 1000 ** 3,  # 20 GB
        'views_last_year': 20,
        'views_last_month': 2,
    },
    {
        'oid': 'oid_3',
        'title': 'Media #3',
        'add_date': '2020-04-25 12:00:00',
        'storage_used': 30 * 1000 ** 3,  # 30 GB
        'views_last_year': 30,
        'views_last_month': 3,
    },
]


class MisconfiguredError(Exception):
    pass


def _build_channel_to_faculty_map(channels: list[dict]) -> dict[str, str]:
    '''Map every channel oid to its top-level (faculty) oid by walking parent_oid.'''
    by_oid = {ch['oid']: ch for ch in channels}
    cache: dict[str, str] = {}

    def find_root(oid: str) -> str:
        if oid in cache:
            return cache[oid]
        ch = by_oid.get(oid)
        if ch is None:
            cache[oid] = oid
            return oid
        parent = ch.get('parent_oid')
        if not parent or parent not in by_oid:
            cache[oid] = oid
            return oid
        root = find_root(parent)
        cache[oid] = root
        return root

    for oid in by_oid:
        find_root(oid)
    return cache


def _get_medias(
    ngc: NudgisClient,
    added_after: Optional[date] = None,
    added_before: Optional[date] = None,
    views_max_count: Optional[int] = None,
    views_playback_threshold: Optional[int] = None,
    views_after: Optional[date] = None,
    views_before: Optional[date] = None,
    skip_categories: list[str] = (),
    faculty_oids: Optional[set] = None,
) -> list[dict]:
    # Media categories are matched case-insensitively (see the lowercased set below),
    # so normalize the skip list the same way to match values like "Niet verwijderen".
    skip_categories = {cat.lower() for cat in skip_categories}
    catalog = ngc.get_catalog('flat')
    channel_to_faculty = _build_channel_to_faculty_map(catalog['channels']) if faculty_oids else None
    unwatched = {}
    if views_max_count is not None:
        unwatched = {
            unwatched['object_id']: unwatched['views_over_period']
            for unwatched in ngc.api(
                'stats/unwatched/',
                params={
                    'playback_threshold': views_playback_threshold,
                    'views_threshold': views_max_count,
                    'recursive': 'yes',
                    'sd': views_after.strftime('%Y-%m-%d'),
                    'ed': views_before.strftime('%Y-%m-%d'),
                },
            )['unwatched']
        }

    channels = {channel['oid']: channel for channel in catalog['channels']}
    selected_medias = []
    records = []
    for key in ('videos', 'lives'):
        medias = catalog.get(key, ())
        for media in medias:
            if faculty_oids is not None:
                root = channel_to_faculty.get(media.get('parent_oid'))
                if root not in faculty_oids:
                    continue
            add_date = datetime.strptime(media['add_date'], '%Y-%m-%d %H:%M:%S').date()
            categories = {cat.strip(' \r\t').lower() for cat in (media['categories'] or '').strip('\n').split('\n')}
            media_pp = f'{media["title"]} [{media["oid"]}]'
            status = None
            reason = ''
            if added_before and add_date >= added_before:
                before_date_pp = added_before.strftime('%Y-%m-%d')
                status = 'skip_added_after'
                reason = f'added after {before_date_pp}'
                logger.debug(f'{media_pp} was skipped because it was added after {before_date_pp}.')
            elif added_after and add_date < added_after:
                after_date_pp = added_after.strftime('%Y-%m-%d')
                status = 'skip_added_before'
                reason = f'added before {after_date_pp}'
                logger.debug(f'{media_pp} was skipped because it was added before {after_date_pp}.')
            elif views_max_count is not None and media['oid'] not in unwatched:
                views_after_pp = views_after.strftime('%Y-%m-%d')
                views_before_pp = views_before.strftime('%Y-%m-%d')
                status = 'skip_views'
                reason = (
                    f'viewed more than {views_max_count} times '
                    f'between {views_after_pp} and {views_before_pp}'
                )
                logger.debug(
                    f'{media_pp} was skipped because it was viewed more than {views_max_count} '
                    f'times between {views_after_pp} and {views_before_pp}.'
                )
            elif skip_categories and (common_categories := categories.intersection(skip_categories)):
                status = 'skip_categories'
                reason = f'has categories {sorted(common_categories)}'
                logger.debug(f'{media_pp} was skipped because it has the categories {common_categories}.')
            else:
                status = 'delete'
                reason = 'selected for deletion'
                if views_max_count is not None:
                    media['views_over_period'] = unwatched[media['oid']]
                    media['views_after'] = views_after.strftime('%Y-%m-%d')
                    media['views_before'] = views_before.strftime('%Y-%m-%d')
                media['managers_emails'] = channels.get(media['parent_oid'], {}).get('managers_emails_raw')
                selected_medias.append(media)
            records.append({
                'oid': media['oid'],
                'title': media['title'],
                'parent_oid': media.get('parent_oid'),
                'add_date': media['add_date'],
                'storage_used': media['storage_used'],
                'status': status,
                'reason': reason,
            })

    storage_used = sum(media['storage_used'] for media in selected_medias)
    logger.info(
        f'Found {len(selected_medias)} medias matching the given filters '
        f'(size: {format_bytes(storage_used)}).'
    )
    return selected_medias, records, catalog['channels']


def _get_users(ngc: NudgisClient, page_size=500):
    users = []
    offset = 0
    response = ngc.api('users/', params={'limit': page_size, 'offset': offset})
    while response['users']:
        users += response['users']
        offset += page_size
        response = ngc.api('users/', params={'limit': page_size, 'offset': offset})
    return users


def _prepare_mail(
    ngc: NudgisClient,
    sender: str,
    speaker_email: str,
    medias: list[dict],
    delete_date: date,
    skip_categories: list[str],
    html_template: Optional[str],
    plain_template: Optional[str],
    email_subject_template: str,
) -> tuple[str, dict, list[dict]]:
    # Ensure each media is only once in the list.
    medias = list({media['oid']: media for media in medias}.values())

    ms_perma_url = ngc.conf['SERVER_URL'] + '/permalink/'
    ms_edit_url = ngc.conf['SERVER_URL'] + '/edit/'
    context = {
        'media_count': len(medias),
        'media_size_pp': format_bytes(sum(media['storage_used'] for media in medias)),
        'delete_date': delete_date.strftime('%B %d, %Y'),
        'skip_categories': ' | '.join(f'"{cat}"' for cat in skip_categories),
        'platform_hostname': urlparse(ngc.conf['SERVER_URL']).netloc,
    }
    message = MIMEMultipart('alternative')
    message['Subject'] = email_subject_template.format(**context)
    message['From'] = sender
    message['To'] = speaker_email

    media_contexts = []
    now = datetime.now()
    for media in medias:
        media_add_date = datetime.strptime(media['add_date'], '%Y-%m-%d %H:%M:%S')
        media_context = {
            'oid': media['oid'],
            'parent_oid': media.get('parent_oid'),
            'storage_used': media.get('storage_used', 0),
            'title': media['title'],
            'add_date': media_add_date.strftime('%Y-%m-%d'),
            'age': format_timedelta(now - media_add_date),
            'view_url': f'{ms_perma_url}{media["oid"]}/',
            'edit_url': f'{ms_edit_url}{media["oid"]}/#id_categories',
        }
        if 'views_over_period' in media:
            media_context['views'] = (
                f'{media["views_over_period"]} times between '
                f'{media["views_after"]} and {media["views_before"]}'
            )
        else:
            media_context['views'] = (
                f'{media["views_last_year"]} times last year, '
                f'{media["views_last_month"]} times last month'
            )
        media_contexts.append(media_context)
    if plain_template:
        plain_media_list = '\n'.join(
            (
                '\t- {view_url} - "{title}" - added on {add_date} ({age} ago), viewed {views} '
                '(click here {edit_url} to protect against deletion)'
            ).format(**ctx)
            for ctx in media_contexts
        )
        plain = plain_template.format(list_of_media=plain_media_list, **context)
        message.attach(MIMEText(plain, 'plain'))
    if html_template:
        html_media_list = '\n'.join(
            (
                '<li><a href="{view_url}">"{title}"</a> added on {add_date} ({age} ago), viewed {views} '
                '(click <a href="{edit_url}">here</a> to protect against deletion)</li>'
            ).format(**ctx)
            for ctx in media_contexts
        )
        html_body = html_template.format(list_of_media=html_media_list, **context)
        message.attach(MIMEText(html_body, 'html'))
    context['media_oids'] = [media['oid'] for media in medias]
    return message.as_string(), context, media_contexts


def _get_templates(
    html_email_template: Path,
    plain_email_template: Path,
):
    try:
        html_template = html_email_template.read_text(encoding='utf-8')
        html_is_custom = True
    except FileNotFoundError:
        html_template = DEFAULT_HTML_EMAIL_TEMPLATE
        html_is_custom = False
    try:
        plain_template = plain_email_template.read_text(encoding='utf-8')
        plain_is_custom = True
    except FileNotFoundError:
        plain_template = DEFAULT_PLAIN_EMAIL_TEMPLATE
        plain_is_custom = False

    # If only one custom template is given,
    # prioritize it and don't send the default version for the other one.
    if html_is_custom and not plain_is_custom:
        plain_template = None
        logger.info(
            'HTML email template was found but plain email template was not. '
            'Using HTML version only.'
        )
    elif not html_is_custom and plain_is_custom:
        html_template = None
        logger.info(
            'Plain email template was found but HTML email template was not. '
            'Using plain version only.'
        )
    elif not html_is_custom and not plain_is_custom:
        logger.info(
            'Neither HTML nor plain email template were found. '
            'Using default email templates.'
        )
    else:
        logger.info(
            'HTML and plain email template were found. '
            'Using custom email templates.'
        )
    return html_template, plain_template


def _is_smtp_connection_failure(error: OSError) -> bool:
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return True
    # A 421 reply closes the connection, including when returned for a recipient.
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return any(code == 421 for code, _message in error.recipients.values())
    if isinstance(error, smtplib.SMTPResponseException):
        return error.smtp_code == 421
    # Other SMTP exceptions also inherit from OSError, but are not disconnects.
    return not isinstance(error, smtplib.SMTPException)


def _warn_speakers_about_deletion(
    ngc: NudgisClient,
    medias: list[dict],
    delete_date: date,
    skip_categories: list[str],
    html_email_template: Path,
    plain_email_template: Path,
    email_subject_template: str,
    fallback_to_channel_manager: bool,
    fallback_email: str,
    apply: bool = False,
):
    smtp_server = ngc.conf.get('SMTP_SERVER')
    smtp_login = ngc.conf.get('SMTP_LOGIN')
    smtp_password = ngc.conf.get('SMTP_PASSWORD')
    smtp_email = ngc.conf.get('SMTP_SENDER_EMAIL')
    if not (smtp_server and smtp_login and smtp_password and smtp_email):
        smtp_password = '*' * len(smtp_password)
        raise MisconfiguredError(f'{smtp_server=} / {smtp_login=} / {smtp_password=} / {smtp_email=}')
    html_template, plain_template = _get_templates(html_email_template, plain_email_template)

    users = _get_users(ngc)
    valid_emails = {
        email.lower(): (user.get('speaker_id') or '').strip()
        for user in users
        if (email := (user.get('email') or '').strip()) and user['is_active']
    }
    emails_by_speaker_id = {v: k for k, v in valid_emails.items()}

    medias_per_speaker = {}
    to_fallback = []
    for media in medias:
        recipients = []
        speakers_ids = [
            speaker_id.strip()
            for speaker_id in (media.get('speaker_id') or '').split('|')
        ]
        speakers_emails = [
            speaker_email.strip().lower()
            for speaker_email in (media.get('speaker_email') or '').split('|')
        ]
        for speaker_id, speaker_email in zip_longest(speakers_ids, speakers_emails):
            if speaker_email and speaker_email in valid_emails:
                recipients.append(speaker_email)
            elif speaker_id and speaker_id in emails_by_speaker_id:
                recipients.append(emails_by_speaker_id[speaker_id])
            elif fallback_to_channel_manager and media['managers_emails']:
                for manager_email in media['managers_emails'].split('\n'):
                    manager_email = manager_email.strip(' \r\t').lower()
                    if manager_email and not manager_email.startswith('#') and manager_email in valid_emails:
                        recipients.append(manager_email)

        if not recipients:
            to_fallback.append(media)

        for speaker_email in recipients:
            medias_per_speaker.setdefault(speaker_email, []).append(media)

    to_send = {
        speaker_email: _prepare_mail(
            ngc,
            sender=smtp_email,
            speaker_email=speaker_email,
            medias=speaker_medias,
            delete_date=delete_date,
            skip_categories=skip_categories,
            html_template=html_template,
            plain_template=plain_template,
            email_subject_template=email_subject_template,
        ) for speaker_email, speaker_medias in medias_per_speaker.items()
    }

    report_data = []
    smtp = None
    connection_error = None
    ssl_context = ssl.create_default_context() if apply else None

    def close_smtp():
        nonlocal smtp
        if smtp is not None:
            try:
                smtp.close()
            except OSError as err:
                logger.warning('Error closing SMTP connection: %s', err)
            smtp = None

    def deliver_email(recipient, message):
        nonlocal smtp
        if smtp is None:
            try:
                smtp = smtplib.SMTP(smtp_server, 587)
                smtp.starttls(context=ssl_context)
                smtp.login(smtp_login, smtp_password)
            except Exception:
                close_smtp()
                raise
        smtp.sendmail(smtp_email, recipient, message)

    def send_email(recipient, prepared_mail, is_fallback=False):
        nonlocal connection_error
        message, context, media_details = prepared_mail
        status = 'dry_run'
        error = ''
        if connection_error is not None:
            status = 'skipped_smtp_disconnect'
            error = connection_error
        elif apply:
            try:
                try:
                    deliver_email(recipient, message)
                except OSError as err:
                    if not _is_smtp_connection_failure(err):
                        raise
                    logger.warning(
                        'SMTP connection failed while sending to "%s": %s; '
                        'reconnecting and retrying once.', recipient, err,
                    )
                    close_smtp()
                    deliver_email(recipient, message)
            except OSError as err:  # Includes smtplib.SMTPException.
                error = f'{type(err).__name__}: {err}'
                if _is_smtp_connection_failure(err):
                    status = 'failed_smtp_disconnect'
                    connection_error = error
                    close_smtp()
                    logger.error(
                        'SMTP connection failed again while sending to "%s": %s. '
                        'Switching the rest of this run to dry-run mode; '
                        'no further emails will be sent or media deleted.',
                        recipient, error,
                    )
                else:
                    status = 'failed_smtp'
                    logger.error('Cannot send email to "%s": %s.', recipient, error)
                    if is_fallback:
                        raise
            else:
                status = 'sent'
        logger.debug('Email to "%s": %s.', recipient, status)
        report_data.append({
            'recipient': recipient,
            'status': status,
            'error': error,
            'context': context,
            'media_details': media_details,
        })
        return status

    try:
        for recipient, prepared_mail in to_send.items():
            if send_email(recipient, prepared_mail) == 'failed_smtp':
                logger.info('Adding medias for "%s" to the fallback email.', recipient)
                to_fallback += medias_per_speaker[recipient]
        if to_fallback:
            fallback_mail = _prepare_mail(
                ngc,
                sender=smtp_email,
                speaker_email=fallback_email,
                medias=to_fallback,
                delete_date=delete_date,
                skip_categories=skip_categories,
                html_template=html_template,
                plain_template=plain_template,
                email_subject_template=email_subject_template,
            )
            send_email(fallback_email, fallback_mail, is_fallback=True)
    finally:
        close_smtp()
    counts = {}
    for email in report_data:
        counts[email['status']] = counts.get(email['status'], 0) + 1
    logger.info('Email results: %s.', ', '.join(f'{status}={count}' for status, count in counts.items()) or 'none')
    return report_data


def _delete_medias(ngc: NudgisClient, medias: list[dict], apply: bool = False):
    ms_url = ngc.conf['SERVER_URL'] + '/permalink/'
    medias = {media['oid']: media for media in medias}
    if not medias:
        logger.info('No media to delete.')
        return
    deleted_count = 0
    deleted_size = 0
    if apply:
        response = ngc.api(
            'catalog/bulk_delete/',
            method='post',
            data=dict(oids=list(medias.keys()))
        )
        for oid, result in response['statuses'].items():
            if result['status'] == 200:
                logger.debug(f'Media {ms_url}{oid} has been deleted.')
                deleted_count += 1
                deleted_size += medias[oid]['storage_used']
            else:
                err = result['message']
                logger.error(
                    f'An error occurred while attempting to delete media {ms_url}{oid}. '
                    f'The media has not been deleted: {err}.'
                )
        logger.info(
            f'{deleted_count} medias ({format_bytes(deleted_size)}) have been successfully deleted.'
        )
    else:
        for oid, media in medias.items():
            logger.debug(f'[Dry run] Media {ms_url}{oid} would have been deleted.')
            deleted_count += 1
            deleted_size += media['storage_used']
        logger.info(
            f'[Dry run] {deleted_count} medias ({format_bytes(deleted_size)}) '
            f'would have been have been deleted.'
        )


STATUS_COLORS = {
    'delete': '#dc2626',
    'skip_added_after': '#2563eb',
    'skip_added_before': '#7c3aed',
    'skip_views': '#ea580c',
    'skip_categories': '#16a34a',
}
STATUS_LABELS = {
    'delete': 'To delete',
    'skip_added_after': 'Skipped — too recent',
    'skip_added_before': 'Skipped — too old',
    'skip_views': 'Skipped — viewed enough',
    'skip_categories': 'Skipped — protected category',
}
EMAIL_STATUS_LABELS = {
    'sent': 'Sent',
    'failed_smtp_disconnect': 'Failed — SMTP connection failure',
    'skipped_smtp_disconnect': 'Not sent — dry run after SMTP connection failure',
    'failed_smtp': 'Failed — SMTP error',
    'dry_run': 'Not sent — dry run',
}
EMAIL_STATUS_COLORS = {
    'sent': '#16a34a',
    'failed_smtp_disconnect': '#dc2626',
    'skipped_smtp_disconnect': '#ea580c',
    'failed_smtp': '#dc2626',
    'dry_run': '#2563eb',
}


_REPORT_STYLE = '''
body { font-family: system-ui, -apple-system, sans-serif; font-size: 14px;
       background: #f5f6f8; color: #1a1a2e; padding: 24px; }
h1 { font-size: 1.4rem; margin-bottom: 6px; }
.meta { font-size: 0.85rem; color: #666; margin-bottom: 16px; }
.legend { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.legend span { padding: 3px 10px; border-radius: 10px; color: #fff;
               font-size: 0.78rem; font-weight: 600; }
details { margin: 2px 0; }
details > summary { cursor: pointer; padding: 4px 6px; border-radius: 6px;
                    user-select: none; list-style: none; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: '▸ '; color: #888; }
details[open] > summary::before { content: '▾ '; }
details > summary:hover { background: #e8eaf0; }
ul.medias { list-style: none; padding-left: 22px; margin: 4px 0;
            border-left: 2px solid #dde1ea; }
ul.medias li { margin: 2px 0; padding: 2px 6px; border-radius: 4px; }
ul.medias li a { text-decoration: none; color: inherit; }
ul.medias li a:hover { text-decoration: underline; }
.oid { color: #888; font-family: monospace; font-size: 0.8rem; }
.reason { color: #666; font-size: 0.8rem; margin-left: 6px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
       margin-right: 6px; vertical-align: middle; }
details > div.children { padding-left: 22px; border-left: 2px solid #dde1ea;
                         margin-left: 10px; }
.badge { font-size: 0.72rem; padding: 1px 6px; border-radius: 10px;
         font-weight: 600; white-space: nowrap; margin-left: 4px;
         color: #fff; }
.controls { margin-bottom: 16px; display: flex; gap: 8px; }
.controls button { padding: 5px 14px; border: 1px solid #ccc;
                   border-radius: 6px; background: #fff; cursor: pointer;
                   font-size: 0.85rem; }
.controls button:hover { background: #e8eaf0; }
'''


_REPORT_SCRIPT = '''
function expandAll() {
  document.querySelectorAll('details').forEach(d => d.open = true);
}
function collapseAll() {
  document.querySelectorAll('details').forEach(d => d.open = false);
}
'''


def _generate_media_report(
    channels: list[dict],
    records: list[dict],
    server_url: str,
    output_path: Path,
    apply: bool,
):
    '''Generate an HTML tree report of every processed media, colour-coded by status.'''
    by_oid = {ch['oid']: ch for ch in channels}
    children_by_parent: dict[Optional[str], list[dict]] = {}
    for ch in channels:
        parent = ch.get('parent_oid') if ch.get('parent_oid') in by_oid else None
        children_by_parent.setdefault(parent, []).append(ch)
    medias_by_parent: dict[Optional[str], list[dict]] = {}
    for record in records:
        medias_by_parent.setdefault(record.get('parent_oid'), []).append(record)

    permalink_url = server_url.rstrip('/') + '/permalink/'

    def aggregate(oid):
        counts: dict[str, int] = {}
        for media in medias_by_parent.get(oid, []):
            counts[media['status']] = counts.get(media['status'], 0) + 1
        for child in children_by_parent.get(oid, []):
            for status, count in aggregate(child['oid']).items():
                counts[status] = counts.get(status, 0) + count
        return counts

    def render_channel(ch):
        counts = aggregate(ch['oid'])
        if not counts:
            return ''
        total = sum(counts.values())
        badges = ''.join(
            f'<span class="badge" style="background:{STATUS_COLORS[s]}" title="{STATUS_LABELS[s]}">{counts[s]}</span>'
            for s in STATUS_COLORS if s in counts
        )
        summary = (
            f'<summary><strong>{html.escape(ch.get("title", "(untitled)"))}</strong> '
            f'<span class="meta">({total} medias)</span>{badges}</summary>'
        )
        parts = [f'<details>{summary}<div class="children">']
        for media in sorted(medias_by_parent.get(ch['oid'], []), key=lambda m: m['title'].lower()):
            color = STATUS_COLORS.get(media['status'], '#000')
            url = f'{permalink_url}{media["oid"]}/iframe/'
            parts.append(
                f'<ul class="medias"><li>'
                f'<span class="dot" style="background:{color}"></span>'
                f'<a href="{html.escape(url)}" target="_blank">{html.escape(media["title"])}</a> '
                f'<span class="oid">[{media["oid"]}]</span>'
                f'<span class="reason">— {html.escape(media["reason"])}</span>'
                f'</li></ul>'
            )
        for sub in sorted(children_by_parent.get(ch['oid'], []), key=lambda c: c.get('title', '').lower()):
            parts.append(render_channel(sub))
        parts.append('</div></details>')
        return ''.join(parts)

    roots = sorted(children_by_parent.get(None, []), key=lambda c: c.get('title', '').lower())
    tree_html = ''.join(render_channel(r) for r in roots)

    orphans = medias_by_parent.get(None, []) + [
        m for m in records
        if m.get('parent_oid') and m['parent_oid'] not in by_oid
    ]
    if orphans:
        items = []
        for media in sorted(orphans, key=lambda m: m['title'].lower()):
            color = STATUS_COLORS.get(media['status'], '#000')
            url = f'{permalink_url}{media["oid"]}/iframe/'
            items.append(
                f'<li><span class="dot" style="background:{color}"></span>'
                f'<a href="{html.escape(url)}" target="_blank">{html.escape(media["title"])}</a> '
                f'<span class="oid">[{media["oid"]}]</span>'
                f'<span class="reason">— {html.escape(media["reason"])}</span></li>'
            )
        tree_html += (
            f'<details><summary><strong>(no parent channel)</strong> '
            f'<span class="meta">({len(orphans)} medias)</span></summary>'
            f'<ul class="medias">{"".join(items)}</ul></details>'
        )

    overall = {}
    for record in records:
        overall[record['status']] = overall.get(record['status'], 0) + 1
    legend = ''.join(
        f'<span style="background:{STATUS_COLORS[s]}">{STATUS_LABELS[s]} '
        f'({overall.get(s, 0)})</span>'
        for s in STATUS_COLORS
    )
    prefix = '' if apply else '[Dry run] '
    generated = datetime.now().strftime('%Y-%m-%d %H:%M')

    doc = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{prefix}Media classification report</title>'
        f'<style>{_REPORT_STYLE}</style></head><body>'
        f'<h1>{prefix}Media classification report</h1>'
        f'<p class="meta">Server: <strong>{html.escape(server_url)}</strong> '
        f'&nbsp;|&nbsp; Generated: <strong>{generated}</strong> '
        f'&nbsp;|&nbsp; Medias scanned: <strong>{len(records)}</strong></p>'
        f'<div class="legend">{legend}</div>'
        f'<div class="controls">'
        f'<button onclick="expandAll()">Expand all</button>'
        f'<button onclick="collapseAll()">Collapse all</button>'
        f'</div>'
        f'{tree_html}'
        f'<script>{_REPORT_SCRIPT}</script>'
        f'</body></html>'
    )
    output_path.write_text(doc, encoding='utf-8')
    logger.info(f'Wrote media report to {output_path}.')


def _generate_email_report(
    report_data: list[dict],
    server_url: str,
    output_path: Path,
    apply: bool,
):
    '''Report every planned email, including failures and emails left unsent.'''
    prefix = '' if apply else '[Dry run] '
    generated = datetime.now().strftime('%Y-%m-%d %H:%M')
    items = []
    total_medias = 0
    counts = {}
    for email_number, email in sorted(enumerate(report_data, 1), key=lambda item: item[1]['recipient']):
        recipient = email['recipient']
        context = email['context']
        media_details = email['media_details']
        status = email['status']
        counts[status] = counts.get(status, 0) + 1
        total_medias += context['media_count']
        error_html = f'<p class="reason">{html.escape(email["error"])}</p>' if email['error'] else ''
        media_items = ''.join(
            f'<li><a href="{html.escape(m["view_url"])}" target="_blank">{html.escape(m["title"])}</a> '
            f'<span class="reason">added {m["add_date"]} ({m["age"]} ago), viewed {html.escape(m["views"])}</span></li>'
            for m in media_details
        )
        items.append(
            f'<details><summary><strong>{html.escape(recipient)}</strong> '
            f'<span class="badge" style="background:{EMAIL_STATUS_COLORS[status]}">'
            f'{EMAIL_STATUS_LABELS[status]}</span> '
            f'<span class="meta">— email #{email_number}, '
            f'{context["media_count"]} medias, {context["media_size_pp"]}</span></summary>'
            f'<div class="children">'
            f'{error_html}'
            f'<p class="meta">Delete date: <strong>{html.escape(context["delete_date"])}</strong> '
            f'&nbsp;|&nbsp; Skip categories: {html.escape(context["skip_categories"])} '
            f'&nbsp;|&nbsp; Platform: {html.escape(context["platform_hostname"])}</p>'
            f'<ul class="medias">{media_items}</ul>'
            f'</div></details>'
        )

    legend = ''.join(
        f'<span style="background:{EMAIL_STATUS_COLORS[status]}">'
        f'{EMAIL_STATUS_LABELS[status]} ({count})</span>'
        for status, count in counts.items()
    )
    failure_note = ''
    if counts.get('failed_smtp_disconnect'):
        failure_note = (
            '<p>SMTP connection failure persisted after one retry. The remainder of this run '
            'continued in dry-run mode. Delivery of the interrupted email could not be confirmed; '
            'emails marked "Not sent" were not attempted.</p>'
        )
    recipient_count = len({email['recipient'] for email in report_data})
    doc = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{prefix}Email notifications report</title>'
        f'<style>{_REPORT_STYLE}</style></head><body>'
        f'<h1>{prefix}Email notifications report</h1>'
        f'<p class="meta">Server: <strong>{html.escape(server_url)}</strong> '
        f'&nbsp;|&nbsp; Generated: <strong>{generated}</strong> '
        f'&nbsp;|&nbsp; Recipients: <strong>{recipient_count}</strong> '
        f'&nbsp;|&nbsp; Emails: <strong>{len(report_data)}</strong> '
        f'&nbsp;|&nbsp; Media references: <strong>{total_medias}</strong></p>'
        f'<div class="legend">{legend}</div>{failure_note}'
        f'<div class="controls">'
        f'<button onclick="expandAll()">Expand all</button>'
        f'<button onclick="collapseAll()">Collapse all</button>'
        f'</div>'
        f'{"".join(items)}'
        f'<script>{_REPORT_SCRIPT}</script>'
        f'</body></html>'
    )
    output_path.write_text(doc, encoding='utf-8')
    logger.info(f'Wrote email report to {output_path}.')


def _generate_email_csv(
    report_data: list[dict],
    channels: list[dict],
    output_path: Path,
):
    '''One row per email message and faculty, including its delivery status.'''
    channel_to_faculty = _build_channel_to_faculty_map(channels)
    title_by_oid = {ch['oid']: ch.get('title', '') for ch in channels}

    rows: dict[tuple[int, str], dict] = {}
    for email_number, email in enumerate(report_data, 1):
        for media in email['media_details']:
            faculty_oid = channel_to_faculty.get(media.get('parent_oid'), '') or ''
            key = (email_number, faculty_oid)
            row = rows.setdefault(key, {
                'email': email['recipient'],
                'faculty_title': title_by_oid.get(faculty_oid, ''),
                'video_count': 0,
                '_total_bytes': 0,
                'delete_date': email['context']['delete_date'],
                'email_number': email_number,
                'status': email['status'],
                'error': email['error'],
            })
            row['video_count'] += 1
            row['_total_bytes'] += media.get('storage_used', 0)

    fieldnames = ['email', 'faculty_title', 'video_count', 'total_size', 'delete_date',
                  'email_number', 'status', 'error']
    sorted_rows = sorted(rows.values(), key=lambda r: (r['email'], r['email_number'], r['faculty_title']))
    with output_path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({
                'email': row['email'],
                'faculty_title': row['faculty_title'],
                'video_count': row['video_count'],
                'total_size': format_bytes(row['_total_bytes']),
                'delete_date': row['delete_date'],
                'email_number': row['email_number'],
                'status': row['status'],
                'error': row['error'],
            })
    logger.info(f'Wrote email CSV ({len(sorted_rows)} rows) to {output_path}.')


def _generate_media_csv(
    channels: list[dict],
    records: list[dict],
    server_url: str,
    output_path: Path,
):
    '''Write deletion candidates grouped by faculty, course, and edition.'''
    channels_by_oid = {channel['oid']: channel for channel in channels}

    def channel_path(oid: Optional[str]) -> list[dict]:
        path = []
        visited = set()
        while oid in channels_by_oid and oid not in visited:
            visited.add(oid)
            channel = channels_by_oid[oid]
            path.append(channel)
            oid = channel.get('parent_oid')
        return list(reversed(path))

    permalink_url = server_url.rstrip('/') + '/permalink/'
    rows = {}
    for record in records:
        if record['status'] != 'delete':
            continue

        path = channel_path(record.get('parent_oid'))
        faculty = path[0] if len(path) > 0 else None
        course = path[1] if len(path) > 1 else None
        edition = path[2] if len(path) > 2 else None
        key = tuple(channel['oid'] if channel else '' for channel in (faculty, course, edition))
        row = rows.setdefault(key, {
            'Faculty': faculty.get('title', '') if faculty else '',
            'Course Name': course.get('title', '') if course else '',
            'Link to course': f'{permalink_url}{course["oid"]}/' if course else '',
            'Edition Name': edition.get('title', '') if edition else '',
            'Link to Course Edition': f'{permalink_url}{edition["oid"]}/' if edition else '',
            'Medias Deleted': [],
        })
        row['Medias Deleted'].append(record.get('title') or record['oid'])

    fieldnames = [
        'Faculty',
        'Course Name',
        'Link to course',
        'Edition Name',
        'Link to Course Edition',
        'Medias Deleted',
    ]
    sorted_rows = sorted(
        rows.values(),
        key=lambda row: (
            row['Faculty'].casefold(),
            row['Course Name'].casefold(),
            row['Edition Name'].casefold(),
        ),
    )
    with output_path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({
                **row,
                'Medias Deleted': ' | '.join(sorted(row['Medias Deleted'], key=str.casefold)),
            })
    logger.info(f'Wrote media CSV ({len(sorted_rows)} rows) to {output_path}.')


def delete_old_medias(sys_args):
    parser = argparse.ArgumentParser(
        'mass_delete_old_medias',
        description=(
            'This script deletes (or warns speakers about the impending deletion of) medias '
            'matching certain filters. To run this script properly, start by choosing a '
            '"--delete-date" set in the future (you should give your users enough time to react '
            'to the deletion notifications). Then, choose at least one filter among:'
            '\n\t- "--added-after" date, to select only media that were added after the given '
            'date.'
            '\n\t- "--added-before" date, to select only media that were added before the given '
            'date.'
            '\n\t- "–-views-max-count", to select only medias that have had less than the given '
            'number of views over a given period ("--views-after" / "--views-before"). When '
            'this parameter is given, an additional parameter ("--views-playback-threshold") can '
            'be used to count only views where the playback time is above a number of seconds.'
            '\nWhen applying multiple filters, only medias that match all the filters are '
            'considered for deletion. If no filters are given, to prevent mistakes, the command '
            'will error out without doing anything.'
            '\n\nFill out the other parameters to your liking then run the script. On the first '
            'run, the script will warn every speaker of the impending deletion of their medias. '
            'You can run the script as many times as you want until the "--delete-date" to send '
            'reminders to speakers. Be sure to always use the same filters as you did during the '
            'first run, to ensure, the medias considered for deletion are always the same. '
            'Finally, after the "--delete-date" has passed, run the script one more time, still '
            'with the same filters as the first run. This last run will delete the medias to the '
            'recycle-bin (assuming the recycle-bin is activated, otherwise the deletion cannot be '
            'undone). You can repeat this whole process at regular intervals and with different '
            'parameters (every 6 months, every year...) to cleanup old medias.'
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        help='Path to the configuration file (e.g. myconfig.json).',
        required=True,
        type=str,
    )
    parser.add_argument(
        '--delete-date',
        help='Date after which content will be deleted when running the script, e.g. "2024-10-28"; '
             'running this script before this date will result in notifying users, otherwise it '
             'will delete the medias.',
        type=str,
        required=True,
    )
    parser.add_argument(
        '--added-after',
        help='Minimum "add_date" for a media to be considered for deletion (e.g. "2022-01-28"). '
             'Any media added before this date will not be considered for deletion.',
        type=str,
        required=False,
    )
    parser.add_argument(
        '--added-before',
        help='Maximum "add_date" for a media to be considered for deletion (e.g. "2022-01-28"). '
             'Any media added on or after this date will not be considered for deletion.',
        type=str,
        required=False,
    )
    parser.add_argument(
        '--views-max-count',
        help='Maximum number of views a media can have over a given period ("--views-after" '
             '/ "--views-before") to be considered for deletion (e.g.: --views-max-count=3 to '
             'select only media that have 3 views or less during the given period). Any media '
             'that has been viewed more times than this number over the given period will not be '
             'considered for deletion. If this parameter is given, then both "--views-after" '
             'and "--views-before" must be given too (and they must be in the past). The script '
             'will error out before doing anything if that is not the case. This ensures the '
             'selection remains the same between runs.',
        type=int,
        required=False,
    )
    parser.add_argument(
        '--views-playback-threshold',
        help='Minimum time played (in seconds) to count as a view. Defaults to 0 (meaning any view '
             'with a duration above 0 is counted). If "–-views-max-count" is not given, this '
             'parameter will be ignored. Example: --views-max-count=3 --views-playback-threshold=5 '
             'will select only media that have been viewed 3 times or less for more than 5 seconds.',
        type=int,
        required=False,
        default=0,
    )
    parser.add_argument(
        '--views-after',
        help='Start of the period to consider for the "–-views-max-count" parameter. If '
             '"–-views-max-count" is not given, this parameter will be ignored. Required '
             'if "--views-max-count" is given. Examples: "2020-09-01".',
        type=str,
        required=False,
    )
    parser.add_argument(
        '--views-before',
        help='End of the period to consider for the "–-views-max-count" parameter. If '
             '"–-views-max-count" is not given, this parameter will be ignored. Required '
             'if "--views-max-count" is given. Examples: "2021-08-31".',
        type=str,
        required=False,
    )
    parser.add_argument(
        '--skip-category',
        help='Category name used to signify that content must be preserved. Can be '
             'passed multiple times to skip multiple categories '
             '(e.g.: --skip-category="do not delete" --skip-category="to keep"). '
             'Default is --skip-category="do not delete"',
        dest='skip_categories',
        action='append',
        default=[],
    )
    parser.add_argument(
        '--html-email-template',
        help='Path to HTML email template file. The template will be populated at runtime with '
             'dynamic values via these 5 variables: platform_hostname, media_count, delete_date, '
             'skip_categories, list_of_media. Your template should use Python new-style formatting '
             'syntax (e.g.: "You have until {delete_date} to review each media").',
        type=Path,
        default='./email.html',
    )
    parser.add_argument(
        '--plain-email-template',
        help='Path to plain email template file. This plain variant will be displayed to users '
             'whose email client is set to prevent HTML in emails. The template variables '
             'available are the same as for the "--html-email-template" argument.',
        type=Path,
        default='./email.txt',
    )
    parser.add_argument(
        '--email-subject-template',
        help='Template string to use for the email subject line. The template variables available '
             'are the same as for the "--html-email-template" argument.',
        type=str,
        default='Action required on the video platform {platform_hostname}: '
                '{media_count} medias will be deleted on {delete_date}',
    )
    parser.add_argument(
        '--send-email-on-deletion',
        help='Notify users on deletion of the list of deleted media and the freed storage summary. '
             'Use "--html-email-template", "--plain-email-template" and "--email-subject-template" '
             'to customize the emails sent on deletion.',
        action='store_true',
        required=False,
        default=False,
    )
    parser.add_argument(
        '--fallback-to-channel-manager',
        help='If medias do not have speakers or if none of the speakers point to an existing '
             'user, send an email to the channel manager if one exists (it must point to an '
             'existing user in the database too).',
        action='store_true',
        required=False,
        default=False,
    )
    parser.add_argument(
        '--fallback-email',
        help='Fallback recipient address. This address will receive a notification for all medias '
             'that do not have speakers/channel-managers or for which none of the speakers/'
             'channel-managers point to an existing user with a valid email address. Medias for '
             'which a notification was sent but the delivery failed will also be added to the '
             'notification sent to this address. If mail delivery to this fallback address fails, '
             'the script will fail with an error. This ensures that at least one address is '
             'notified of the impending deletion of any media.',
        type=str,
        required=True,
    )
    parser.add_argument(
        '--apply',
        help='Whether to apply changes or not. If not set, the script will simulate the work and '
             'generate logs. It is a good idea to set "--log-level" to "debug" if "--apply" is '
             'not set.',
        action='store_true',
    )
    parser.add_argument(
        '--test-email-template',
        help='Use this flag to test your email templates. A single email will be printed to the '
             'console with dummy values. No email will be sent and no media will be deleted.',
        action='store_true',
    )
    parser.add_argument(
        '--media-report',
        help='Path of the HTML media-classification report to write. '
             'The report is a collapsible tree of channels with each video '
             'colour-coded by status (to-delete / various skip reasons). '
             'Defaults to "./media_report_<hostname>_<timestamp>.html". '
             'Pass an empty string to disable.',
        type=str,
        default=None,
    )
    parser.add_argument(
        '--media-csv',
        help='Path of the CSV media-deletion report to write. The report has '
             'one row per course edition and stores all media selected for '
             'deletion in a single, pipe-separated cell. Defaults to '
             '"./media_report_<hostname>_<timestamp>.csv". '
             'Pass an empty string to disable.',
        type=str,
        default=None,
    )
    parser.add_argument(
        '--email-report',
        help='Path of the HTML email-notifications report to write. '
             'The report is a collapsible tree of recipients with their '
             'delivery status, SMTP errors, and a clickable list of videos. '
             'After two connection failures for an email, the rest of the run '
             'continues in dry-run mode and the report includes unsent emails. '
             'Defaults to "./email_report_<hostname>_<timestamp>.html". '
             'Pass an empty string to disable. Only produced when emails are '
             'sent or simulated.',
        type=str,
        default=None,
    )
    parser.add_argument(
        '--email-csv',
        help='Path of the CSV email summary to write. One row per email message '
             'and top-level faculty channel, with an aggregate video count, '
             'email number, delivery status, and SMTP error. Defaults to '
             '"./email_report_<hostname>_<timestamp>.csv". '
             'Pass an empty string to disable. Only produced when emails are '
             'sent or simulated.',
        type=str,
        default=None,
    )
    parser.add_argument(
        '--log-level',
        help='Log level.',
        default='info',
        choices=['critical', 'error', 'warn', 'info', 'debug']
    )
    args = parser.parse_args(sys_args)

    logging.basicConfig()
    logger.setLevel(args.log_level.upper())

    ngc = NudgisClient(args.conf)
    ngc.conf['TIMEOUT'] = max(600, ngc.conf['TIMEOUT'])

    hostname_slug = urlparse(ngc.conf['SERVER_URL']).netloc.replace('.', '_')
    timestamp_slug = datetime.now().strftime('%Y%m%dT%H%M%S')
    if args.media_report is None:
        args.media_report = f'./media_report_{hostname_slug}_{timestamp_slug}.html'
    if args.media_csv is None:
        args.media_csv = f'./media_report_{hostname_slug}_{timestamp_slug}.csv'
    if args.email_report is None:
        args.email_report = f'./email_report_{hostname_slug}_{timestamp_slug}.html'
    if args.email_csv is None:
        args.email_csv = f'./email_report_{hostname_slug}_{timestamp_slug}.csv'

    if args.apply:
        answer = input(
            'The script is running in normal mode. '
            'Emails will be sent, medias will be deleted.\n'
            'Please ensure that the recycle-bin is enabled on your platform '
            f'{ngc.conf["SERVER_URL"]}/admin/settings/#id_trash_enabled '
            'Proceed ? [y / n]'
        )
        if answer.lower() not in ['yes', 'y']:
            sys.exit(0)
    else:
        logger.info(
            '[Dry run] The script is running in dry-run mode. '
            'No email will be sent, no media will be deleted.'
        )
    today = date.today()
    delete_date = datetime.strptime(args.delete_date, '%Y-%m-%d').date()
    added_after = None
    added_before = None
    views_max_count = args.views_max_count
    views_playback_threshold = args.views_playback_threshold
    views_after = None
    views_before = None
    if args.added_after:
        added_after = datetime.strptime(args.added_after, '%Y-%m-%d').date()
    if args.added_before:
        added_before = datetime.strptime(args.added_before, '%Y-%m-%d').date()
    if args.views_after:
        views_after = datetime.strptime(args.views_after, '%Y-%m-%d').date()
    if args.views_before:
        views_before = datetime.strptime(args.views_before, '%Y-%m-%d').date()

    if views_max_count is None and added_after is None and added_before is None:
        raise MisconfiguredError(
            'At least one filter ("--added-after", "--added-before", '
            '"--views-max-count") is required.'
        )
    safe_end_date = today - timedelta(days=1)
    if views_max_count is None:
        views_playback_threshold = None
        views_after = None
        views_before = None
    elif views_max_count < 0:
        raise MisconfiguredError('If given, "--views-max-count" must be >= 0.')
    elif (
        not views_after or views_after > safe_end_date
        or not views_before or views_before > safe_end_date
    ):
        raise MisconfiguredError(
            'Both "--views-after" or "--views-before" must be given with "--views-max-count" '
            'to prevent deleting newer videos for which stats may not have been computed yet.'
            f'It must be at most {safe_end_date.strftime("%Y-%m-%d")}'
        )

    skip_categories = args.skip_categories or ['do not delete']

    if args.test_email_template:
        html_template, plain_template = _get_templates(
            args.html_email_template,
            args.plain_email_template,
        )
        message, _context, _details = _prepare_mail(
            ngc,
            sender=ngc.conf.get('SMTP_SENDER_EMAIL', 'your-smtp-account@example.com'),
            speaker_email=args.fallback_email,
            medias=DUMMY_MEDIAS,
            delete_date=delete_date,
            skip_categories=skip_categories,
            html_template=html_template,
            plain_template=plain_template,
            email_subject_template=args.email_subject_template,
        )
        logger.info(message)
    else:
        logger.info('Fetching catalog to list faculties...')
        tree = ngc.get_catalog(fmt='tree')
        faculties = sorted(tree.get('channels', []), key=lambda ch: ch.get('title', ''))

        print('\nAvailable faculties:')
        print('  0. All faculties')
        for i, ch in enumerate(faculties, 1):
            print(f'  {i}. {ch["title"]}')

        selection = input('\nSelect faculties to process (0 for all, or comma/space separated numbers): ').strip()

        if not selection or selection == '0':
            faculty_oids = None
        else:
            indices = [int(x) for x in re.split(r'[\s,]+', selection) if x.isdigit()]
            invalid = [x for x in indices if not (1 <= x <= len(faculties))]
            if invalid:
                print(f'Invalid selection(s): {invalid}')
                sys.exit(1)
            faculty_oids = {faculties[i - 1]['oid'] for i in indices}
            selected_titles = [faculties[i - 1]['title'] for i in indices]
            print(f'\nProcessing: {", ".join(selected_titles)}')

        medias, records, channels = _get_medias(
            ngc,
            added_after=added_after,
            added_before=added_before,
            views_max_count=views_max_count,
            views_playback_threshold=views_playback_threshold,
            views_after=views_after,
            views_before=views_before,
            skip_categories=skip_categories,
            faculty_oids=faculty_oids,
        )
        if args.media_report:
            _generate_media_report(
                channels,
                records,
                server_url=ngc.conf['SERVER_URL'],
                output_path=Path(args.media_report),
                apply=args.apply,
            )
        if args.media_csv:
            _generate_media_csv(
                channels,
                records,
                server_url=ngc.conf['SERVER_URL'],
                output_path=Path(args.media_csv),
            )
        report_data = None
        apply_deletion = args.apply
        if delete_date > today or args.send_email_on_deletion:
            report_data = _warn_speakers_about_deletion(
                ngc,
                medias,
                delete_date=delete_date,
                skip_categories=skip_categories,
                html_email_template=args.html_email_template,
                plain_email_template=args.plain_email_template,
                email_subject_template=args.email_subject_template,
                fallback_to_channel_manager=args.fallback_to_channel_manager,
                fallback_email=args.fallback_email,
                apply=args.apply,
            )
            if any(email['status'] == 'failed_smtp_disconnect' for email in report_data):
                apply_deletion = False
        if args.email_report and report_data is not None:
            _generate_email_report(
                report_data,
                server_url=ngc.conf['SERVER_URL'],
                output_path=Path(args.email_report),
                apply=args.apply,
            )
        if args.email_csv and report_data is not None:
            _generate_email_csv(
                report_data,
                channels=channels,
                output_path=Path(args.email_csv),
            )
        if delete_date <= today:
            _delete_medias(ngc, medias, apply=apply_deletion)


if __name__ == '__main__':
    delete_old_medias(sys.argv[1:])
