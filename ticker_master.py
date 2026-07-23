from pykrx import stock
import pandas as pd

DATE = "20260424"

tickers_kospi  = stock.get_market_ticker_list(DATE, market="KOSPI")
tickers_kosdaq = stock.get_market_ticker_list(DATE, market="KOSDAQ")

rows = []
for t in tickers_kospi[:5]:
    rows.append({"Code": t, "Name": stock.get_market_ticker_name(t), "Market": "KOSPI"})
for t in tickers_kosdaq[:5]:
    rows.append({"Code": t, "Name": stock.get_market_ticker_name(t), "Market": "KOSDAQ"})

df = pd.DataFrame(rows)
print(df.to_string(index=False))
print(f"\nKOSPI: {len(tickers_kospi)}개 / KOSDAQ: {len(tickers_kosdaq)}개")
