import csv
import smtplib
from collections import namedtuple
from datetime import date, timedelta
from unittest import mock

import pytest


from examples.mass_delete_old_medias import (
    EMAIL_STATUS_LABELS,
    _generate_email_csv,
    _generate_email_report,
    _generate_media_csv,
    _get_templates,
    _warn_speakers_about_deletion,
    delete_old_medias,
    MisconfiguredError,
)


TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
IN_A_MONTH = TODAY + timedelta(days=30)
ONE_YEAR_AGO = TODAY - timedelta(days=365)
TWO_YEARS_AGO = TODAY - timedelta(days=365 * 2)
THREE_YEARS_AGO = TODAY - timedelta(days=365 * 3)
FOUR_YEARS_AGO = TODAY - timedelta(days=365 * 4)
FIVE_YEARS_AGO = TODAY - timedelta(days=365 * 5)


@pytest.fixture(autouse=True)
def no_prompt():
    with mock.patch('examples.mass_delete_old_medias.input', return_value='y') as mock_input:
        yield mock_input


@pytest.fixture()
def catalog():
    return {
        'channels': [
            {
                'oid': 'channel_1',
                'managers_emails': 'manager@example.com\n#manager_inactive@example.com\nmanager_invalid@example.com',
            },
            {
                'oid': 'channel_2',
                'managers_emails': '',
            },
        ],
        'videos': [
            {
                'oid': 'two_years_ago',
                'title': 'Two Years Ago',
                'parent_oid': 'channel_1',
                'add_date': TWO_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': '',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': 'john.doe@example.com',
            },
            {
                'oid': 'three_years_ago_no_speaker',
                'title': 'Three Years Ago: no speaker',
                'parent_oid': 'channel_1',
                'add_date': THREE_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': '',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': '',
            },
            {
                'oid': 'three_years_ago_dnd',
                'title': 'Three Years Ago: do not delete',
                'parent_oid': 'channel_1',
                'add_date': THREE_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': 'do not delete',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': '',
            },
            {
                'oid': 'three_years_ago_mail_error',
                'title': 'Three Years Ago: mail error',
                'parent_oid': 'channel_2',
                'add_date': THREE_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': '',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': 'error@example.com',
            },
            {
                'oid': 'four_years_ago',
                'title': 'Four Years Ago',
                'parent_oid': 'channel_1',
                'add_date': FOUR_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': 'some_category',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': 'john.doe@example.com',
            },
            {
                'oid': 'five_years_ago',
                'title': 'Five Years Ago',
                'parent_oid': 'channel_1',
                'add_date': FIVE_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': '',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': 'john.doe@example.com | jane.doe@example.com',
            },
        ],
        'lives': [
            {
                'oid': 'live_three_years_ago',
                'title': 'Live: Three Years Ago',
                'parent_oid': 'channel_1',
                'add_date': THREE_YEARS_AGO.strftime('%Y-%m-%d 20:00:00'),
                'categories': '',
                'storage_used': 30 * 1024 ** 3,  # 30 GB
                'views_last_year': 75,
                'views_last_month': 1,
                'speaker_email': 'john.doe@example.com |  | inactive@example.com | deleted@example.com',
                'speaker_id': 'john.doe | june.doe |  | ',
            },
        ],
    }


@pytest.fixture()
def users():
    return [
        {
            'email': 'john.doe@example.com',
            'is_active': True,
            'speaker_id': '',
        },
        {
            'email': 'jane.doe@example.com',
            'is_active': True,
            'speaker_id': '',
        },
        {
            'email': 'june.doe@example.com',
            'is_active': True,
            'speaker_id': 'june.doe',
        },
        {
            'email': 'inactive@example.com',
            'is_active': False,
            'speaker_id': '',
        },
        {
            'email': 'manager@example.com',
            'is_active': True,
            'speaker_id': '',
        },
    ]


@pytest.fixture()
def api_client(catalog, users):
    def mock_api_call(url, **kwargs):
        if url == 'catalog/bulk_delete/':
            return {
                'statuses': {oid: {'status': 200} for oid in kwargs['data']['oids']}
            }
        elif url == 'catalog/get-all/':
            return catalog
        elif url == 'stats/unwatched/':
            return {
                'success': True,
                'start_date': kwargs['params']['sd'],
                'end_date': kwargs['params']['ed'],
                'unwatched': [
                    {
                        'object_id': 'three_years_ago_no_speaker',
                        'views_over_period': 0,
                    },
                    {
                        'object_id': 'three_years_ago_dnd',
                        'views_over_period': 0,
                    },
                    {
                        'object_id': 'three_years_ago_mail_error',
                        'views_over_period': 0,
                    },
                ],
            }
        elif url == 'users/':
            if kwargs.get('params', {}).get('offset', 0) == 0:
                return {'users': users}
            else:
                return {'users': []}

    from ms_client.client import MediaServerClient

    client = MediaServerClient()
    client._server_version = (12, 3, 0)
    client.conf['SMTP_SERVER'] = 'smtp.example.com'
    client.conf['SMTP_LOGIN'] = 'sender'
    client.conf['SMTP_PASSWORD'] = 's3cr3t'
    client.conf['SMTP_SENDER_EMAIL'] = 'sender@example.com'
    client.api = mock.MagicMock(side_effect=mock_api_call)
    with mock.patch('examples.mass_delete_old_medias.MediaServerClient', return_value=client):
        yield client


Message = namedtuple('Message', ['sender', 'recipient', 'message'])


class MockSMTP:
    def __init__(self):
        self.factory = None
        self._tls_started = False
        self._logged_in = False
        self.closed = False
        self.mailbox: list[Message] = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        pass

    def starttls(self, *, context):
        assert context is not None
        self._tls_started = True

    def login(self, sender, password):
        assert self._tls_started
        assert sender == 'sender'
        assert password == 's3cr3t'
        self._logged_in = True

    def close(self):
        self.closed = True

    def sendmail(self, sender, recipient, message):
        assert self._logged_in
        if recipient == 'error@example.com':
            raise smtplib.SMTPRecipientsRefused({'error@example.com': (550, b'User unknown')})
        self.mailbox.append(Message(sender, recipient, message))

    def has_mail(self, sender_address: str, recipient: str, oids: list[str]):
        for mail in self.mailbox:
            if (
                mail.sender == sender_address
                and mail.recipient == recipient
                and all((oid in mail.message) for oid in oids)
            ):
                return True
        return False


@pytest.fixture()
def mock_smtp():
    mock_smtp = MockSMTP()
    with mock.patch('smtplib.SMTP', autospec=True, return_value=mock_smtp) as factory:
        mock_smtp.factory = factory
        yield mock_smtp


@pytest.fixture()
def warn_speakers(api_client, tmp_path):
    def warn(medias, **overrides):
        options = {
            'delete_date': IN_A_MONTH,
            'skip_categories': ['do not delete'],
            'html_email_template': tmp_path / 'missing.html',
            'plain_email_template': tmp_path / 'missing.txt',
            'email_subject_template': 'Deletion warning for {platform_hostname}',
            'fallback_to_channel_manager': False,
            'fallback_email': 'fallback@example.com',
            'apply': True,
        }
        options.update(overrides)
        return _warn_speakers_about_deletion(api_client, medias, **options)
    return warn


def test_smtp_uses_starttls_on_port_587(api_client, catalog, mock_smtp, tmp_path):
    _warn_speakers_about_deletion(
        api_client,
        medias=[catalog['videos'][0]],
        delete_date=IN_A_MONTH,
        skip_categories=['do not delete'],
        html_email_template=tmp_path / 'missing.html',
        plain_email_template=tmp_path / 'missing.txt',
        email_subject_template='Deletion warning for {platform_hostname}',
        fallback_to_channel_manager=False,
        fallback_email='fallback@example.com',
        apply=True,
    )

    mock_smtp.factory.assert_called_once_with('smtp.example.com', 587)
    assert mock_smtp._tls_started


@pytest.mark.parametrize('disconnect_error', [
    smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError,
])
@pytest.mark.parametrize('media_index, recipient', [
    (0, 'john.doe@example.com'),
    (1, 'fallback@example.com'),
])
def test_smtp_reconnects_after_disconnect(
    api_client, catalog, tmp_path, media_index, recipient, disconnect_error,
):
    class DisconnectingSMTP(MockSMTP):
        def sendmail(self, sender, email, message):
            if email == recipient:
                raise disconnect_error('Connection lost')
            super().sendmail(sender, email, message)

    first = DisconnectingSMTP()
    second = MockSMTP()
    with mock.patch('smtplib.SMTP', side_effect=[first, second]) as factory:
        report_data = _warn_speakers_about_deletion(
            api_client,
            medias=[catalog['videos'][media_index]],
            delete_date=IN_A_MONTH,
            skip_categories=['do not delete'],
            html_email_template=tmp_path / 'missing.html',
            plain_email_template=tmp_path / 'missing.txt',
            email_subject_template='Deletion warning for {platform_hostname}',
            fallback_to_channel_manager=False,
            fallback_email='fallback@example.com',
            apply=True,
        )

    assert factory.call_args_list == [
        mock.call('smtp.example.com', 587),
        mock.call('smtp.example.com', 587),
    ]
    assert first.closed and second.closed
    assert second._tls_started and second._logged_in
    assert second.has_mail('sender@example.com', recipient, [catalog['videos'][media_index]['oid']])
    assert [(email['recipient'], email['status']) for email in report_data] == [(recipient, 'sent')]


def test_smtp_recipient_rejection_uses_fallback_without_reconnecting(
    api_client, catalog, users, mock_smtp, tmp_path,
):
    users.append({'email': 'error@example.com', 'is_active': True, 'speaker_id': ''})
    media = catalog['videos'][3]
    with mock.patch.object(mock_smtp, 'sendmail', wraps=mock_smtp.sendmail) as sendmail:
        report_data = _warn_speakers_about_deletion(
            api_client,
            medias=[media],
            delete_date=IN_A_MONTH,
            skip_categories=['do not delete'],
            html_email_template=tmp_path / 'missing.html',
            plain_email_template=tmp_path / 'missing.txt',
            email_subject_template='Deletion warning for {platform_hostname}',
            fallback_to_channel_manager=False,
            fallback_email='fallback@example.com',
            apply=True,
        )

    mock_smtp.factory.assert_called_once_with('smtp.example.com', 587)
    assert [call.args[1] for call in sendmail.call_args_list] == [
        'error@example.com', 'fallback@example.com',
    ]
    assert mock_smtp.has_mail('sender@example.com', 'fallback@example.com', [media['oid']])
    assert [(email['recipient'], email['status']) for email in report_data] == [
        ('error@example.com', 'failed_smtp'),
        ('fallback@example.com', 'sent'),
    ]


@pytest.mark.parametrize('error', [
    smtplib.SMTPServerDisconnected('Connection lost <unexpectedly>'),
    ConnectionResetError('Connection reset'),
    TimeoutError('Connection timed out'),
    smtplib.SMTPRecipientsRefused({'jane.doe@example.com': (421, b'Service unavailable')}),
    smtplib.SMTPSenderRefused(421, b'Service unavailable', 'sender@example.com'),
    smtplib.SMTPDataError(421, b'Service unavailable'),
])
def test_smtp_second_connection_failure_preserves_sent_and_unsent_emails(
    warn_speakers, catalog, tmp_path, error,
):
    first, second = MockSMTP(), MockSMTP()
    original_sendmail = first.sendmail

    def sendmail(sender, recipient, message):
        if recipient == 'jane.doe@example.com':
            raise error
        original_sendmail(sender, recipient, message)

    first.sendmail = mock.Mock(side_effect=sendmail)
    second.sendmail = mock.Mock(side_effect=error)
    with mock.patch('smtplib.SMTP', side_effect=[first, second]) as factory:
        report_data = warn_speakers([
            catalog['videos'][0], catalog['videos'][5],
            catalog['lives'][0], catalog['videos'][1],
        ])

    assert factory.call_count == 2
    assert first.closed and second.closed
    assert [call.args[1] for call in first.sendmail.call_args_list] == [
        'john.doe@example.com', 'jane.doe@example.com',
    ]
    assert [call.args[1] for call in second.sendmail.call_args_list] == ['jane.doe@example.com']
    assert len(first.mailbox) == 1
    expected_statuses = {
        'john.doe@example.com': 'sent',
        'jane.doe@example.com': 'failed_smtp_disconnect',
        'june.doe@example.com': 'skipped_smtp_disconnect',
        'fallback@example.com': 'skipped_smtp_disconnect',
    }
    assert {email['recipient']: email['status'] for email in report_data} == expected_statuses
    assert report_data[0]['error'] == ''
    assert str(error) in report_data[1]['error']
    assert report_data[2]['error'] == report_data[1]['error']

    html_path, csv_path = tmp_path / 'emails.html', tmp_path / 'emails.csv'
    _generate_email_report(report_data, 'https://video.example', html_path, apply=True)
    _generate_email_csv(report_data, catalog['channels'], csv_path)
    doc = html_path.read_text(encoding='utf-8')
    for status in expected_statuses.values():
        assert EMAIL_STATUS_LABELS[status] in doc
    assert 'Delivery of the interrupted email could not be confirmed' in doc
    assert '<unexpectedly>' not in doc
    assert 'Medias notified:' not in doc
    with csv_path.open(encoding='utf-8', newline='') as file:
        rows = list(csv.DictReader(file))
    assert {row['email']: row['status'] for row in rows} == expected_statuses
    assert next(row for row in rows if row['email'] == 'jane.doe@example.com')['error'] == report_data[1]['error']


@pytest.mark.parametrize('stage', ['connect', 'starttls', 'login'])
def test_smtp_setup_failure_switches_to_dry_run_after_retry(warn_speakers, catalog, stage):
    error = smtplib.SMTPServerDisconnected('Connection lost during setup')
    connections = [MockSMTP(), MockSMTP()]
    if stage == 'connect':
        side_effect = [ConnectionRefusedError('Server unavailable')] * 2
    else:
        for connection in connections:
            setattr(connection, stage, mock.Mock(side_effect=error))
        side_effect = connections
    with mock.patch('smtplib.SMTP', side_effect=side_effect) as factory:
        report_data = warn_speakers([catalog['videos'][5]])
    assert factory.call_count == 2
    assert [email['status'] for email in report_data] == ['failed_smtp_disconnect', 'skipped_smtp_disconnect']
    assert all(not connection.mailbox for connection in connections)
    if stage != 'connect':
        assert all(connection.closed for connection in connections)


@pytest.mark.parametrize('fallback_email', ['fallback@example.com', 'john.doe@example.com'])
def test_smtp_fallback_disconnect_preserves_both_messages(warn_speakers, catalog, tmp_path, fallback_email):
    error = smtplib.SMTPServerDisconnected('Connection lost')
    first, second = MockSMTP(), MockSMTP()
    first.sendmail = mock.Mock(side_effect=[{}, error])
    second.sendmail = mock.Mock(side_effect=error)
    with mock.patch('smtplib.SMTP', side_effect=[first, second]) as factory:
        report_data = warn_speakers(
            [catalog['videos'][0], catalog['videos'][1]], fallback_email=fallback_email,
        )
    assert factory.call_count == 2
    assert [(email['recipient'], email['status']) for email in report_data] == [
        ('john.doe@example.com', 'sent'),
        (fallback_email, 'failed_smtp_disconnect'),
    ]
    csv_path = tmp_path / 'emails.csv'
    _generate_email_csv(report_data, catalog['channels'], csv_path)
    with csv_path.open(encoding='utf-8', newline='') as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert {row['email_number']: row['status'] for row in rows} == {
        '1': 'sent', '2': 'failed_smtp_disconnect',
    }


def test_smtp_dry_run_records_unsent_messages_without_connecting(warn_speakers, catalog, mock_smtp):
    report_data = warn_speakers([catalog['videos'][0], catalog['videos'][1]], apply=False)
    mock_smtp.factory.assert_not_called()
    assert [email['status'] for email in report_data] == ['dry_run', 'dry_run']


def test_smtp_fallback_rejection_still_stops_the_run(warn_speakers, catalog, mock_smtp):
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        warn_speakers([catalog['videos'][1]], fallback_email='error@example.com')
    mock_smtp.factory.assert_called_once_with('smtp.example.com', 587)
    assert mock_smtp.closed


@pytest.mark.parametrize('delete_date', [TODAY, IN_A_MONTH])
def test_smtp_disconnect_writes_reports_and_disables_deletion(
    api_client, catalog, no_prompt, tmp_path, delete_date,
):
    no_prompt.side_effect = ['y', '0']
    html_path, csv_path = tmp_path / 'emails.html', tmp_path / 'emails.csv'
    medias = [catalog['videos'][0], catalog['videos'][1]]
    tree = {'channels': [{'oid': 'channel_1', 'title': 'Faculty'}]}
    error = smtplib.SMTPServerDisconnected('Connection lost')
    first, second = MockSMTP(), MockSMTP()
    first.sendmail = mock.Mock(side_effect=[{}, error])
    second.sendmail = mock.Mock(side_effect=error)
    with (
        mock.patch.object(api_client, 'get_catalog', return_value=tree),
        mock.patch('examples.mass_delete_old_medias._get_medias', return_value=(medias, [], catalog['channels'])),
        mock.patch('smtplib.SMTP', side_effect=[first, second]) as factory,
    ):
        delete_old_medias([
            '--conf=./conf.json', f'--delete-date={delete_date}',
            f'--added-before={ONE_YEAR_AGO}', '--apply', '--send-email-on-deletion',
            '--fallback-email=fallback@example.com', '--media-report=', '--media-csv=',
            f'--email-report={html_path}', f'--email-csv={csv_path}',
        ])
    assert factory.call_count == 2
    assert 'Failed — SMTP connection failure' in html_path.read_text(encoding='utf-8')
    with csv_path.open(encoding='utf-8', newline='') as file:
        rows = list(csv.DictReader(file))
    assert {row['email']: row['status'] for row in rows} == {
        'john.doe@example.com': 'sent', 'fallback@example.com': 'failed_smtp_disconnect',
    }
    assert not any(call.args[0] == 'catalog/bulk_delete/' for call in api_client.api.call_args_list)


@pytest.mark.parametrize(
    'delete_date, added_before, skip_category, apply,'
    'expected_sent_mails, expected_deleted_oids', [
        pytest.param(
            IN_A_MONTH, TWO_YEARS_AGO, 'do not delete', True,
            [
                ('fallback@example.com', [
                    'three_years_ago_no_speaker', 'three_years_ago_mail_error']),
                ('john.doe@example.com', [
                    'four_years_ago', 'five_years_ago', 'live_three_years_ago']),
                ('jane.doe@example.com', ['five_years_ago']),
                ('june.doe@example.com', ['live_three_years_ago']),
            ], [], id='First notification'
        ),
        pytest.param(
            IN_A_MONTH, TWO_YEARS_AGO, 'do not delete', False,
            [], [], id='First notification - dry-run'
        ),
        pytest.param(
            TOMORROW, TWO_YEARS_AGO, 'do not delete', True,
            [
                ('fallback@example.com', [
                    'three_years_ago_no_speaker', 'three_years_ago_mail_error']),
                ('john.doe@example.com', [
                    'four_years_ago', 'five_years_ago', 'live_three_years_ago']),
                ('jane.doe@example.com', ['five_years_ago']),
                ('june.doe@example.com', ['live_three_years_ago']),
            ], [], id='Second notification'
        ),
        pytest.param(
            TODAY, TWO_YEARS_AGO, 'do not delete', True,
            [], [
                'three_years_ago_no_speaker', 'three_years_ago_mail_error',
                'four_years_ago', 'five_years_ago', 'live_three_years_ago'
            ], id='Deletion'
        ),
        pytest.param(
            TODAY, TWO_YEARS_AGO, 'do not delete', False,
            [], [], id='Deletion - dry-run'
        ),
    ]
)
def test_delete_old_medias__full_workflow(
    api_client, mock_smtp,
    delete_date, added_before, skip_category, apply,
    expected_sent_mails, expected_deleted_oids,
):
    delete_old_medias([
        '--conf=./conf.json',
        f'--delete-date={delete_date.strftime("%Y-%m-%d")}',
        f'--added-before={added_before.strftime("%Y-%m-%d")}',
        f'--skip-category={skip_category}',
        '--fallback-email=fallback@example.com',
        *(('--apply',) if apply else ()),
        '--log-level=info',
    ])

    # Check mails
    assert len(mock_smtp.mailbox) == len(expected_sent_mails)
    for recipient, oids in expected_sent_mails:
        assert mock_smtp.has_mail('sender@example.com', recipient, oids)
    if expected_sent_mails:
        mock_smtp.factory.assert_called_once_with('smtp.example.com', 587)
    else:
        mock_smtp.factory.assert_not_called()

    # Check api calls and deleted oids
    assert api_client.api.call_count == 2 if expected_deleted_oids else 1
    assert api_client.api.call_args_list[0] == mock.call(
        'catalog/get-all/',
        params={'format': 'json'},
        parse_json=True,
        timeout=120
    )
    if expected_deleted_oids:
        assert api_client.api.call_args_list[1] == mock.call(
            'catalog/bulk_delete/',
            method='post',
            data=dict(oids=expected_deleted_oids)
        )


@pytest.mark.parametrize(
    'added_after, added_before, skip_categories,'
    'views_max_count, views_playback_threshold, views_after, views_before,'
    'expected_deleted_oids', [
        pytest.param(
            FOUR_YEARS_AGO, TWO_YEARS_AGO, ['do not delete'],
            None, None, None, None,
            [
                'three_years_ago_no_speaker',
                'three_years_ago_mail_error',
                'four_years_ago',
                'live_three_years_ago',
            ], id='Filter by added date'
        ),
        pytest.param(
            FOUR_YEARS_AGO, TWO_YEARS_AGO, ['do not delete', 'some_category'],
            None, None, None, None,
            [
                'three_years_ago_no_speaker',
                'three_years_ago_mail_error',
                'live_three_years_ago',
            ], id='Filter by added date and multiple category'
        ),
        pytest.param(
            None, None, ['do not delete', 'some_category'],
            1, 5, TWO_YEARS_AGO, ONE_YEAR_AGO,
            [
                'three_years_ago_no_speaker',
                'three_years_ago_mail_error',
            ], id='Filter by views count'
        ),
        pytest.param(
            None, None, ['do not delete', 'some_category'],
            1, 5, THREE_YEARS_AGO, TWO_YEARS_AGO,
            [], id='Filter by views eliminates media created after beginning of view period'
        ),
    ]
)
def test_delete_old_medias__selection_filters(
    api_client,
    added_after, added_before, skip_categories,
    views_max_count, views_playback_threshold, views_after, views_before,
    expected_deleted_oids,
):
    params = [
        '--conf=./conf.json',
        f'--delete-date={TODAY.strftime("%Y-%m-%d")}',
        '--fallback-email=fallback@example.com',
        '--apply',
        '--log-level=debug',
    ]
    if added_after is not None:
        params.append(f'--added-after={added_after.strftime("%Y-%m-%d")}')
    if added_before is not None:
        params.append(f'--added-before={added_before.strftime("%Y-%m-%d")}')
    if skip_categories is not None:
        params += [f'--skip-category={skip_category}' for skip_category in skip_categories]
    if views_max_count is not None:
        params.append(f'--views-max-count={views_max_count}')
    if views_playback_threshold is not None:
        params.append(f'--views-playback-threshold={views_playback_threshold}')
    if views_after is not None:
        params.append(f'--views-after={views_after.strftime("%Y-%m-%d")}')
    if views_before is not None:
        params.append(f'--views-before={views_before.strftime("%Y-%m-%d")}')

    delete_old_medias(params)

    api_calls = iter(api_client.api.call_args_list)
    assert next(api_calls) == mock.call(
        'catalog/get-all/',
        params={'format': 'json'},
        parse_json=True,
        timeout=120
    )

    if views_max_count:
        assert next(api_calls) == mock.call(
            'stats/unwatched/',
            params={
                'playback_threshold': views_playback_threshold,
                'views_threshold': views_max_count,
                'recursive': 'yes',
                'sd': views_after.strftime('%Y-%m-%d'),
                'ed': views_before.strftime('%Y-%m-%d'),
            },
        )

    if expected_deleted_oids:
        assert next(api_calls) == mock.call(
            'catalog/bulk_delete/',
            method='post',
            data=dict(oids=expected_deleted_oids)
        )


@pytest.mark.parametrize(
    'delete_date, send_email_on_deletion, fallback_to_channel_manager, apply, expected_sent_mails', [
        pytest.param(
            IN_A_MONTH, False, False, True,
            [
                ('fallback@example.com', ['three_years_ago_no_speaker', 'three_years_ago_mail_error']),
                ('john.doe@example.com', ['four_years_ago', 'live_three_years_ago']),
                ('june.doe@example.com', ['live_three_years_ago']),
            ], id='First notification'
        ),
        pytest.param(
            IN_A_MONTH, False, True, True,
            [
                ('manager@example.com', ['three_years_ago_no_speaker']),
                ('fallback@example.com', ['three_years_ago_mail_error']),
                ('john.doe@example.com', ['four_years_ago', 'live_three_years_ago']),
                ('june.doe@example.com', ['live_three_years_ago']),
            ], id='First notification - fallback on channel_manager'
        ),
        pytest.param(
            IN_A_MONTH, False, False, False,
            [], id='First notification - dry-run'
        ),
        pytest.param(
            TODAY, False, False, True,
            [], id='Deletion - no mails on deletion'
        ),
        pytest.param(
            TODAY, True, False, True,
            [
                ('fallback@example.com', ['three_years_ago_no_speaker', 'three_years_ago_mail_error']),
                ('john.doe@example.com', ['four_years_ago', 'live_three_years_ago']),
                ('june.doe@example.com', ['live_three_years_ago']),
            ], id='Deletion - send mails on deletion'
        ),
        pytest.param(
            TODAY, True, False, False,
            [], id='Deletion - send mails on deletion - dry-run'
        ),
    ]
)
@pytest.mark.usefixtures('api_client')
def test_delete_old_medias__mailing_behaviour(
    mock_smtp,
    delete_date, send_email_on_deletion, fallback_to_channel_manager, apply,
    expected_sent_mails,
):
    delete_old_medias([
        '--conf=./conf.json',
        f'--delete-date={delete_date.strftime("%Y-%m-%d")}',
        f'--added-after={FOUR_YEARS_AGO.strftime("%Y-%m-%d")}',
        f'--added-before={TWO_YEARS_AGO.strftime("%Y-%m-%d")}',
        '--skip-category="do not delete"',
        '--fallback-email=fallback@example.com',
        *(('--send-email-on-deletion',) if send_email_on_deletion else ()),
        *(('--fallback-to-channel-manager',) if fallback_to_channel_manager else ()),
        *(('--apply',) if apply else ()),
        '--log-level=info',
    ])

    # Check mails
    assert len(mock_smtp.mailbox) == len(expected_sent_mails)
    for recipient, oids in expected_sent_mails:
        assert mock_smtp.has_mail('sender@example.com', recipient, oids)


@pytest.mark.parametrize(
    'added_after, added_before, views_max_count, views_after, views_before', [
        pytest.param(
            None, None, None, None, None,
            id='No filters'
        ),
        pytest.param(
            None, None, 3, None, None,
            id='No views period'
        ),
        pytest.param(
            None, None, 3, THREE_YEARS_AGO, None,
            id='Incomplete views period - start only'
        ),
        pytest.param(
            None, None, 3, None, TWO_YEARS_AGO,
            id='Incomplete views period - end only'
        ),
        pytest.param(
            None, None, 3, ONE_YEAR_AGO, TODAY,
            id='Views period crosses today'
        ),
        pytest.param(
            None, None, -3, THREE_YEARS_AGO, ONE_YEAR_AGO,
            id='Negative views count'
        ),
    ]
)
@pytest.mark.usefixtures('api_client')
def test_delete_old_medias__misconfigured(
    added_after, added_before, views_max_count, views_after, views_before,
):
    params = [
        '--conf=./conf.json',
        f'--delete-date={TODAY.strftime("%Y-%m-%d")}',
        '--fallback-email=fallback@example.com',
        '--apply',
        '--log-level=debug',
    ]
    if added_after is not None:
        params.append(f'--added-after={added_after.strftime("%Y-%m-%d")}')
    if added_before is not None:
        params.append(f'--added-before={added_before.strftime("%Y-%m-%d")}')
    if views_max_count is not None:
        params.append(f'--views-max-count={views_max_count}')
    if views_after is not None:
        params.append(f'--views-after={views_after.strftime("%Y-%m-%d")}')
    if views_before is not None:
        params.append(f'--views-before={views_before.strftime("%Y-%m-%d")}')

    with pytest.raises(MisconfiguredError):
        delete_old_medias(params)


def test_generate_media_csv_groups_deleted_medias_in_one_cell(tmp_path):
    channels = [
        {'oid': 'faculty', 'title': 'Faculty'},
        {'oid': 'course', 'title': 'Course', 'parent_oid': 'faculty'},
        {'oid': 'edition', 'title': '2025', 'parent_oid': 'course'},
    ]
    records = [
        {
            'oid': 'media-b',
            'title': 'Media B',
            'parent_oid': 'edition',
            'status': 'delete',
        },
        {
            'oid': 'media-skipped',
            'title': 'Skipped Media',
            'parent_oid': 'edition',
            'status': 'skip_categories',
        },
        {
            'oid': 'media-a',
            'title': 'Media A',
            'parent_oid': 'edition',
            'status': 'delete',
        },
    ]
    output_path = tmp_path / 'media_report.csv'

    _generate_media_csv(
        channels,
        records,
        server_url='https://video.example/',
        output_path=output_path,
    )

    assert len(output_path.read_text(encoding='utf-8').splitlines()) == 2
    with output_path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
            'Faculty',
            'Course Name',
            'Link to course',
            'Edition Name',
            'Link to Course Edition',
            'Medias Deleted',
        ]
        assert list(reader) == [{
            'Faculty': 'Faculty',
            'Course Name': 'Course',
            'Link to course': 'https://video.example/permalink/course/',
            'Edition Name': '2025',
            'Link to Course Edition': 'https://video.example/permalink/edition/',
            'Medias Deleted': 'Media A | Media B',
        }]


def test_get_templates_reads_custom_templates_as_utf8(tmp_path):
    html_path = tmp_path / 'email.html'
    plain_path = tmp_path / 'email.txt'
    html_path.write_text('<p>⚠️ Eén waarschuwing</p>', encoding='utf-8')
    plain_path.write_text('⚠️ Eén waarschuwing', encoding='utf-8')

    html_template, plain_template = _get_templates(html_path, plain_path)

    assert html_template == '<p>⚠️ Eén waarschuwing</p>'
    assert plain_template == '⚠️ Eén waarschuwing'
