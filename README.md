# MT5 <-> Python Socket Bridge

Kết nối MetaTrader 5 (EA) với Python qua TCP, JSON mỗi dòng một message
(newline-delimited JSON). **Python là server, MT5 là client.**

```
mql5/
  Include/
    Socket.mqh   # wrapper Winsock (ws2_32.dll) - TCP client non-blocking
    Json.mqh     # JSON phẳng (flat) tối giản: parse + serialize
  Experts/
    SocketBridgeEA.mq5   # EA: gửi tick, nhận signal, đặt lệnh

python/
  protocol.py      # encode/decode khung JSON + \n
  socket_server.py # transport layer (asyncio TCP server), không chứa business logic
  handlers.py       # business logic: xử lý message theo "type"
  main.py            # điểm khởi chạy
```

## Chạy thử

```bash
cd python
python3 main.py
```

Server lắng nghe `127.0.0.1:9090`.

Trong MetaEditor: copy `mql5/Include/*.mqh` vào `MQL5/Include/`,
`mql5/Experts/SocketBridgeEA.mq5` vào `MQL5/Experts/`, compile, rồi gắn EA
vào chart.

**Bắt buộc**: trong MT5, bật *Tools > Options > Expert Advisors > Allow DLL
imports*, và tick "Allow DLL imports" khi gắn EA vào chart. `Socket.mqh` dùng
`ws2_32.dll` (Winsock) vì MQL5 không có socket built-in — đây là cách chuẩn
mà cộng đồng MT5 dùng. Chỉ chạy được trên Windows (hoặc Wine, vì Wine có hỗ
trợ ws2_32).

## Giao thức

Mỗi message là một dòng JSON phẳng, kết thúc bằng `\n`.

MT5 -> Python:
```json
{"type":"tick","symbol":"EURUSD","bid":1.0855,"ask":1.0857,"time":1234567890}
```

Python -> MT5:
```json
{"type":"signal","action":"BUY","symbol":"EURUSD","volume":0.01}
```

`action` hỗ trợ sẵn: `BUY`, `SELL`, `CLOSE`.

## Vì sao nhanh (yếu tố thời gian)

- TCP thô + JSON dòng đơn giản, không có framework/HTTP overhead.
- Python dùng `asyncio` (non-blocking I/O), không có polling/sleep giả tạo.
- EA gửi tick ngay trong `OnTick()` (không chờ timer), và đọc socket
  non-blocking mỗi `InpPollMs` (mặc định 20ms) trong `OnTimer()` nên
  `OnTick()` không bao giờ bị block chờ dữ liệu vào.
- Socket đặt non-blocking (`ioctlsocket FIONBIO`) ngay sau khi connect.

## Cách mở rộng

**Thêm loại message mới từ Python gửi cho EA** (ví dụ đóng tất cả lệnh):
1. Python: gửi `{"type": "close_all"}` từ `handlers.py` (dùng `client.send(...)`
   hoặc `server.broadcast(...)`).
2. MQL5: thêm nhánh `if(type == "close_all") ...` trong `HandleMessage()`
   ở `SocketBridgeEA.mq5`.

**Thêm loại message mới từ EA gửi cho Python** (ví dụ gửi thông tin
account):
1. MQL5: build một `CJson` mới, `msg.AddString("type", "account")`, thêm
   field cần thiết, `g_socket.Send(msg.Serialize() + "\n")`.
2. Python: thêm `@server.on("account")` trong `handlers.py`.

**Thêm field vào message có sẵn**: chỉ cần thêm một dòng `Add...()` (MQL5)
hoặc thêm key vào dict (Python) — không cần sửa transport layer.

## Giới hạn hiện tại

- `Json.mqh` chỉ hỗ trợ object phẳng (string/number/bool), chưa hỗ trợ
  nested object hoặc array. Đủ dùng cho tick/signal/order; nếu cần cấu
  trúc phức tạp hơn, mở rộng `Parse()`/`Serialize()` trong file đó.
- Server Python hiện chấp nhận nhiều client cùng lúc nhưng chưa phân biệt
  client theo tên EA/account — nếu chạy nhiều EA song song, có thể thêm
  một message `{"type":"hello","account":...}` lúc kết nối để định danh.
# PyGay
