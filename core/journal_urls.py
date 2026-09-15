from django.urls import path
from . import views
app_name = "journal"
urlpatterns = [path("days/<str:day>/", views.archive_day, name="day"), path("early/save/", views.journal_early_save, name="early_save"), path("", views.journal_today, name="today"), path("history/", views.journal_history, name="history"), path("trends/", views.journal_trends, name="trends"), path("cards/<uuid:card_id>/", views.card_detail, name="detail"), path("cards/<uuid:card_id>/addendum/", views.card_addendum, name="addendum"), path("cards/<uuid:card_id>/attachments/add/", views.card_attachment_add, name="attachment_add"), path("cards/<uuid:card_id>/attachments/<int:attachment_id>/", views.attachment_download, name="attachment")]
