from django.urls import path
from . import views
app_name = "review"
urlpatterns = [path("", views.review_list, name="list"), path("trends/", views.review_trends, name="trends"), path("cards/<uuid:card_id>/", views.card_detail, name="detail"), path("cards/<uuid:card_id>/organize/", views.review_metadata, name="metadata"), path("cards/<uuid:card_id>/attachments/<int:attachment_id>/", views.attachment_download, name="attachment")]

