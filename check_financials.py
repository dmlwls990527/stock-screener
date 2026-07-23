import yfinance as yf
import pandas as pd

pd.set_option("display.max_rows", 200)
pd.set_option("display.width", 120)

t = yf.Ticker("AAPL")

print("=" * 60)
print("  [1] financials (연간 손익계산서)")
print("=" * 60)
print(t.financials)

print("\n" + "=" * 60)
print("  [2] quarterly_financials (분기 손익계산서)")
print("=" * 60)
print(t.quarterly_financials)

print("\n" + "=" * 60)
print("  [3] balance_sheet (연간 대차대조표)")
print("=" * 60)
print(t.balance_sheet)

print("\n" + "=" * 60)
print("  [4] cashflow (연간 현금흐름)")
print("=" * 60)
print(t.cashflow)

print("\n" + "=" * 60)
print("  [5] info 주요 밸류에이션")
print("=" * 60)
info = t.info
keys = ["trailingPE","forwardPE","priceToBook","trailingEps","forwardEps",
        "dividendYield","revenueGrowth","earningsGrowth","debtToEquity",
        "returnOnEquity","returnOnAssets","freeCashflow","marketCap"]
for k in keys:
    print(f"  {k:30s}: {info.get(k)}")
