"""Daily recording windows and archive presentation, without scheduled jobs."""
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from calendar import Calendar

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.urls import reverse
from django.utils import timezone

from .models import Card, RecordingSchedule


def opening_time(day=None):
    day = day or timezone.localdate()
    schedule = RecordingSchedule.objects.filter(effective_date__lte=day).order_by('-effective_date').first()
    return schedule.opens_at if schedule else time(19)


def phase(day, now=None):
    local = timezone.localtime(now or timezone.now())
    if day != local.date():
        return 'closed'
    return 'early' if local.time().replace(tzinfo=None) < opening_time(day) else 'reflection'


def require_phase(card, expected):
    if card.status != Card.Status.DRAFT or phase(card.local_date) != expected:
        raise ValidationError('This recording window has closed. Your unsaved input has not been recorded. Open Today to continue.')


def boundary_context(day):
    current = phase(day)
    boundary = datetime.combine(day, opening_time(day)) if current == 'early' else datetime.combine(day + timedelta(days=1), time())
    return {'recording_phase': current, 'opening_time': opening_time(day),
            'recording_timezone': timezone.get_current_timezone_name(),
            'recording_boundary': timezone.make_aware(boundary).isoformat()}


def meaningful_draft(data):
    return bool(data.get('emotions') or any(item.get('value') is not False and item.get('value') not in ('', None, [], {}) for item in data.get('custom', [])))


def archive_state(card, day):
    if card and card.status == Card.Status.SUBMITTED:
        return 'complete'
    if day == timezone.localdate():
        return 'in progress'
    if card and (card.quick_notes.exists() or card.attachments.exists() or meaningful_draft(card.draft_data)):
        return 'incomplete'
    return 'missing'


def archive_context(request, patient, cards, catalog, reviewer=False):
    """Fill absent dates virtually; no empty card rows or disk folders are created."""
    today = timezone.localdate()
    joined = timezone.localtime(patient.date_joined).date() if patient else today
    # Preserve pre-existing imported records even when they predate account creation.
    first = min([joined] + [card.local_date for card in cards])
    by_date = {card.local_date: card for card in cards}
    def entry(day):
        if day < first or day > today:
            return None
        card = by_date.get(day)
        if day < joined and not card:
            return None
        if card and card.status == Card.Status.QUARANTINED:
            return None
        state = archive_state(card, day)
        if not card:
            card = SimpleNamespace(id=None, local_date=day, status='draft', content_index={}, unread_comment_count=0, quick_notes=SimpleNamespace(count=0), attachments=SimpleNamespace(count=0), addenda=SimpleNamespace(count=0), therapist_comments=SimpleNamespace(count=0))
        card.archive_state = state
        card.display_emotions = [{**item, 'face': getattr(catalog.get(item.get('id')), 'face', '◆'), 'color': getattr(catalog.get(item.get('id')), 'color', '#79ffe1')} for item in card.content_index.get('emotions', [])] if state == 'complete' else []
        card.prominent_emotion = max(card.display_emotions, key=lambda item: int(item.get('intensity') or 1), default=None)
        card.is_read = getattr(card, 'is_read', True) if state == 'complete' else True
        if state == 'complete':
            card.archive_url = reverse('review:detail' if reviewer else 'journal:detail', args=[card.pk])
        elif reviewer:
            card.archive_url = reverse('review:day', args=[patient.id, day.isoformat()])
        elif state == 'in progress':
            card.archive_url = reverse('journal:today')
        else:
            card.archive_url = reverse('journal:day', args=[day.isoformat()])
        return card
    try:
        month_start = datetime.strptime(request.GET.get('month', ''), '%Y-%m').date().replace(day=1)
        if not 2 <= month_start.year <= 9998: raise ValueError()
    except ValueError:
        month_start = today.replace(day=1)
    days = sorted({card.local_date for card in cards if card.local_date < joined} | {joined + timedelta(days=i) for i in range(max(0, (today-joined).days + 1))}) if patient else []
    if request.GET.get('sort') != 'oldest': days.reverse()
    # Filter by actual unread submitted records, not missing calendar days.
    if reviewer and request.GET.get('status') == 'unread':
        days = [day for day in days if day in by_date and by_date[day].status == Card.Status.SUBMITTED and not by_date[day].is_read]
    days = [day for day in days if not (day in by_date and by_date[day].status == Card.Status.QUARANTINED)]
    page = Paginator(days, 31).get_page(request.GET.get('page'))
    rows = [entry(day) for day in page.object_list]
    weeks = [[{'date': day, 'in_month': day.month == month_start.month, 'is_today': day == today,
               'card': entry(day) if patient else None} for day in week] for week in Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month)]
    return {'cards': rows, 'archive_page': page, 'calendar_weeks': weeks, 'month_start': month_start,
            'previous_month': (month_start-timedelta(days=1)).strftime('%Y-%m'),
            'next_month': (month_start.replace(day=28)+timedelta(days=4)).replace(day=1).strftime('%Y-%m'), 'filters': request.GET}
