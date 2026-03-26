import asyncio
import logging
from app.main import _process_eod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

async def run_test():
    sender = 'shriya'
    clean_message = 'Thursday Doe\n-WEMS Catalogue draft\n-Reel shoot x1 \n-Gudi Padva Banners x2'
    timestamp = '2026-03-23T00:00:00Z'
    result = await _process_eod(sender, clean_message, timestamp)
    print('FINAL_RESULT:', result.model_dump_json())

if __name__ == '__main__':
    asyncio.run(run_test())
