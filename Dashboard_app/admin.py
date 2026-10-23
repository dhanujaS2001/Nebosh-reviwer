from django.contrib import admin
from django.utils.html import format_html

from .models import BotState, Batch, Document, ResultUpload


class DocumentInline(admin.TabularInline):
    model = Document
    extra = 0
    can_delete = False
    fields = ("file_name", "file_format", "status", "similarity_pct", "ai_pct",
              "similarity_report", "ai_report")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class ResultUploadInline(admin.TabularInline):
    model = ResultUpload
    extra = 0
    can_delete = False
    fields = ("received_at", "source", "uploaded_by", "original_name", "matched_count")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("batch_id", "original_name", "total_files", "uploaded_at",
                    "stage_badge", "confirmed_by", "confirmed_at",
                    "results_source", "results_received_at", "send_attempts")
    list_filter = ("results_source", "uploaded_at")
    search_fields = ("batch_id", "original_name", "confirmed_by")
    date_hierarchy = "uploaded_at"
    ordering = ("-uploaded_at",)
    readonly_fields = ("batch_id", "uploaded_at", "initial_message_id", "reminder_message_id",
                       "last_error", "stage")
    inlines = [ResultUploadInline, DocumentInline]

    @admin.display(description="Stage")
    def stage_badge(self, obj):
        colours = {"queued": "#5b6785", "sent": "#e98a00",
                   "confirmed": "#0b5bf5", "results_received": "#1f9d55"}
        return format_html('<b style="color:{}">{}</b>',
                           colours.get(obj.stage, "#000"), obj.stage.replace("_", " "))


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("file_name", "batch", "file_format", "status",
                    "similarity_pct", "ai_pct", "ai_below_threshold", "size_mb")
    list_filter = ("status", "file_format", "ai_below_threshold", "batch")
    search_fields = ("file_name", "batch__batch_id")
    readonly_fields = ("stem",)
    list_select_related = ("batch",)

    @admin.display(description="Size")
    def size_mb(self, obj):
        return f"{obj.size_bytes / 1024 ** 2:.1f} MB"


@admin.register(ResultUpload)
class ResultUploadAdmin(admin.ModelAdmin):
    """Audit log of every results ZIP, from the website or Telegram."""
    list_display = ("received_at", "batch", "source", "uploaded_by",
                    "original_name", "matched_count", "unmatched_count")
    list_filter = ("source", "received_at")
    search_fields = ("batch__batch_id", "original_name", "uploaded_by")
    date_hierarchy = "received_at"
    list_select_related = ("batch",)
    readonly_fields = [f.name for f in ResultUpload._meta.fields]

    @admin.display(description="Not matched")
    def unmatched_count(self, obj):
        return len(obj.unmatched or [])

    def has_add_permission(self, request):
        return False


@admin.register(BotState)
class BotStateAdmin(admin.ModelAdmin):
    list_display = ("key", "value")