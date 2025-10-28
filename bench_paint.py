import time
import tool

# micro-benchmark: measure how many paint packets can be created and queued per second
N = 200000
uid = 1000
token_bytes = bytes(range(16))
uid_bytes3 = (uid).to_bytes(3, 'little')
start = time.time()
for i in range(N):
    # vary x,y,color a bit to avoid trivial optimizations
    x = i & 0x3FF
    y = (i >> 10) & 0x3FF
    r = i & 0xFF
    g = (i >> 8) & 0xFF
    b = (i >> 16) & 0xFF
    paint_id = i
    tool.paint(None, uid, token_bytes, uid_bytes3, r, g, b, x, y, paint_id)
end = time.time()

duration = end - start
print(f"Created {N} paint packets in {duration:.3f}s -> {N/duration:.1f} ops/s")

# measure merge cost
start = time.time()
merged = tool.get_merged_data()
end = time.time()
print(f"Merged data size: {len(merged) if merged else 0} bytes, cost {(end-start)*1000:.3f} ms")
