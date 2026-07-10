# MT5 <-> Python Socket Bridge + Web Dashboard

Kết nối MetaTrader 5 (EA) với Python qua TCP, JSON mỗi dòng một message
(newline-delimited JSON). **Python là server, MT5 là client.** Phía trên
cầu nối đó là một dashboard web (FastAPI) để xem danh sách giao dịch, thông
tin tài khoản + insight BUY/SELL theo thời gian, đặt lệnh (kể cả batch/grid
DCA), đóng lệnh và sửa SL/TP.

```
mql5/
  Include/
    Socket.mqh   # wrapper Winsock (ws2_32.dll) - TCP client non-blocking
    Json.mqh     # JSON phẳng (flat) tối giản: parse + serialize
  Experts/
    SocketBridgeEA.mq5   # EA: gửi tick + snapshot vị thế + account, nhận lệnh,
                          # đặt/đóng/sửa lệnh, đồng bộ deal đã đóng

python/
  protocol.py       # encode/decode khung JSON + \n
  socket_server.py  # transport layer (asyncio TCP server), không chứa business logic
  handlers.py        # business logic: xử lý message theo "type"
  models.py           # dataclass Position
  position_store.py   # snapshot vị thế hiện tại + pub/sub cho WebSocket
  account_store.py      # snapshot tài khoản (balance/equity/margin/...) + pub/sub
  price_cache.py          # giá bid/ask/point mới nhất theo symbol (từ tick)
  trade_gateway.py         # gửi lệnh xuống EA, chờ order_result theo id
  grid_jobs.py               # batch order kiểu grid/DCA (theo dõi tick để bắn lệnh tiếp theo)
  db.py                        # SQLite: lưu deal đã đóng + query insight theo bucket thời gian
  web.py                         # FastAPI: REST + WebSocket cho dashboard
  static/index.html                # giao diện dashboard (vanilla JS, không build step)
  tools/fake_ea.py                   # giả lập EA để test không cần MT5 thật
  main.py                              # điểm khởi chạy (chạy chung socket server + web server)
  requirements.txt
  trades.db                             # SQLite, tự tạo khi chạy - không commit (đã .gitignore)
```

## Chạy thử

```bash
cd python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

- Socket server (cho EA) lắng nghe `127.0.0.1:9090`.
- Web dashboard chạy ở `http://127.0.0.1:8000` (chỉ nên chạy trên
  localhost - **chưa có xác thực/đăng nhập**).

Chưa có MT5 thật? Mở terminal khác, chạy `python3 tools/fake_ea.py` để giả
lập EA (random-walk giá + vị thế giả) và thao tác thử trên dashboard.

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

### MT5 -> Python

| type | fields | khi nào gửi |
|---|---|---|
| `tick` | symbol, bid, ask, point, time | mỗi tick |
| `positions_begin` | count | mở đầu 1 đợt snapshot vị thế |
| `position` | ticket, symbol, side, volume, price_open, sl, tp, profit, swap, time_open | mỗi vị thế đang mở, trong 1 đợt snapshot |
| `positions_end` | (không có) | kết thúc 1 đợt snapshot vị thế |
| `order_result` | id, ok, ticket, error | trả lời open_order/close_position/close_all/modify_position |
| `account` | balance, equity, margin, margin_free, margin_level, currency, leverage | định kỳ, cùng nhịp với snapshot vị thế |
| `deal_closed` | ticket (=position id), symbol, side, volume, price_open, price_close, profit, swap, commission, time_open, time_close | khi phát hiện 1 vị thế vừa đóng (qua `HistorySelect`), chỉ tính từ lúc EA kết nối - không backfill lịch sử cũ |

Ví dụ tick: `{"type":"tick","symbol":"EURUSD","bid":1.0855,"ask":1.0857,"point":0.00001,"time":1234567890}`

### Python -> MT5

| type | fields | hành động EA |
|---|---|---|
| `signal` | action (`BUY`\|`SELL`\|`CLOSE`), symbol, volume, sl, tp | đường tự động cũ (model/chiến lược tự gửi tín hiệu) |
| `get_positions` | (không có) | gửi ngay 1 đợt snapshot vị thế |
| `open_order` | id, symbol, side, volume, sl (giá tuyệt đối, 0 = không đặt), tp | `CTrade::Buy/Sell` |
| `close_position` | id, ticket | `CTrade::PositionClose(ticket)` |
| `close_all` | id, filter (`all`\|`profit`\|`loss`) | đóng các vị thế khớp filter |
| `modify_position` | id, ticket, sl, tp | `CTrade::PositionModify(ticket, sl, tp)` |

`open_order`/`close_position`/`close_all`/`modify_position` đều có `id` (uuid) để
Python khớp `order_result` trả về với lời gọi tương ứng (`trade_gateway.py`).

`close_by_threshold` (đóng khi tổng lãi/lỗ đạt ngưỡng $) không có message EA
riêng — Python tự tính tổng từ `position_store` rồi gửi `close_all` bình
thường nếu đạt ngưỡng. Đây là hành động kiểm tra 1 lần khi bấm nút, không
phải watcher chạy nền.

## Dashboard web

Mở `http://127.0.0.1:8000`:
- Bảng vị thế đang mở, cập nhật realtime qua WebSocket (`/ws/positions`).
- **+ Thêm lệnh**: đặt 1 lệnh hoặc 1 batch (grid DCA) — count, khoảng cách
  giá (points), hướng giãn giá (ngược/cùng chiều lệnh), khoảng cách thời
  gian giữa các lệnh (giây), lot tăng dần (cộng cố định hoặc nhân hệ số).
  Lệnh #1 vào ngay; các lệnh sau chỉ vào khi giá đã dịch đủ + đã đủ giây,
  theo dõi qua chính tick stream đang chảy về (`grid_jobs.py`).
- **Đóng lệnh**: từng lệnh, tất cả, chỉ lệnh lãi, chỉ lệnh lỗ, hoặc khi
  tổng lãi/lỗ toàn tài khoản đạt ngưỡng $ nhập tay.
- **Sửa SL/TP**: sửa trực tiếp trên bảng, nhập giá tuyệt đối.
- **Panel tài khoản**: Balance, Equity, Margin, Free Margin, Margin Level,
  lãi/lỗ trôi nổi (floating), số lệnh + volume BUY/SELL đang mở - cập nhật
  realtime qua WebSocket (`/ws/account`).
- **Insight BUY/SELL theo thời gian**: bảng tổng hợp số lệnh + lãi/lỗ đã
  chốt, tách riêng BUY/SELL, gộp theo phút/giờ/ngày/tháng/năm (dropdown),
  kèm win rate và profit factor tổng. Dữ liệu lấy từ `trades.db` (SQLite),
  được điền dần qua message `deal_closed` từ EA - vì vậy chỉ có dữ liệu từ
  lúc EA bắt đầu kết nối trở đi.

REST API chính: `GET /api/positions`, `GET /api/account`,
`GET /api/insights?bucket=day&limit=30`, `GET /api/summary`, `POST /api/orders`,
`POST /api/positions/{ticket}/close`, `POST /api/positions/close_all`,
`POST /api/positions/close_by_threshold`, `POST /api/positions/{ticket}/modify`,
`POST /api/positions/refresh` (yêu cầu EA gửi lại snapshot ngay).

## Vì sao nhanh (yếu tố thời gian)

- TCP thô + JSON dòng đơn giản, không có framework/HTTP overhead.
- Python dùng `asyncio` (non-blocking I/O), không có polling/sleep giả tạo.
- EA gửi tick ngay trong `OnTick()` (không chờ timer), và đọc socket
  non-blocking mỗi `InpPollMs` (mặc định 20ms) trong `OnTimer()` nên
  `OnTick()` không bao giờ bị block chờ dữ liệu vào.
- Socket đặt non-blocking (`ioctlsocket FIONBIO`) ngay sau khi connect.

## Cách mở rộng

**Thêm loại message mới từ Python gửi cho EA** (ví dụ một lệnh mới
`hedge_close` chẳng hạn):
1. Python: gọi `client.send({"type": "hedge_close", ...})` từ đâu đó (thường
   qua một method mới trong `trade_gateway.py`, rồi expose bằng 1 endpoint
   trong `web.py` nếu cần bấm từ UI).
2. MQL5: thêm nhánh `if(type == "hedge_close") { HandleHedgeClose(msg); return; }`
   trong `HandleMessage()` ở `SocketBridgeEA.mq5`, viết hàm xử lý tương tự
   `HandleClosePosition`.

**Thêm loại message mới từ EA gửi cho Python** (ví dụ gửi thông tin
account):
1. MQL5: build một `CJson` mới, `msg.AddString("type", "account")`, thêm
   field cần thiết, `g_socket.Send(msg.Serialize() + "\n")`.
2. Python: thêm `@server.on("account")` trong `handlers.py`.

**Thêm field vào message có sẵn**: chỉ cần thêm một dòng `Add...()` (MQL5)
hoặc thêm key vào dict (Python) — không cần sửa transport layer.

**Thêm nút/hành động mới trên dashboard**: thêm 1 REST endpoint trong
`web.py` (gọi `trade_gateway`/`grid_manager`/`store` tương ứng), rồi thêm
nút + hàm `fetch()` trong `static/index.html`.

## Giới hạn hiện tại

- `Json.mqh` chỉ hỗ trợ object phẳng (string/number/bool), chưa hỗ trợ
  nested object hoặc array. Đủ dùng cho tick/signal/order/position; nếu cần
  cấu trúc phức tạp hơn, mở rộng `Parse()`/`Serialize()` trong file đó. Danh
  sách vị thế vì vậy được truyền dưới dạng nhiều message `position` liên
  tiếp (đóng khung bởi `positions_begin`/`positions_end`) thay vì 1 mảng JSON.
- Server Python/`trade_gateway.py` giả định **chỉ 1 EA/terminal kết nối**
  (lấy client kết nối gần nhất). Nếu chạy nhiều EA song song, cần thêm một
  message `{"type":"hello","account":...}` lúc kết nối để định danh và một
  cách chọn client theo account ở `trade_gateway.py`.
- Dashboard web **chưa có xác thực/đăng nhập** — chỉ nên chạy trên
  `127.0.0.1`, không expose ra ngoài mạng.
- `close_by_threshold` là hành động kiểm tra 1 lần khi bấm nút, không phải
  watcher tự động chạy nền theo dõi ngưỡng liên tục.
- `deal_closed`/`trades.db` chỉ có dữ liệu **từ lúc EA bắt đầu kết nối trở
  đi** — không backfill lịch sử deal cũ hơn. Nếu cần đầy đủ lịch sử, phải tự
  chạy `HistorySelect(0, TimeCurrent())` full range 1 lần và gửi hết (chưa
  làm, vì có thể rất lớn với tài khoản lâu năm).
- Margin trong `tools/fake_ea.py` là số giả lập đơn giản (không tính theo
  đòn bẩy/giá thực) — chỉ để test UI, không phản ánh margin thật.

## Ý tưởng mở rộng thêm (chưa làm)

- Biểu đồ equity curve, lãi/lỗ theo giờ-trong-ngày / ngày-trong-tuần (hiện
  tại insight mới ở dạng bảng + thanh bar, chưa có chart theo thời gian).
- Cảnh báo rủi ro: tổng exposure hiện tại, % rủi ro mỗi lệnh so với balance,
  cảnh báo gần chạm giới hạn lỗ trong ngày hoặc gần margin call.
- Thời gian giữ lệnh trung bình, lãi/lỗ trung bình mỗi lệnh thắng/thua.
