#!/bin/bash
# MTG Card Scanner - Backup Script
# Creates a backup of user data (inventory, database, scanned images)

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
SCANNER_DIR="/home/pi/scanner"
BACKUP_DIR="$HOME/scanner-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="scanner-backup-$TIMESTAMP.tar.gz"
KEEP_BACKUPS=10  # Number of backups to keep

# Print messages
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Change to scanner directory
if [ ! -d "$SCANNER_DIR" ]; then
    echo "Error: Scanner directory not found: $SCANNER_DIR"
    exit 1
fi

cd "$SCANNER_DIR"

# Create backup directory
mkdir -p "$BACKUP_DIR"

print_info "Creating backup..."

# Create backup archive
tar -czf "$BACKUP_DIR/$BACKUP_FILE" \
    --exclude='data/logs' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    data/ \
    scanned_cards/ \
    .env 2>/dev/null || true

# Get backup size
BACKUP_SIZE=$(du -h "$BACKUP_DIR/$BACKUP_FILE" | cut -f1)

print_info "Backup created: $BACKUP_DIR/$BACKUP_FILE"
print_info "Backup size: $BACKUP_SIZE"

# List backup contents
print_info "Backup contains:"
tar -tzf "$BACKUP_DIR/$BACKUP_FILE" | head -10
TOTAL_FILES=$(tar -tzf "$BACKUP_DIR/$BACKUP_FILE" | wc -l)
echo "  ... ($TOTAL_FILES files total)"

# Clean up old backups
cd "$BACKUP_DIR"
BACKUP_COUNT=$(ls -1 scanner-backup-*.tar.gz 2>/dev/null | wc -l)

if [ "$BACKUP_COUNT" -gt "$KEEP_BACKUPS" ]; then
    print_info "Cleaning up old backups (keeping $KEEP_BACKUPS most recent)..."
    ls -t scanner-backup-*.tar.gz | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm
    REMOVED=$((BACKUP_COUNT - KEEP_BACKUPS))
    print_info "Removed $REMOVED old backup(s)"
fi

# Display current backups
echo ""
print_info "Current backups:"
ls -lth scanner-backup-*.tar.gz 2>/dev/null | head -5 || echo "  No backups found"

# Calculate total backup size
TOTAL_SIZE=$(du -sh . | cut -f1)
print_info "Total backup directory size: $TOTAL_SIZE"

echo ""
print_info "Backup complete!"
echo ""
echo "To restore this backup on another system:"
echo "  tar -xzf $BACKUP_DIR/$BACKUP_FILE -C /home/pi/scanner/"
echo ""
