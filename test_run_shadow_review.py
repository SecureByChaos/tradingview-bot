import asyncio
from app.ai.shadow import run_shadow_review

async def main():
    mock_trade_data = {
        'trade_id': 1,
        'strategy_name': 'TestStrategy',
        'signal': 'BUY_CE',
        'current_premium': 100,
        'entry_price': 90,
        'strike': 45000,
        'expiry': '2026-07-10',
        'holding_time': 5
    }
    await run_shadow_review(mock_trade_data)

# Running the main function
asyncio.run(main())
