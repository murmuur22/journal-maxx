from django.urls import path
from . import views
app_name = "control"
urlpatterns = [path("", views.control_dashboard, name="dashboard"), path("invites/", views.control_invite, name="invite"), path("emotions/<int:emotion_id>/", views.control_emotion, name="emotion"), path("form-fields/", views.control_form_field, name="form_field"), path("cards/<uuid:card_id>/quarantine/", views.control_quarantine, name="quarantine"), path("cards/<uuid:card_id>/restore/", views.control_restore, name="restore")]
