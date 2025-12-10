#!/usr/bin/env python3
"""
Database Setup Script
Downloads and populates the card database from Scryfall
"""

import sys
from pathlib import Path

# Add parent directory to path to import modules
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from database import CardDatabase


def progress_callback(message):
    """Print progress messages"""
    print(message)


def main():
    """Setup the card database"""
    print("="*60)
    print("Card Database Setup - Scryfall Data Download")
    print("="*60)
    print("\nThis will download ~150MB of Magic: The Gathering card data")
    print("from Scryfall. This may take 5-10 minutes depending on your")
    print("internet connection.\n")
    
    # Create directories
    print("Creating directories...")
    Config.create_directories()
    print("✓ Directories created")
    
    # Initialize database
    print("\nInitializing database...")
    db = CardDatabase()
    
    # Check if database exists and has data
    stats = db.get_database_stats()
    
    if stats['total_cards'] > 0:
        print(f"\n{'='*60}")
        print("Existing database found:")
        print('='*60)
        print(f"  Total cards: {stats['total_cards']:,}")
        print(f"  Cards with prices: {stats['cards_with_prices']:,}")
        print(f"  Location: {Config.DATABASE_FILE}")
        print('='*60)
        
        update = input("\nUpdate database with latest data? (y/N): ").strip().lower()
        if update != 'y':
            db.close()
            print("\nSetup cancelled. Using existing database.")
            return
    
    try:
        print("\n" + "="*60)
        print("Downloading Scryfall Card Data")
        print("="*60)
        
        # Download and populate
        cards_data = db.download_scryfall_data(progress_callback)
        
        print("\n" + "="*60)
        print("Populating Database")
        print("="*60)
        
        inserted = db.populate_database(cards_data, progress_callback)
        
        # Show final stats
        stats = db.get_database_stats()
        print(f"\n{'='*60}")
        print("✓ Database Setup Complete!")
        print('='*60)
        print(f"  Total cards: {stats['total_cards']:,}")
        print(f"  Cards with prices: {stats['cards_with_prices']:,}")
        print(f"  Database location: {Config.DATABASE_FILE}")
        print(f"  Database size: {Config.DATABASE_FILE.stat().st_size / (1024*1024):.1f} MB")
        print('='*60)
        print("\n✓ You can now run the scanner with: python3 app.py")
        
    except KeyboardInterrupt:
        print("\n\n⚠ Setup interrupted by user")
        print("You can run this script again to complete the setup.")
    except Exception as e:
        print(f"\n{'='*60}")
        print("✗ Error Setting Up Database")
        print('='*60)
        print(f"Error: {e}")
        print("\nTroubleshooting:")
        print("1. Check your internet connection")
        print("2. Make sure you have enough disk space (~150MB)")
        print("3. Try running the script again")
        print("4. Check if Scryfall.com is accessible")
    finally:
        db.close()


if __name__ == "__main__":
    main()
