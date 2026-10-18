"""
Cleanup utility for scanned card images.
Automatically removes old scanned images to free up disk space.
"""

import logging
import stat
import time
from card_scanner.config import Config

logger = logging.getLogger('cleanup')  # the name its log lines always had (the module is card_scanner.core.cleanup)


def cleanup_old_images(days_to_keep=7, dry_run=False):
    """
    Delete scanned card images older than specified days.

    Args:
        days_to_keep (int): Keep images from the last N days (default: 7)
        dry_run (bool): If True, only log what would be deleted without actually deleting

    Returns:
        tuple: (files_deleted, space_freed_mb)
    """
    images_dir = Config.IMAGES_DIR

    if not images_dir.exists():
        logger.info(f"Images directory does not exist: {images_dir}")
        return 0, 0

    cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
    files_deleted = 0
    space_freed = 0

    # Get all image files
    image_extensions = ('.jpg', '.jpeg', '.png', '.bmp')
    image_files = [f for f in images_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in image_extensions]

    logger.info(f"Found {len(image_files)} image files in {images_dir}")

    for image_file in image_files:
        try:
            file_mtime = image_file.stat().st_mtime

            if file_mtime < cutoff_time:
                file_size = image_file.stat().st_size
                age_days = (time.time() - file_mtime) / (24 * 60 * 60)

                if dry_run:
                    logger.info(f"[DRY RUN] Would delete: {image_file.name} (age: {age_days:.1f} days, size: {file_size/1024:.1f} KB)")
                else:
                    image_file.unlink()
                    logger.debug(f"Deleted: {image_file.name} (age: {age_days:.1f} days)")

                files_deleted += 1
                space_freed += file_size

        except Exception as e:
            logger.error(f"Error processing {image_file.name}: {e}")

    space_freed_mb = space_freed / (1024 * 1024)

    if files_deleted > 0:
        action = "Would delete" if dry_run else "Deleted"
        logger.info(f"{action} {files_deleted} old image(s), freeing {space_freed_mb:.2f} MB")
    else:
        logger.info(f"No images older than {days_to_keep} days found")

    return files_deleted, space_freed_mb


def cleanup_all_images(dry_run=False):
    """
    Delete all scanned card images.

    Args:
        dry_run (bool): If True, only log what would be deleted without actually deleting

    Returns:
        tuple: (files_deleted, space_freed_mb)
    """
    images_dir = Config.IMAGES_DIR

    if not images_dir.exists():
        logger.info(f"Images directory does not exist: {images_dir}")
        return 0, 0

    files_deleted = 0
    space_freed = 0

    # Get all image files
    image_extensions = ('.jpg', '.jpeg', '.png', '.bmp')
    image_files = [f for f in images_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in image_extensions]

    logger.info(f"Found {len(image_files)} image files to clean up")

    for image_file in image_files:
        try:
            file_size = image_file.stat().st_size

            if dry_run:
                logger.info(f"[DRY RUN] Would delete: {image_file.name} (size: {file_size/1024:.1f} KB)")
            else:
                image_file.unlink()
                logger.debug(f"Deleted: {image_file.name}")

            files_deleted += 1
            space_freed += file_size

        except Exception as e:
            logger.error(f"Error deleting {image_file.name}: {e}")

    space_freed_mb = space_freed / (1024 * 1024)

    if files_deleted > 0:
        action = "Would delete" if dry_run else "Deleted"
        logger.info(f"{action} {files_deleted} image(s), freeing {space_freed_mb:.2f} MB")
    else:
        logger.info("No images found to clean up")

    return files_deleted, space_freed_mb


def get_images_stats():
    """
    Get statistics about scanned images.

    Returns:
        dict: Statistics including file count, total size, oldest file age
    """
    empty = {'count': 0, 'total_size_mb': 0, 'oldest_days': 0}
    image_extensions = ('.jpg', '.jpeg', '.png', '.bmp')
    count = 0
    total_size = 0
    oldest_time = None
    try:
        for f in Config.IMAGES_DIR.iterdir():
            if f.suffix.lower() not in image_extensions:
                continue
            try:
                # One look at each file: the clean-up (another thread) may delete it at any
                # moment, and a file that is gone is simply not counted
                info = f.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            count += 1
            total_size += info.st_size
            oldest_time = info.st_mtime if oldest_time is None else min(oldest_time, info.st_mtime)
    except OSError:
        return empty  # no folder (yet), or it cannot be read

    if not count:
        return empty

    return {
        'count': count,
        'total_size_mb': total_size / (1024 * 1024),
        'oldest_days': (time.time() - oldest_time) / (24 * 60 * 60)
    }


if __name__ == '__main__':
    # Command-line usage
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='Cleanup scanned card images')
    parser.add_argument('--days', type=int, default=7,
                        help='Delete images older than N days (default: 7)')
    parser.add_argument('--all', action='store_true',
                        help='Delete all images')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be deleted without actually deleting')
    parser.add_argument('--stats', action='store_true',
                        help='Show statistics about scanned images')

    args = parser.parse_args()

    if args.stats:
        stats = get_images_stats()
        print("\nScanned Images Statistics:")
        print(f"  Files: {stats['count']}")
        print(f"  Total size: {stats['total_size_mb']:.2f} MB")
        print(f"  Oldest file: {stats['oldest_days']:.1f} days old")
    elif args.all:
        cleanup_all_images(dry_run=args.dry_run)
    else:
        cleanup_old_images(days_to_keep=args.days, dry_run=args.dry_run)
