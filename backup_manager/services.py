import logging
from .models import BackupJob

logger = logging.getLogger(__name__)


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
