# test_env.py
import os
from dotenv import load_dotenv

load_dotenv()

url = os.getenv("DATABASE_URL")
print(url)