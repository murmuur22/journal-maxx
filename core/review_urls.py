from django.urls import path
from . import views
app_name = "review"
urlpatterns = [path("patients/<int:patient_id>/days/<str:day>/", views.archive_day, name="day"), path("", views.review_list, name="list"), path("patient/", views.review_patient_select, name="patient_select"), path("trends/", views.review_trends, name="trends"), path("cards/<uuid:card_id>/", views.card_detail, name="detail"), path("cards/<uuid:card_id>/organize/", views.review_metadata, name="metadata"), path("cards/<uuid:card_id>/comments/", views.review_comment, name="comment"), path("cards/<uuid:card_id>/attachments/<int:attachment_id>/", views.attachment_download, name="attachment")]
