import hashlib
from pathlib import Path
from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from . import tests as fixtures
from .attachment_uploads import prepare_uploads, atomic_write_upload
from .models import AttachmentSettings, AuditEvent, Card, User
from .services import add_card_attachments, card_directory, submit_card, valid_signature, verify_card_integrity

MIB = 1024 * 1024
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42' + b'\x00\x00\x00\x10mdat12345678'
MP3 = b'\xff\xfb\x90\x00' + bytes(413)


def upload(name='clip.mp4', data=MP4, kind='video/mp4'):
    return SimpleUploadedFile(name, data, content_type=kind)


class AttachmentFeatureTests(TestCase):
    setUp = fixtures.DiaryTests.setUp
    tearDown = fixtures.DiaryTests.tearDown
    submitted = fixtures.DiaryTests.submitted

    def limits(self, file=1, card=2):
        AttachmentSettings.objects.update_or_create(pk=1, defaults={'file_limit_mib': file, 'card_limit_mib': card})

    def test_progress_upload_submission_and_validation(self):
        self.client.force_login(self.patient)
        url = reverse('journal:today')
        response = self.client.post(url, {'recording_date': '2026-08-16', 'action': 'submit', 'attachments': upload()}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('emotion', response.json()['error'])
        self.assertEqual(Card.objects.get(patient=self.patient).status, Card.Status.DRAFT)
        response = self.client.post(url, {'recording_date': '2026-08-16', 'action': 'submit', 'emotions': ['joy'], 'intensity_joy': '3', 'attachments': upload()}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        card = Card.objects.get(patient=self.patient)
        self.assertEqual(card.status, Card.Status.SUBMITTED)
        self.assertEqual(card.attachments.count(), 1)
        self.assertEqual(response.json()['redirect'], reverse('journal:detail', args=[card.id]))
        self.assertContains(self.client.get(response.json()['redirect']), 'Diary card locked and submitted.')

    def test_progress_append_success_and_error(self):
        card = self.submitted()
        self.client.force_login(self.patient)
        url = reverse('journal:attachment_add', args=[card.id])
        response = self.client.post(url, {'attachments': upload()}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['redirect'], reverse('journal:detail', args=[card.id]))
        response = self.client.post(url, {'attachments': upload(data=b'invalid')}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('declared type', response.json()['error'])
        self.assertEqual(card.attachments.count(), 2)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.patient)
        self.assertEqual(csrf_client.post(url, {'attachments': upload()}, HTTP_ACCEPT='application/json').status_code, 403)

    def test_defaults_and_media_types_on_both_upload_paths(self):
        limits = AttachmentSettings.current()
        self.assertEqual((limits.file_limit_mib, limits.card_limit_mib), (100, 500))
        card = Card.objects.create(patient=self.patient, local_date=date(2026, 8, 16))
        submit_card(card, {'emotions': [{'id': 'joy', 'intensity': 3}]}, [upload()], self.patient)
        for kind in ('audio/mp4', 'audio/x-m4a', 'video/mp4', 'application/octet-stream'):
            add_card_attachments(card, [upload('recording.m4a', MP4, kind)], self.patient)
        for kind in ('audio/mpeg', 'audio/mp3', 'audio/x-mp3'):
            add_card_attachments(card, [upload('music.mp3', MP3, kind)], self.patient)
        self.assertEqual(card.attachments.filter(content_type='audio/mp4').count(), 4)
        self.assertEqual(card.attachments.filter(content_type='audio/mpeg').count(), 3)
        self.assertEqual(verify_card_integrity(card), 'verified')
        for role, route in ((self.patient, 'journal:detail'), (self.reviewer, 'review:detail')):
            self.client.force_login(role)
            response = self.client.get(reverse(route, args=[card.id]))
            self.assertContains(response, 'data-media-type="video/mp4"')
            self.assertContains(response, 'data-viewer-audio controls')
            self.assertContains(response, 'data-viewer-video controls playsinline')
            if role == self.patient:
                self.assertContains(response, '100 MiB per file')
                self.assertContains(response, '500 MiB per card')

    def test_content_validation_and_aliases(self):
        for name, data, kind in [('bad.mp4', b'not a video', 'video/mp4'), ('bad.mp3', b'ID3', 'audio/mpeg'),
                                 ('bad.m4a', MP4[:8], 'audio/mp4'), ('bad.mp4', MP4.replace(b'isom', b'qt  '), 'video/mp4'),
                                 ('bad.txt', b'text\x00', 'text/plain'), ('bad.webm', MP4, 'video/webm')]:
            with self.subTest(name=name, data=data), self.assertRaises(ValidationError):
                prepare_uploads([upload(name, data, kind)], 0, valid_signature)
        tagged = b'ID3\x04\x00\x00\x00\x00\x00\x04test' + MP3
        self.assertEqual(prepare_uploads([upload('song.mp3', tagged, 'audio/mpeg')], 0, valid_signature)[0][1], 'audio/mpeg')
        unicode_text = ('é' * 40000).encode()
        prepare_uploads([upload('text.md', unicode_text, 'application/octet-stream')], 0, valid_signature)

    def test_file_and_total_boundaries_and_stored_usage(self):
        self.limits()
        exact = lambda: upload('text.txt', b'a' * MIB, 'text/plain')
        prepare_uploads([exact(), exact()], 0, valid_signature)
        with self.assertRaisesMessage(ValidationError, 'per-file limit'):
            prepare_uploads([upload('large.txt', b'a' * (MIB + 1), 'text/plain')], 0, valid_signature)
        with self.assertRaisesMessage(ValidationError, 'card limit'):
            prepare_uploads([exact(), exact()], 1, valid_signature)
        card = self.submitted()
        add_card_attachments(card, [exact()], self.patient)
        with self.assertRaisesMessage(ValidationError, 'card limit'):
            add_card_attachments(card, [exact()], self.patient)
        self.limits(file=1, card=1)
        with self.assertRaises(ValidationError):
            add_card_attachments(card, [upload()], self.patient)
        self.client.force_login(self.patient)
        response = self.client.get(reverse('journal:detail', args=[card.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-stored-bytes="' + str(MIB + len(b'safe context')) + '"')

    def test_settings_permissions_validation_and_audit(self):
        url = reverse('control:attachment_limits')
        for user in (self.patient, self.reviewer):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, {'file_limit_mib': 50, 'card_limit_mib': 200}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        for values in [(0, 100), (-1, 100), (100, 10), ('1.5', 100)]:
            response = self.client.post(url, dict(zip(('file_limit_mib', 'card_limit_mib'), values)))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(AttachmentSettings.current().file_limit_mib, 100)
        self.assertRedirects(self.client.post(url, {'file_limit_mib': 50, 'card_limit_mib': 200}), reverse('control:dashboard') + '#maintenance')
        event = AuditEvent.objects.get(action='attachment_limits.updated')
        self.assertEqual(event.metadata['old']['card_limit_mib'], 500)
        self.assertEqual(event.metadata['new']['card_limit_mib'], 200)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(url, {'file_limit_mib': 1, 'card_limit_mib': 2}).status_code, 403)

    def test_stale_form_limits_are_checked_at_submission(self):
        self.client.force_login(self.patient)
        card = self.submitted()
        self.assertContains(self.client.get(reverse('journal:detail', args=[card.id])), '100 MiB per file')
        self.limits()
        with self.assertRaises(ValidationError):
            add_card_attachments(card, [upload('large.txt', b'a' * (MIB + 1), 'text/plain')], self.patient)
        self.assertEqual(card.attachments.count(), 1)

    def test_streamed_write_checksum_and_failed_batch_cleanup(self):
        card = self.submitted()
        original = set(card_directory(card).iterdir())
        real = atomic_write_upload
        calls = []
        def fail_second(path, item):
            calls.append(path)
            if len(calls) == 2: raise OSError('disk full')
            return real(path, item)
        with patch('core.services.atomic_write_upload', side_effect=fail_second), self.assertRaises(OSError):
            add_card_attachments(card, [upload(), upload('second.mp4')], self.patient)
        self.assertEqual(set(card_directory(card).iterdir()), original)
        self.assertEqual(card.attachments.count(), 1)
        item = upload('chunk.txt', b'abcd' * 20000, 'text/plain')
        with patch.object(item, 'chunks', return_value=iter([b'abcd' * 10000, b'abcd' * 10000])):
            target = Path(self.temp.name) / 'chunked.txt'
            size, checksum = atomic_write_upload(target, item)
        self.assertEqual(size, 80000)
        self.assertEqual(checksum, hashlib.sha256(target.read_bytes()).hexdigest())

    def test_ranges_head_download_and_authorization(self):
        card = self.submitted()
        add_card_attachments(card, [upload()], self.patient)
        item = card.attachments.get(content_type='video/mp4')
        url = reverse('journal:attachment', args=[card.id, item.id]) + '?inline=1'
        self.client.force_login(self.patient)
        for value, expected in [('bytes=0-3', MP4[:4]), ('bytes=4-', MP4[4:]), ('bytes=-5', MP4[-5:]), ('bytes=0-9999', MP4)]:
            response = self.client.get(url, HTTP_RANGE=value)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b''.join(response.streaming_content), expected)
            self.assertEqual(int(response['Content-Length']), len(expected))
            self.assertEqual(response['Accept-Ranges'], 'bytes')
        for value in ('bytes=9999-', 'bytes=6-2', 'bytes=-0'):
            response = self.client.get(url, HTTP_RANGE=value)
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response['Content-Range'], f'bytes */{len(MP4)}')
        for value in ('broken', 'bytes=0-1,4-5'):
            response = self.client.get(url, HTTP_RANGE=value)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), MP4)
        response = self.client.get(url, HTTP_RANGE='bytes=0-1', HTTP_IF_RANGE='"old"')
        self.assertEqual(response.status_code, 200)
        response.close()
        head = self.client.head(url)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head['Content-Length']), len(MP4))
        self.assertEqual(head['Content-Type'], 'video/mp4')
        download = self.client.get(url.split('?')[0])
        self.assertIn('attachment;', download['Content-Disposition'])
        download.close()
        self.assertTrue(AuditEvent.objects.filter(actor=self.patient, action='attachment.downloaded', target_id=str(card.id)).exists())
        for user in (self.admin, User.objects.create_user(username='stranger', role=User.Role.PATIENT)):
            self.client.force_login(user)
            self.assertIn(self.client.get(url, HTTP_RANGE='bytes=0-3').status_code, (403, 404))
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('review:attachment', args=[card.id, item.id]) + '?inline=1', HTTP_RANGE='bytes=0-3')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b''.join(response.streaming_content), MP4[:4])
