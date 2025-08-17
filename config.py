import os
from dotenv import load_dotenv

def load_mode_env():
    mode = os.getenv("MODE", "paper").lower()
    envfile = f".env.{mode}"
    if os.path.exists(envfile):
        load_dotenv(envfile)
    return mode
