from django.db import models


class Batch(models.Model):
    batch_id = models.CharField(max_length=12, unique=True)
    original_name = models.CharField(max_length=255)
    zip_path = models.CharField(max_length=500)          # relative to MEDIA_ROOT
    total_files = models.PositiveIntegerField()
    by_type = models.JSONField(default=dict)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # Telegram state
    initial_sent_at = models.DateTimeField(null=True, blank=True)
    initial_message_id = models.BigIntegerField(null=True, blank=True)
    send_attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    reminder_count = models.PositiveIntegerField(default=0)
    reminder_message_id = models.BigIntegerField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.CharField(max_length=150, blank=True)

    # Results state
    results_received_at = models.DateTimeField(null=True, blank=True)
    results_source = models.CharField(max_length=20, blank=True)   # "web" | "telegram"

    @property
    def stage(self):
        if self.results_received_at:
            return "results_received"
        if self.confirmed_at:
            return "confirmed"
        if self.initial_sent_at:
            return "sent"
        return "queued"

    def __str__(self):
        return f"{self.batch_id} ({self.original_name})"


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"

    batch = models.ForeignKey(Batch, related_name="documents", on_delete=models.CASCADE)
    file_name = models.CharField(max_length=255)
    stem = models.CharField(max_length=255, db_index=True)   # lowercase, no extension
    file_format = models.CharField(max_length=10)
    size_bytes = models.BigIntegerField(default=0)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    similarity_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    ai_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    ai_below_threshold = models.BooleanField(default=False)   # Turnitin shows '*%' under 20%
    similarity_report = models.CharField(max_length=500, blank=True)   # relative to MEDIA_ROOT
    ai_report = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["id"]

    def refresh_status(self):
        n = bool(self.similarity_report) + bool(self.ai_report)
        self.status = (self.Status.COMPLETED if n == 2
                       else self.Status.IN_PROGRESS if n == 1
                       else self.Status.PENDING)

    def __str__(self):
        return self.file_name


class ResultUpload(models.Model):
    """Audit log: every results ZIP received, from either channel."""
    batch = models.ForeignKey(Batch, related_name="result_uploads", on_delete=models.CASCADE)
    source = models.CharField(max_length=20)               # "web" | "telegram"
    uploaded_by = models.CharField(max_length=150, blank=True)
    original_name = models.CharField(max_length=255)
    zip_path = models.CharField(max_length=500)
    received_at = models.DateTimeField(auto_now_add=True)
    matched_count = models.PositiveIntegerField(default=0)
    unmatched = models.JSONField(default=list)             # files we couldn't place

    def __str__(self):
        return f"{self.batch.batch_id} via {self.source} ({self.matched_count} matched)"


class BotState(models.Model):
    """Small key/value store (remembers the last Telegram update we processed)."""
    key = models.CharField(max_length=50, unique=True)
    value = models.CharField(max_length=200, default="")

    def __str__(self):
        return f"{self.key}={self.value}"