import os
from dotenv import load_dotenv
load_dotenv('.env.test')
openrouter = os.environ.get('OPENROUTER_API_KEY', 'MISSING')
print(f'TEST ENV OPENROUTER_API_KEY: {"PRESENT" if openrouter != "MISSING" else "MISSING"}')

load_dotenv('.env', override=True)
openrouter = os.environ.get('OPENROUTER_API_KEY', 'MISSING')
print(f'PROD ENV OPENROUTER_API_KEY: {"PRESENT" if openrouter != "MISSING" else "MISSING"}')
