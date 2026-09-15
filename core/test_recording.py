"""Behavior tests for time-gated recording, visibility, and persistent early files."""
import uuid
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import tests as fixtures
from .models import AttachmentSettings, Card, QuickNote, RecordingSchedule, User
from .recording import archive_state, opening_time, phase
from .services import card_directory, card_file_inventory, quarantine_card, restore_card, save_quick_note, submit_card, verify_card_integrity


class RecordingTests(TestCase):
    setUp = fixtures.DiaryTests.setUp
    tearDown = fixtures.DiaryTests.tearDown

    def clock(self, hour=10, minute=0, day=16, second=0):
        return patch('django.utils.timezone.now', return_value=timezone.make_aware(datetime(2026, 8, day, hour, minute, second)))

    def card(self):
        return Card.objects.get_or_create(patient=self.patient, local_date=date(2026, 8, 16))[0]

    def early(self, body='A quiet morning', files=None, key=None):
        with self.clock():
            return save_quick_note(self.card(), body, files or [], self.patient, key or uuid.uuid4())

    def upload(self, body=b'early file'):
        return SimpleUploadedFile('early.txt', body, content_type='text/plain')

    def full(self, files=None):
        with self.clock(20):
            return submit_card(self.card(), {'emotions': [{'id': 'joy', 'label': 'Joy', 'intensity': 3}]}, files or [], self.patient)

    def test_window_boundaries(self):
        day = date(2026, 8, 16)
        for hour, minute, second, expected in [(0, 0, 0, 'early'), (18, 59, 59, 'early'), (19, 0, 0, 'reflection'), (23, 59, 59, 'reflection')]:
            with self.subTest(hour=hour), self.clock(hour, minute, second=second):
                self.assertEqual(phase(day), expected)
        with self.clock(0, day=17):
            self.assertEqual(phase(day), 'closed')
            self.assertEqual(phase(date(2026, 8, 17)), 'early')

    def test_schedule_uses_local_timezone_and_dst(self):
        with timezone.override(ZoneInfo('America/New_York')):
            # Both occurrences of 01:30 during fall-back are early.
            for instant in [datetime(2026, 11, 1, 5, 30, tzinfo=dt_timezone.utc), datetime(2026, 11, 1, 6, 30, tzinfo=dt_timezone.utc)]:
                self.assertEqual(phase(date(2026, 11, 1), instant), 'early')
            self.assertEqual(phase(date(2026, 3, 8), datetime(2026, 3, 8, 23, tzinfo=dt_timezone.utc)), 'reflection')
            self.assertEqual(phase(date(2026, 11, 1), datetime(2026, 11, 2, 0, tzinfo=dt_timezone.utc)), 'reflection')

    def test_shared_schedule_changes_tomorrow_and_requires_admin(self):
        url = reverse('control:recording_schedule')
        self.client.force_login(self.patient)
        self.assertEqual(self.client.post(url, {'opens_at': '18:30'}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url, {'opens_at': '18:30'}).status_code, 302)
        self.assertEqual(opening_time(), time(19))
        self.assertEqual(opening_time(date(2026, 8, 17)), time(18, 30))
        self.client.post(url, {'opens_at': '25:00'})
        self.assertEqual(RecordingSchedule.objects.count(), 1)

    def test_separate_timestamped_notes_and_retry_idempotency(self):
        key = uuid.uuid4()
        first = self.early(key=key, files=[self.upload()])
        second = self.early(body='Another thought')
        self.early(key=key, files=[self.upload()])
        card = self.card()
        self.assertEqual(card.quick_notes.count(), 2)
        self.assertEqual(card.attachments.count(), 1)
        self.assertNotEqual(first.filename, second.filename)
        text = (card_directory(card) / first.filename).read_text()
        self.assertIn('2026-08-16T10:00:00+00:00', text)
        self.assertIn(str(card.pk), text)
        self.assertEqual(verify_card_integrity(card), 'verified')

    def test_early_attachment_only_and_empty_rejection(self):
        self.early(body='', files=[self.upload()])
        self.assertEqual(self.card().quick_notes.get().filename, '')
        with self.clock(), self.assertRaisesMessage(ValidationError, 'Write a quick note'):
            save_quick_note(self.card(), '   ', [], self.patient, uuid.uuid4())
        with self.clock(0, day=17): self.assertEqual(archive_state(self.card(), date(2026, 8, 16)), 'incomplete')

    def test_full_submission_preserves_early_files_and_raw_notes(self):
        note = self.early(files=[self.upload()])
        before = {p.name: p.read_bytes() for p in card_directory(self.card()).iterdir()}
        card = self.full([self.upload()])
        self.assertEqual(card.attachments.count(), 2)
        for name, content in before.items(): self.assertEqual((card_directory(card)/name).read_bytes(), content)
        self.client.force_login(self.patient)
        self.assertContains(self.client.get(reverse('journal:detail', args=[card.pk])), 'A quiet morning')
        self.assertContains(self.client.get(reverse('journal:detail', args=[card.pk])+'?view=raw'), note.filename)
        self.assertEqual(verify_card_integrity(card), 'verified')

    def test_submission_failure_leaves_early_files_untouched(self):
        self.early(files=[self.upload()])
        card = self.card()
        before = {p.name: p.read_bytes() for p in card_directory(card).iterdir()}
        with patch('core.services.atomic_write_upload', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.full([self.upload()])
        card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.DRAFT)
        self.assertEqual(before, {p.name: p.read_bytes() for p in card_directory(card).iterdir()})
        self.assertEqual(card.attachments.count(), 1)
        self.full()

    def test_early_batch_failure_rolls_back_note_and_files(self):
        self.early()
        card = self.card()
        before = set(card_directory(card).iterdir())
        with patch('core.services.atomic_write_upload', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.early(body='failed note', files=[self.upload()])
        self.assertEqual(set(card_directory(card).iterdir()), before)
        self.assertEqual(card.quick_notes.count(), 1)

    def test_existing_early_uploads_count_toward_evening_quota(self):
        AttachmentSettings.objects.update_or_create(pk=1, defaults={'file_limit_mib': 1, 'card_limit_mib': 1})
        self.early(files=[self.upload(b'a' * 1024 * 1024)])
        with self.assertRaisesMessage(ValidationError, 'card limit'): self.full([self.upload()])
        self.client.force_login(self.patient)
        self.assertContains(self.client.get(reverse('journal:today')), 'data-stored-bytes="1048576"')

    def test_early_visibility_changes_on_submission_or_midnight(self):
        self.early(files=[self.upload()])
        card = self.card(); attachment = card.attachments.get()
        download = reverse('journal:attachment', args=[card.pk, attachment.pk])
        self.client.force_login(self.patient)
        with self.clock():
            self.assertContains(self.client.get(reverse('journal:today')), 'A quiet morning')
            response = self.client.get(download); self.assertEqual(response.status_code, 200); response.close()
        with self.clock(19):
            form = self.client.get(reverse('journal:today'))
            self.assertContains(form, 'HOW DO YOU FEEL TODAY?')
            self.assertNotContains(form, 'A quiet morning')
            self.assertNotContains(form, 'early.txt')
            self.assertEqual(self.client.get(download).status_code, 404)
        self.client.force_login(self.reviewer)
        day_url = reverse('review:day', args=[self.patient.pk, '2026-08-16'])
        self.assertNotContains(self.client.get(day_url), 'A quiet morning')
        self.assertEqual(self.client.get(download).status_code, 404)
        with self.clock(0, day=17):
            self.client.force_login(self.reviewer)
            self.assertContains(self.client.get(day_url), 'A quiet morning')
            response = self.client.get(download); self.assertEqual(response.status_code, 200); response.close()

    def test_no_phase_or_ownership_bypass(self):
        self.early()
        self.client.force_login(self.patient)
        early_url = reverse('journal:early_save')
        data = {'recording_date': '2026-08-16', 'body': 'late note', 'operation_id': str(uuid.uuid4())}
        response = self.client.post(early_url, data, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 409)
        with self.clock():
            response = self.client.post(reverse('journal:today'), {'recording_date': '2026-08-16', 'action': 'submit', 'emotions': ['joy']})
            self.assertEqual(response.status_code, 409)
        with self.clock(0, day=17):
            self.client.force_login(self.patient)
            for url in [early_url, reverse('journal:today')]:
                self.assertEqual(self.client.post(url, data, HTTP_ACCEPT='application/json').status_code, 409)
            self.assertFalse(Card.objects.filter(patient=self.patient, local_date=date(2026, 8, 17)).exists())
        stranger = User.objects.create_user(username='stranger', role=User.Role.PATIENT)
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('review:day', args=[self.patient.pk, '2026-08-16'])).status_code, 404)

    def test_past_form_draft_and_missing_days_are_read_only(self):
        self.patient.date_joined = timezone.make_aware(datetime(2026, 8, 13)); self.patient.save()
        Card.objects.create(patient=self.patient, local_date=date(2026, 8, 14), draft_data={'emotions': [{'id': 'joy', 'label': 'Joy', 'intensity': 2, 'note': 'unfinished thought'}]})
        Card.objects.create(patient=self.patient, local_date=date(2026, 8, 15), draft_data={'custom': [{'value': ''}]})
        for user, route, args in [(self.patient, 'journal:day', []), (self.reviewer, 'review:day', [self.patient.pk])]:
            self.client.force_login(user)
            incomplete = self.client.get(reverse(route, args=args+['2026-08-14']))
            self.assertContains(incomplete, 'unfinished thought')
            self.assertContains(incomplete, 'INCOMPLETE')
            self.assertNotContains(incomplete, 'LOCK + SUBMIT')
            self.assertNotContains(incomplete, 'ADD ADDENDUM')
            self.assertContains(self.client.get(reverse(route, args=args+['2026-08-13'])), 'NO SIGNAL RECORDED')
            self.assertContains(self.client.get(reverse(route, args=args+['2026-08-15'])), 'NO SIGNAL RECORDED')
            self.assertEqual(self.client.get(reverse(route, args=args+['2026-08-12'])).status_code, 404)
            self.assertEqual(self.client.get(reverse(route, args=args+['2026-08-17'])).status_code, 404)
        self.assertEqual(Card.objects.count(), 2)

    def test_archives_include_join_date_and_paginate_virtual_days(self):
        self.patient.date_joined = timezone.make_aware(datetime(2026, 7, 1)); self.patient.save()
        for user, route in [(self.patient, 'journal:history'), (self.reviewer, 'review:list')]:
            self.client.force_login(user)
            response = self.client.get(reverse(route))
            self.assertEqual(len(response.context['cards']), 31)
            self.assertEqual(response.context['archive_page'].paginator.count, 47)
            calendar = self.client.get(reverse(route), {'view': 'calendar', 'month': '2026-07'})
            self.assertContains(calendar, 'MISSING')
            last = self.client.get(reverse(route), {'page': 2})
            self.assertEqual(last.context['cards'][-1].local_date, date(2026, 7, 1))
        self.assertFalse(Card.objects.exists())

    def test_incomplete_integrity_and_quarantine_restore(self):
        note = self.early(files=[self.upload()]); card = self.card()
        quarantine_card(card, self.admin); restore_card(card, self.admin); card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.DRAFT)
        self.assertEqual(verify_card_integrity(card), 'verified')
        with self.clock(0, day=17):
            self.client.force_login(self.reviewer)
            url = reverse('review:day', args=[self.patient.pk, '2026-08-16'])
            self.assertEqual(self.client.get(url).status_code, 200)
            (card_directory(card)/note.filename).write_text('tampered')
            self.assertEqual(self.client.get(url).status_code, 409)
            quarantine_card(card, self.admin)
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_empty_draft_inventory_does_not_read_year_directory(self):
        self.early()
        empty = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 15))
        self.assertEqual(card_file_inventory(empty)['file_count'], 0)
        with self.assertRaises(ValidationError): quarantine_card(empty, self.admin)

    def test_evening_draft_save_becomes_incomplete_at_midnight(self):
        self.client.force_login(self.patient)
        response = self.client.post(reverse('journal:today'), {'recording_date': '2026-08-16', 'action': 'save', 'emotions': ['joy'], 'intensity_joy': '4'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        with self.clock(0, day=17):
            self.client.force_login(self.patient)
            self.assertEqual(archive_state(self.card(), date(2026, 8, 16)), 'incomplete')
            self.assertEqual(self.client.post(reverse('journal:today'), {'recording_date': '2026-08-16', 'action': 'submit'}, HTTP_ACCEPT='application/json').status_code, 409)

    def test_writes_crossing_boundary_roll_back_their_new_files(self):
        self.early()
        card = self.card()
        original = set(card_directory(card).iterdir())
        from .services import require_phase as real_require_phase
        calls = []
        def cross_boundary(candidate, expected):
            calls.append(expected)
            if len(calls) == 2: raise ValidationError('This recording window has closed.')
            return real_require_phase(candidate, expected)
        with patch('core.services.require_phase', side_effect=cross_boundary), self.assertRaises(ValidationError):
            self.early(body='crossing cutoff', files=[self.upload()])
        self.assertEqual(set(card_directory(card).iterdir()), original)
        self.assertEqual(card.quick_notes.count(), 1)
        calls.clear()
        with patch('core.services.require_phase', side_effect=cross_boundary), self.assertRaises(ValidationError):
            self.full([self.upload()])
        card.refresh_from_db()
        self.assertEqual(card.status, Card.Status.DRAFT)
        self.assertEqual(set(card_directory(card).iterdir()), original)

    def test_early_endpoint_and_non_json_cutoff_do_not_expose_hidden_notes(self):
        self.client.force_login(self.patient)
        with self.clock():
            self.client.get(reverse('journal:today'))
            response = self.client.post(reverse('journal:early_save'), {'recording_date': '2026-08-16', 'body': 'private early words', 'operation_id': str(uuid.uuid4())}, HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 200)
        self.client.force_login(self.patient)
        response = self.client.post(reverse('journal:early_save'), {'recording_date': '2026-08-16', 'body': 'unsaved late words', 'operation_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, 'private early words', status_code=409)
        self.assertContains(response, 'unsaved late words', status_code=409)
        self.full()
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('review:detail', args=[self.card().pk]))
        self.assertContains(response, 'private early words')

    def test_tampered_early_note_cannot_be_submitted(self):
        note = self.early()
        (card_directory(self.card()) / note.filename).write_text('tampered')
        with self.assertRaisesMessage(ValidationError, 'could not be verified'): self.full()
        self.assertEqual(self.card().status, Card.Status.DRAFT)

    def test_unassigned_therapist_and_admin_cannot_read_incomplete_day(self):
        self.early(files=[self.upload()])
        stranger = User.objects.create_user(username='unassigned', role=User.Role.REVIEWER)
        with self.clock(0, day=17):
            for user in [stranger, self.admin]:
                self.client.force_login(user)
                response = self.client.get(reverse('review:day', args=[self.patient.pk, '2026-08-16']))
                self.assertIn(response.status_code, [403, 404])

    def test_numeric_zero_is_saved_content(self):
        card = self.card(); card.draft_data = {'custom': [{'type': 'number', 'value': 0}]}; card.save()
        with self.clock(0, day=17): self.assertEqual(archive_state(card, card.local_date), 'incomplete')

    def test_apply_now_opens_form_immediately_and_replaces_pending_time(self):
        self.early(body='Keep this early note')
        url = reverse('control:recording_schedule')
        with self.clock(12):
            self.client.force_login(self.admin)
            self.client.post(url, {'opens_at': '09:00', 'apply_when': 'tomorrow'})
            self.assertEqual(phase(date(2026, 8, 16)), 'early')
            response = self.client.post(url, {'opens_at': '11:00', 'apply_when': 'now'}, follow=True)
            self.assertContains(response, 'Recording time applied now.')
            self.assertEqual(opening_time(), time(11))
            self.assertEqual(opening_time(date(2026, 8, 17)), time(11))
            self.client.force_login(self.patient)
            response = self.client.get(reverse('journal:today'))
            self.assertContains(response, 'HOW DO YOU FEEL TODAY?')
            self.assertNotContains(response, 'Keep this early note')
            self.assertEqual(self.card().quick_notes.count(), 1)
            self.assertEqual(verify_card_integrity(self.card()), 'verified')

    def test_apply_now_can_restore_early_window_without_erasing_form_draft(self):
        card = self.card()
        card.draft_data = {'emotions': [{'id': 'joy', 'label': 'Joy', 'intensity': 3}]}
        card.save()
        self.client.force_login(self.admin)
        self.client.post(reverse('control:recording_schedule'), {'opens_at': '21:00', 'apply_when': 'now'})
        self.client.force_login(self.patient)
        self.assertContains(self.client.get(reverse('journal:today')), 'A quick note')
        card.refresh_from_db()
        self.assertEqual(card.draft_data['emotions'][0]['intensity'], 3)

    def test_invalid_schedule_timing_does_not_change_schedule(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('control:recording_schedule'), {'opens_at': '09:00', 'apply_when': 'yesterday'})
        self.assertEqual(opening_time(), time(19))
        self.assertFalse(RecordingSchedule.objects.exists())
