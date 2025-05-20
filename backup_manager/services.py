import hashlib
from django.utils import timezone
import magic
import logging
import os
import shutil
from django.db import models
from pathlib import Path

from django.conf import settings
from django.db import transaction

from .models import BackupFileLog, BackupJob, UniqueFileBackup

logger = logging.getLogger(__name__)


# -- Constants --
BACKUP_SUBDIR = "backup"

# -- Helper Functions --


def get_backup_storage_dir():
    """Gets the absolute path to the backup storage directory, creating it if necessary."""
    try:
        base_dir = os.path.join(settings.MEDIA_ROOT, BACKUP_SUBDIR)
        os.makedirs(base_dir, exist_ok=True)
        return base_dir
    except AttributeError:
        logger.error(
            "settings.MEDIA_ROOT is not defined. Cannot determine backup directory."
        )
        raise ValueError("settings.MEDIA_ROOT is not defined.")  # Or handle differently
    except Exception as e:
        logger.error(f"Error creating backup storage directory '{base_dir}': {e}")
        raise  # Re-raise after logging


def calculate_file_sha256(file_path: str) -> str | None:
    """Calculates the SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(8192), b""):  # Read in 8KB blocks
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        logger.error(f"Hash calculation error: File not found at {file_path}")
        return None
    except Exception as e:
        logger.error(f"Error calculating hash for {file_path}: {e}", exc_info=True)
        return None


# -- Main Services --
def start_backup_job(
    source_description: str = "", total_files_intended: int = 0
) -> BackupJob:
    """
    Creates and returns a new BackupJob instance.
    """
    job = BackupJob.objects.create(
        source_description=source_description,
        total_files_intended=total_files_intended,
        status=BackupJob.JobStatus.IN_PROGRESS,  # Start as IN_PROGRESS
    )
    logger.info(f"Started Backup Job: {job.job_id} for source: {source_description}")
    return job


def process_file_for_backup(
    job: BackupJob, original_file_path: str
) -> BackupFileLog | None:
    """
    Processes a single file for backup as part of a given BackupJob.
    - Calculates hash for deduplication.
    - Copies file to backup storage if it's new content.
    - Creates/updates relevant database records.
    """
    original_filename = Path(original_file_path).name
    log_entry_status = BackupFileLog.ProcessStatus.FAILED  # Default to failure
    log_notes = ""
    unique_file_instance = None
    is_newly_backed_up = False

    if not os.path.exists(original_file_path) or not os.path.isfile(original_file_path):
        log_notes = f"Source file not found or is not a file: {original_file_path}"
        logger.warning(log_notes)
        # Create a log entry even for file not found, to track it within the job
        return BackupFileLog.objects.create(
            job=job,
            original_filename=original_filename,
            original_path=original_file_path,
            status_in_job=BackupFileLog.ProcessStatus.FAILED_COPY,  # Or a new status like FAILED_NOT_FOUND
            notes_for_job=log_notes,
        )

    file_hash = calculate_file_sha256(original_file_path)
    if not file_hash:
        log_notes = f"Could not calculate hash for '{original_filename}'."
        log_entry_status = BackupFileLog.ProcessStatus.FAILED_HASH
        # Create log entry and return
        return BackupFileLog.objects.create(
            job=job,
            original_filename=original_filename,
            original_path=original_file_path,
            status_in_job=log_entry_status,
            notes_for_job=log_notes,
        )

    try:
        with transaction.atomic():
            # Check if unique file content already exists
            unique_file_instance, created_new_unique_file = (
                UniqueFileBackup.objects.get_or_create(
                    file_hash=file_hash,
                    defaults={
                        # These defaults are only used if a new UniqueFileBackup is created
                        "stored_filename": f"{file_hash}{Path(original_filename).suffix.lower() or '.dat'}",  # Store with hash + original ext
                        "filesize": os.path.getsize(original_file_path),
                        "mimetype": magic.from_file(original_file_path, mime=True),
                    },
                )
            )

            if created_new_unique_file:
                # This is new content, so copy the file to backup storage
                backup_storage_dir = get_backup_storage_dir()
                storage_target_path = os.path.join(
                    backup_storage_dir, unique_file_instance.stored_filename
                )

                # Ensure target directory for stored_filename exists if it includes subdirs
                os.makedirs(os.path.dirname(storage_target_path), exist_ok=True)

                shutil.copy2(original_file_path, storage_target_path)
                logger.info(
                    f"New unique content: Copied '{original_filename}' to '{storage_target_path}' for job {job.job_id}"
                )
                log_entry_status = BackupFileLog.ProcessStatus.SUCCESS_NEW
                is_newly_backed_up = True
            else:
                # Content already exists in backup store
                logger.info(
                    f"Existing unique content: '{original_filename}' (hash: {file_hash}) already backed up as '{unique_file_instance.stored_filename}' for job {job.job_id}."
                )
                log_entry_status = BackupFileLog.ProcessStatus.SUCCESS_EXISTING
                # Optionally update last_seen_at on UniqueFileBackup if desired, though BackupFileLog tracks occurrences.

            # Create the log entry for this file in this job
            file_log = BackupFileLog.objects.create(
                job=job,
                unique_file=unique_file_instance,
                original_filename=original_filename,
                original_path=original_file_path,
                status_in_job=log_entry_status,
                notes_for_job=log_notes if log_notes else "Processed successfully.",
            )

            # Update job counters
            job.files_processed_successfully = (
                models.F("files_processed_successfully") + 1
            )
            if is_newly_backed_up:
                job.new_files_backed_up = models.F("new_files_backed_up") + 1
            job.save(
                update_fields=["files_processed_successfully", "new_files_backed_up"]
            )

            return file_log

    except Exception as e:
        log_notes = (
            f"Error processing file '{original_filename}' for job {job.job_id}: {e}"
        )
        logger.error(log_notes, exc_info=True)
        # Attempt to create a failure log entry
        try:
            return BackupFileLog.objects.create(
                job=job,
                unique_file=unique_file_instance,  # Might be None if error was before get_or_create
                original_filename=original_filename,
                original_path=original_file_path,
                status_in_job=BackupFileLog.ProcessStatus.FAILED_DB
                if unique_file_instance
                else BackupFileLog.ProcessStatus.FAILED_COPY,
                notes_for_job=log_notes,
            )
        except Exception as log_e:
            logger.error(
                f"Critical error: Could not even create failure log for '{original_filename}' in job {job.job_id}: {log_e}",
                exc_info=True,
            )
            # At this point, the job's failure count might need manual adjustment or a different tracking mechanism
            # For simplicity, we don't update job.files_failed here to avoid another DB call in an error state.
            # This can be handled by the finalize_backup_job function.
            return None  # Indicate total failure to log this file instance


def finalize_backup_job(job: BackupJob):
    """
    Finalizes a backup job, updating its status and end time.
    """
    # Refresh job object from DB to get latest counts
    job.refresh_from_db()

    # Determine overall job status based on counts
    if job.files_failed > 0 and job.files_processed_successfully > 0:
        job.status = BackupJob.JobStatus.PARTIALLY_COMPLETED
    elif job.files_failed > 0 and job.files_processed_successfully == 0:
        # If all intended files failed (and total_files_intended was set)
        if (
            job.total_files_intended > 0
            and job.files_failed == job.total_files_intended
        ):
            job.status = BackupJob.JobStatus.FAILED
        # If some files were processed (e.g. skipped) but others failed
        elif job.files_failed > 0:
            job.status = (
                BackupJob.JobStatus.PARTIALLY_COMPLETED
            )  # Or FAILED if no successes at all
        else:  # Default to FAILED if only failures are recorded
            job.status = BackupJob.JobStatus.FAILED

    elif (
        job.files_failed == 0
        and job.total_files_intended > 0
        and job.files_processed_successfully == job.total_files_intended
    ):
        job.status = BackupJob.JobStatus.COMPLETED
    elif (
        job.files_failed == 0
        and job.files_processed_successfully > 0
        and job.total_files_intended == 0
    ):
        # Job completed but total_files_intended was not set, assume all processed were intended
        job.status = BackupJob.JobStatus.COMPLETED
    else:
        # If job is still marked IN_PROGRESS but no more files are coming,
        # and it's not clearly completed or failed based on counts.
        # This might indicate an incomplete job or an issue with tracking total_files_intended.
        # For now, if there are successes and no failures, mark as completed.
        if job.files_processed_successfully > 0 and job.files_failed == 0:
            job.status = BackupJob.JobStatus.COMPLETED
        elif (
            job.status == BackupJob.JobStatus.IN_PROGRESS
        ):  # If still in progress and counts don't make it complete/failed
            logger.warning(
                f"Backup Job {job.job_id} finalized with status IN_PROGRESS and unclear completion state based on counts. Review manually."
            )
            job.status = (
                BackupJob.JobStatus.PARTIALLY_COMPLETED
            )  # Or some other review state

    job.end_time = timezone.now()
    job.save()
    logger.info(
        f"Finalized Backup Job: {job.job_id} with status {job.get_status_display()}"  # type: ignore
    )
