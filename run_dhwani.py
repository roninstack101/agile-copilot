import asyncio
import logging
from app.main import _process_eod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

async def run_test():
    result = await _process_eod('dhwani ', 'saturday eod:\n-wogom microfiction draft', '2026-03-23T00:00:00Z')
    print('FINAL_RESULT:', result.model_dump_json())

if __name__ == '__main__':
    asyncio.run(run_test())
