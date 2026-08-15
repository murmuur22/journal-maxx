from django.urls import path
from . import api
urlpatterns = [path("cards", api.cards), path("cards/<uuid:card_id>", api.card), path("ready", api.readiness), path("schema", api.schema)]

