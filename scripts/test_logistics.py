"""测试物流轨迹生成器（各订单状态的多节点轨迹）。"""
import sys
sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from xhs_kefu.zhixia_tools import ZhixiaTools

tools = ZhixiaTools()

for oid, phone in [
    ("123456", "7658"),             # 演示订单，运输中
    ("ZX202608200147", "7319"),   # 运输中
    ("ZX202608180031", "4826"),   # 已签收
    ("ZX202608210083", "1654"),   # 待发货
    ("ZX202608170219", "9042"),   # 售后中
]:
    r = tools.logistics_lookup(oid, phone)
    if not r or r.get("error"):
        print(f"[{oid}] 失败: {r}")
        continue
    print(f"=== {oid}（{r['status']}）{r['carrier']} {r['tracking_masked']} ===")
    print(f"  ETA: {r['eta']}")
    for t in r["trace"]:
        print(f"  {t['time']}  {t['desc']}")
    print()
