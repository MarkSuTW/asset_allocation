import ssl
import urllib.request
import ssl
import urllib.request

ctx = ssl._create_unverified_context()
url = 'https://www.twse.com.tw/zh/ETFortune/dividendList?stkNo=2330&startDate=114&endDate=115'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
    txt = resp.read().decode('utf-8', 'ignore')

print(repr(txt))
print('TR_COUNT', len(re.findall(r'<tr[^>]*>', txt, re.I)))
for needle in ['ajax', 'dividend.js', '2330', '公告年度', '收益分配發放日', 'document.location']:
    print('NEEDLE', needle, txt.find(needle))
