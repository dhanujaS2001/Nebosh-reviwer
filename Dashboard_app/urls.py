# Merge with your existing urls.py (keep your own prefixes / app_name if you have them).
from django.urls import path

from . import views

urlpatterns = [
    path("", views.DashboardView, name="dashboard"),
    path("api/upload-for-scanning/", views.upload_for_scanning, name="upload_for_scanning"),
    path("api/upload-results/", views.upload_results, name="upload_results"),
    path("api/batch/", views.batch_state, name="batch_state"),
    path("reports/<int:pk>/<str:kind>/", views.download_report, name="download_report"),
    path("batch/<str:batch_id>/download/", views.download_batch, name="download_batch"),
    path("telegram/tick/<str:secret>/", views.telegram_tick, name="telegram_tick"),
    path("clear-batch/", views.clear_batch, name="clear_batch"),
]