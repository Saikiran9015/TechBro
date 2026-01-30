"""
Database initialization and management script for KropKart
Creates collections and indexes for MongoDB
"""

from pymongo import MongoClient, ASCENDING, DESCENDING
from dotenv import load_dotenv
import os

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("ERROR: MONGO_URI not found in .env file")
    exit(1)

def init_database():
    """Initialize KropKart database with all collections and indexes"""
    
    client = MongoClient(MONGO_URI)
    db = client.KropKart
    
    print("=" * 50)
    print("KropKart Database Initialization")
    print("=" * 50)
    
    # Define collections and their indexes
    collections_config = {
        "users": [
            ("email", ASCENDING, {"unique": True}),
            ("user_type", ASCENDING),
            ("created_at", DESCENDING)
        ],
        "products": [
            ("category", ASCENDING),
            ("name", ASCENDING),
            ("price", ASCENDING)
        ],
        "orders": [
            ("user_email", ASCENDING),
            ("created_at", DESCENDING),
            ("status", ASCENDING)
        ],
        "categories": [
            ("name", ASCENDING)
        ],
        "shipments": [
            ("order_id", ASCENDING),
            ("status", ASCENDING)
        ],
        "admin": [
            ("email", ASCENDING, {"unique": True})
        ]
    }
    
    # Create collections and indexes
    for collection_name, indexes in collections_config.items():
        if collection_name not in db.list_collection_names():
            db.create_collection(collection_name)
            print(f"✓ Created collection: {collection_name}")
        else:
            print(f"✓ Collection exists: {collection_name}")
        
        # Create indexes
        collection = db[collection_name]
        for index_info in indexes:
            if len(index_info) == 2:
                field, direction = index_info
                collection.create_index([(field, direction)])
                print(f"  └─ Index: {field}")
            elif len(index_info) == 3:
                field, direction, options = index_info
                collection.create_index([(field, direction)], **options)
                print(f"  └─ Index: {field} (unique)")
    
    # Create sample admin user if it doesn't exist
    admin_count = db.admin.count_documents({})
    if admin_count == 0:
        print("\n📝 Creating sample admin user...")
        from werkzeug.security import generate_password_hash
        db.admin.insert_one({
            "email": "admin@kropkart.com",
            "password": generate_password_hash("admin123"),
            "name": "Admin",
            "created_at": "2025-01-01"
        })
        print("✓ Sample admin created: admin@kropkart.com / admin123")
    
    print("\n" + "=" * 50)
    print("Database initialization complete!")
    print("=" * 50)
    
    # Display database structure
    print("\nDatabase Structure:")
    print(f"Database: {db.name}")
    print("Collections:")
    for col in db.list_collection_names():
        count = db[col].count_documents({})
        print(f"  ├─ {col} ({count} documents)")
    
    client.close()

if __name__ == "__main__":
    try:
        init_database()
    except Exception as e:
        print(f"ERROR: {e}")
        exit(1)
