from unittest import mock

import examples.update_course_and_edition_channel_managers_email as script


def _course_and_editions():
    new_email = 'manager@example.com'
    course = {
        'oid': 'course-1',
        'title': 'COURSE1 Course title',
        'managers_emails_raw': 'old@example.com',
    }
    children_of = {
        course['oid']: [
            {
                'oid': 'edition-success',
                'managers_emails_raw': 'old@example.com',
            },
            {
                'oid': 'edition-error',
                'managers_emails_raw': 'old@example.com',
            },
            {
                'oid': 'edition-correct',
                'managers_emails_raw': new_email,
            },
        ],
    }
    return course, children_of, new_email


def test_failed_updates_are_counted_as_errors(monkeypatch):
    course, children_of, new_email = _course_and_editions()
    client = mock.Mock()

    def api_call(_url, *, method, data):
        if data['oid'] == 'course-1':
            raise RuntimeError(f"update failed for {data['oid']}")
        if data['oid'] == 'edition-error':
            raise RuntimeError()
        return {'success': True}

    client.api.side_effect = api_call
    monkeypatch.setattr(script, '_get_thread_client', lambda _conf_path: client)

    result = script._process_course_channel(
        course,
        {'COURSE1': new_email},
        children_of,
        'test-conf.json',
        False,
        'https://mediaserver.example',
        'Faculty',
    )

    row, course_updated, course_correct, course_error, updated, correct, errors = result
    assert row['Match'] == 'error'
    assert row['editions'] == 3
    assert row['updated'] == 1
    assert row['errors'] == 1
    assert course_updated is False
    assert course_correct is False
    assert course_error is True
    assert (updated, correct, errors) == (1, 1, 1)
    assert client.api.call_args_list == [
        mock.call(
            'channels/edit/',
            method='post',
            data={'oid': 'course-1', 'managers_emails': new_email},
        ),
        mock.call(
            'channels/edit/',
            method='post',
            data={'oid': 'edition-success', 'managers_emails': new_email},
        ),
        mock.call(
            'channels/edit/',
            method='post',
            data={'oid': 'edition-error', 'managers_emails': new_email},
        ),
    ]


def test_dry_run_counts_changes_without_creating_a_client(monkeypatch):
    course, children_of, new_email = _course_and_editions()
    get_client = mock.Mock()
    monkeypatch.setattr(script, '_get_thread_client', get_client)

    result = script._process_course_channel(
        course,
        {'COURSE1': new_email},
        children_of,
        'test-conf.json',
        True,
        'https://mediaserver.example',
        'Faculty',
    )

    row, course_updated, course_correct, course_error, updated, correct, errors = result
    assert row['Match'] == 'yes'
    assert row['editions'] == 3
    assert row['updated'] == 2
    assert row['errors'] == 0
    assert course_updated is True
    assert course_correct is False
    assert course_error is False
    assert (updated, correct, errors) == (2, 1, 0)
    get_client.assert_not_called()
