from pykrx import stock

# 삼성전자 최근 1주
df = stock.get_market_cap("20260420", "20260424", "005930")
print(df)
print(f"\n컬럼: {list(df.columns)}")
