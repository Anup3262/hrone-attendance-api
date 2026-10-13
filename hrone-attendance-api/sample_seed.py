"""Loads the sample documents from sample_data/ into your database.

Usage:  python sample_seed.py   (reads MONGO_URI / MONGO_DB from env or .env)
"""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
here = pathlib.Path(__file__).parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                 tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

for name in ("employees", "attendance_logs"):
    docs = json_util.loads((here / f"{name}.json").read_text(),
                           json_options=opts)
    db[name].delete_many({})
    if docs:
        db[name].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")
