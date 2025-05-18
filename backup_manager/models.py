import uuid
from django.db import models
from django.utils import timezone
import logging

# Create your models here.
logger = logging.getLogger(__name__)


class BackupJob(models.Model):
    """
    Represents a single backup operation or session.
    """

    class JobStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"
        PARTIALLY_COMPLETED = (
            "PARTIALLY_COMPLETED",
            "Partially Completed",
        )  # Some files failed
        FAILED = "FAILED", "Failed"

    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=JobStatus.choices, default=JobStatus.PENDING
    )
    source_description = models.TextField(
        blank=True,
        null=True,
        help_text="Description of the source of this backup job (e.g., 'Nightly scan of /invoices', 'Manual upload by user X').",
    )
    total_files_intended = models.PositiveIntegerField(
        default=0,
        help_text="Total number of files intended to be processed in this job.",
    )
    files_processed_successfully = models.PositiveIntegerField(
        default=0,
        help_text="Number of files successfully processed (either newly backed up or found as existing).",
    )
    new_files_backed_up = models.PositiveIntegerField(
        default=0,
        help_text="Number of unique file contents newly added to the backup store during this job.",
    )
    files_failed = models.PositiveIntegerField(
        default=0, help_text="Number of files that failed to process during this job."
    )
    notes = models.TextField(
        blank=True, null=True, help_text="Additional notes or summary for this job."
    )

    def __str__(self):
        return f"Backup Job {self.job_id} - Started: {self.start_time.strftime('%Y-%m-%d %H:%M')}"

    class Meta:
        ordering = ["-start_time"]
        verbose_name = "Backup Job"
        verbose_name_plural = "Backup Jobs"
