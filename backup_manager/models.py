import logging
import os
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

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

    def update_job_completion_status(self):
        """Updates the job status based on file processing counts."""
        if self.files_failed > 0 and self.files_processed_successfully > 0:
            self.status = BackupJob.JobStatus.PARTIALLY_COMPLETED
        elif self.files_failed > 0 and self.files_processed_successfully == 0:
            self.status = BackupJob.JobStatus.FAILED
        elif (
            self.files_failed == 0
            and self.files_processed_successfully == self.total_files_intended
        ):
            self.status = BackupJob.JobStatus.COMPLETED
        else:
            # Could be still IN_PROGRESS if called mid-way, or if counts don't align (e.g. total_files_intended not set properly)
            # For simplicity, if no failures and not all processed, it might still be in progress or needs review.
            # A more robust state machine might be needed for complex scenarios.
            pass  # Keep current status or re-evaluate based on more detailed logic
        self.end_time = timezone.now()
        self.save()


class UniqueFileBackup(models.Model):
    """
    Represents a unique piece of file content that has been backed up.
    Deduplicated by file_hash.
    """

    file_hash = models.CharField(
        max_length=64,  # SHA256 hash is 64 hex characters
        unique=True,
        db_index=True,
        primary_key=True,  # Making hash the PK simplifies lookups and ensures uniqueness at DB level
        help_text="SHA256 hash of the file content, used for deduplication.",
    )
    # Name of the file as stored in the backup location (e.g., <hash>.pdf or <hash>.dat)
    # The actual extension might be useful to store if not part of the backup_filename.
    stored_filename = models.CharField(
        max_length=100,  # e.g., "hash_value.pdf" or "hash_value.dat"
        help_text="Filename (typically hash + original extension) as stored in the backup location.",
    )
    # Example: if backup_dir is /media/deduplicated_backups/
    # and you store files in subdirs like /media/deduplicated_backups/ab/cd/abcdef123.pdf
    # then stored_filename could be "ab/cd/abcdef123.pdf"
    # Or, if all in one dir, just "abcdef123.pdf"

    filesize = models.PositiveIntegerField(help_text="Size of the file in bytes.")
    mimetype = models.CharField(
        max_length=100,
        default="application/octet-stream",
        help_text="MIME type of the file.",
    )

    first_backed_up_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when this unique file content was first stored.",
    )

    def __str__(self):
        return f"Unique File (Hash: {self.file_hash[:10]}..., Stored: {self.stored_filename})"

    def get_full_storage_path(self):
        """Returns the absolute path to the stored backup file."""
        try:
            backup_base_dir = os.path.join(settings.MEDIA_ROOT, "deduplicated_backups")
            return os.path.join(backup_base_dir, self.stored_filename)
        except AttributeError:
            logger.error("settings.MEDIA_ROOT is not defined.")
            return None

    class Meta:
        ordering = ["-first_backed_up_at"]
        verbose_name = "Unique File Backup"
        verbose_name_plural = "Unique File Backups"


class BackupFileLog(models.Model):
    """
    Logs each instance of a file being processed as part of a BackupJob,
    linking to the UniqueFileBackup record for its content.
    """

    class ProcessStatus(models.TextChoices):
        SUCCESS_NEW = "NEW", "Newly Backed Up"
        SUCCESS_EXISTING = "EXISTING", "Already Backed Up (Existing)"
        FAILED_HASH = "FAIL_HASH", "Failed (Hashing Error)"
        FAILED_COPY = "FAIL_COPY", "Failed (File Copy Error)"
        FAILED_DB = "FAIL_DB", "Failed (Database Error)"
        SKIPPED = "SKIPPED", "Skipped"  # e.g., if file was 0 bytes or not a PDF

    log_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        BackupJob, on_delete=models.CASCADE, related_name="file_logs"
    )
    unique_file = models.ForeignKey(
        UniqueFileBackup,
        on_delete=models.PROTECT,  # Don't delete unique file if logs exist, or use SET_NULL if appropriate
        null=True,
        blank=True,  # Null if processing failed before unique file was identified/created
        related_name="job_logs",
    )
    original_filename = models.CharField(
        max_length=255,
        help_text="Original filename as encountered in this specific job.",
    )
    original_path = models.TextField(
        null=True,
        blank=True,
        help_text="Original full path of the file as encountered in this job.",
    )
    processed_at = models.DateTimeField(auto_now_add=True)
    status_in_job = models.CharField(max_length=20, choices=ProcessStatus.choices)
    notes_for_job = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"Log: {self.original_filename} in Job {self.job.job_id} ({self.get_status_in_job_display()})"

    class Meta:
        ordering = ["-processed_at"]
        verbose_name = "Backup File Log"
        verbose_name_plural = "Backup File Logs"
