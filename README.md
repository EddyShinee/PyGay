# MT5 <-> Python Socket Bridge + Web Dashboard

Kết nối nhiều MetaTrader 5 (EA) với Python qua TCP, JSON mỗi dòng một
message (newline-delimited JSON). **Python là server, mỗi MT5 terminal là
1 client.** Phía trên cầu nối đó là **1 dashboard web duy nhất quản lý
nhiều tài khoản** (FastAPI): trang tổng quan liệt kê mọi tài khoản đang/đã
từng kết nối, bấm vào 1 tài khoản để xem chi tiết - danh sách giao dịch,
thông tin tài khoản + insight BUY/SELL theo thời gian, đặt lệnh (kể cả
batch/grid DCA), đóng lệnh và sửa SL/TP.

```
mql5/
  Include/
    Socket.mqh   # wrapper Winsock (ws2_32.dll) - TCP client non-blocking
    Json.mqh     # JSON phẳng (flat) tối giản: parse + serialize
  Experts/
    SocketBridgeEA.mq5   # EA: gửi hello + tick + snapshot vị thế + account,
                          # nhận lệnh, đặt/đóng/sửa lệnh, đồng bộ deal đã đóng

python/
  protocol.py       # encode/decode khung JSON + \n
  socket_server.py  # transport layer (asyncio TCP server) - Client.account_id
                     # gắn lúc "hello", không chứa business logic
  session_manager.py # SessionManager: 1 AccountSession (gói mọi store/gateway
                      # bên dưới) cho mỗi account_id, tra cứu theo client.account_id
  handlers.py         # business logic: xử lý message theo "type", route vào
                       # đúng AccountSession qua client.account_id
  models.py             # dataclass Position
  position_store.py     # snapshot vị thế hiện tại + pub/sub cho WebSocket
  account_store.py        # snapshot tài khoản (balance/equity/margin/...) + pub/sub
  symbol_store.py           # danh sách symbol từ sàn (từ EA) + pub/sub
  price_cache.py              # giá bid/ask/point mới nhất theo symbol (từ tick)
  trade_gateway.py             # gửi lệnh xuống EA của 1 account, chờ order_result theo id
  history_gateway.py             # xin dữ liệu giá lịch sử (OHLC) từ EA của 1 account
  history.py                       # ghi bar lịch sử vào CSV theo account, dedupe theo time
  grid_jobs.py                       # batch order kiểu grid/DCA (theo dõi tick để bắn lệnh tiếp theo)
  db.py                                # SQLite: lưu deal đã đóng (khóa theo account_id+ticket)
  web.py                                 # FastAPI: REST + WebSocket, mọi route scope theo {account_id}
  static/index.html                        # dashboard: view tổng quan + view chi tiết theo account
  tools/fake_ea.py                           # giả lập EA (--account <id>) để test không cần MT5 thật
  tools/fetch_history_cron.py                  # cron gọi mỗi giờ để lấy giá lịch sử -> CSV
  main.py                                        # điểm khởi chạy (chạy chung socket server + web server)
  requirements.txt
  trades.db      # SQLite, tự tạo khi chạy - không commit (đã .gitignore)
  history/         # CSV giá lịch sử theo account/symbol/timeframe, tự tạo - không commit
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

Chưa có MT5 thật? Mở terminal khác, chạy `python3 tools/fake_ea.py --account 1001`
để giả lập EA (random-walk giá + vị thế giả) và thao tác thử trên dashboard.
Chạy thêm `python3 tools/fake_ea.py --account 1002 --ticket-start 5000` ở
terminal khác nữa để giả lập tài khoản thứ 2 - cả 2 cùng connect vào port
9090, dashboard sẽ hiện cả 2 trong trang tổng quan.

Trong MetaEditor: copy `mql5/Include/*.mqh` vào `MQL5/Include/`,
`mql5/Experts/SocketBridgeEA.mq5` vào `MQL5/Experts/`, compile, rồi gắn EA
vào từng chart - **mỗi MT5 terminal/tài khoản gắn EA riêng, tất cả trỏ về
cùng 1 `InpHost`/`InpPort`** (mặc định `127.0.0.1:9090`); Python tự phân
biệt tài khoản qua message `hello` mà EA gửi lúc kết nối, không cần cấu
hình gì thêm để chạy nhiều tài khoản.

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
| `hello` | account_id (=`ACCOUNT_LOGIN`), broker, name, currency | **luôn gửi đầu tiên**, ngay sau khi connect thành công, trước mọi message khác |
| `tick` | symbol, bid, ask, point, time | mỗi tick |
| `positions_begin` | count | mở đầu 1 đợt snapshot vị thế |
| `position` | ticket, symbol, side, volume, price_open, sl, tp, profit, swap, time_open, magic | mỗi vị thế đang mở, trong 1 đợt snapshot |
| `positions_end` | (không có) | kết thúc 1 đợt snapshot vị thế |
| `order_result` | id, ok, ticket, error | trả lời open_order/close_position/close_all/modify_position/set_magic |
| `account` | balance, equity, margin, margin_free, margin_level, currency, leverage, magic | định kỳ, cùng nhịp với snapshot vị thế |
| `deal_closed` | ticket (=position id), symbol, side, volume, price_open, price_close, profit, swap, commission, time_open, time_close | khi phát hiện 1 vị thế vừa đóng (qua `HistorySelect`), chỉ tính từ lúc EA kết nối - không backfill lịch sử cũ |
| `symbols` | list (chuỗi symbol cách nhau bởi dấu phẩy) | 1 lần ngay sau khi connect thành công |
| `history_begin` / `bar` / `history_end` | begin: id, symbol, timeframe, count; bar: id, time, open, high, low, close, tick_volume, spread; end: id | trả lời `get_history`, mỗi bar 1 message, đóng khung bởi begin/end |

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
| `set_magic` | id, magic | `CTrade::SetExpertMagicNumber(magic)` - áp dụng cho mọi lệnh mở sau đó |
| `get_history` | id, symbol, timeframe (`M1`..`MN1`), count | `CopyRates(...)`, trả về qua `history_begin`/`bar`/`history_end` |

`open_order`/`close_position`/`close_all`/`modify_position`/`set_magic`/`get_history`
đều có `id` (uuid) để Python khớp phản hồi (`order_result` hoặc chuỗi
`history_begin`/`bar`/`history_end`) với lời gọi tương ứng
(`trade_gateway.py`/`history_gateway.py`).

`close_by_threshold` (đóng khi tổng lãi/lỗ đạt ngưỡng $) không có message EA
riêng — Python tự tính tổng từ `position_store` rồi gửi `close_all` bình
thường nếu đạt ngưỡng. Đây là hành động kiểm tra 1 lần khi bấm nút, không
phải watcher chạy nền.

## Dashboard web

Mở `http://127.0.0.1:8000` — **trang tổng quan** hiện ra trước tiên: 1 card
mỗi tài khoản đang/đã từng kết nối (chấm xanh = online, xám = offline),
kèm Balance/Equity/lãi-lỗ trôi nổi/số lệnh rút gọn, cập nhật realtime qua
`WS /ws/accounts`. Bấm vào 1 card để vào **view chi tiết** của tài khoản đó
(nút "← Tất cả tài khoản" để quay lại). Tài khoản offline vẫn hiện trong
danh sách và xem được dữ liệu/lịch sử cuối cùng biết được, nhưng mọi nút
đặt/đóng/sửa lệnh trong view chi tiết bị vô hiệu hóa (mờ đi, server cũng
chặn bằng HTTP 409 nếu cố gọi thẳng API).

Trong view chi tiết:
- Bảng vị thế đang mở, cập nhật realtime qua WebSocket (`/ws/{account_id}/positions`).
  Click header **Giá vào / Thời gian / Profit** để sort (click lại để đảo
  chiều tăng/giảm), và ô **Lọc theo ticket** để lọc nhanh 1 lệnh cụ thể -
  cả hai chỉ ảnh hưởng hiển thị, không ảnh hưởng "Số lệnh"/"Tổng lãi/lỗ".
- **Lệnh nhanh**: dropdown Symbol (lấy từ danh sách sàn thật trả về qua
  message `symbols`, không gõ tay) + 2 nút BUY/SELL + ô Lot, vào lệnh
  market ngay lập tức (không cần mở form). Form "+ Thêm lệnh" cũng dùng
  chung dropdown này.
- **+ Thêm lệnh**: đặt 1 lệnh hoặc 1 batch (grid DCA) — count, khoảng cách
  giá (points), hướng giãn giá (ngược/cùng chiều lệnh), khoảng cách thời
  gian giữa các lệnh (giây), lot tăng dần (cộng cố định hoặc nhân hệ số).
  Lệnh #1 vào ngay; các lệnh sau chỉ vào khi giá đã dịch đủ + đã đủ giây,
  theo dõi qua chính tick stream đang chảy về (`grid_jobs.py`).
- **Đóng lệnh**: từng lệnh, tất cả, chỉ lệnh lãi, chỉ lệnh lỗ, hoặc khi
  tổng lãi/lỗ toàn tài khoản đạt ngưỡng $ nhập tay.
- **Sửa SL/TP**: sửa trực tiếp trên bảng, nhập giá tuyệt đối.
- **Magic Number**: ô nhập + nút Lưu, đổi runtime qua message `set_magic`
  (không cần vào MetaEditor/recompile). Áp dụng cho mọi lệnh mở sau đó qua
  `CTrade::SetExpertMagicNumber()`; giá trị hiện tại lấy từ `account.magic`.
- **Panel tài khoản**: Balance, Equity, Margin, Free Margin, Margin Level,
  lãi/lỗ trôi nổi (floating), số lệnh + volume BUY/SELL đang mở - cập nhật
  realtime qua WebSocket (`/ws/account`).
- **Insight BUY/SELL theo thời gian**: bảng tổng hợp số lệnh + lãi/lỗ đã
  chốt, tách riêng BUY/SELL, gộp theo phút/giờ/ngày/tháng/năm (dropdown),
  kèm win rate và profit factor tổng. Dữ liệu lấy từ `trades.db` (SQLite),
  được điền dần qua message `deal_closed` từ EA - vì vậy chỉ có dữ liệu từ
  lúc EA bắt đầu kết nối trở đi.

`GET /api/accounts` (danh sách tất cả tài khoản, dùng cho trang tổng quan)
và `WS /ws/accounts` không cần account_id. Mọi route còn lại scope theo
1 tài khoản qua prefix `/api/{account_id}/...`: `GET .../positions`,
`GET .../account`, `GET .../insights?bucket=day&limit=30`,
`GET .../summary`, `POST .../orders`, `POST .../positions/{ticket}/close`,
`POST .../positions/close_all`, `POST .../positions/close_by_threshold`,
`POST .../positions/{ticket}/modify`, `POST .../positions/refresh`,
`POST .../magic` (`{"magic": <int>}`); WS tương tự:
`/ws/{account_id}/positions`, `/ws/{account_id}/account`,
`/ws/{account_id}/symbols`. Account_id không tồn tại (`session_manager`
chưa từng thấy `hello` nào) trả về `404`; gọi lệnh khi tài khoản offline
trả về `409`.

## Lấy giá lịch sử (cho pipeline ML sau này)

Mục tiêu: tích lũy dữ liệu giá OHLC theo thời gian ra file CSV, để sau này
dùng huấn luyện model ML, đóng gói ONNX, chạy kèm indicator.

- **Không phải 1 tiến trình nền tự chạy riêng** — vì chỉ có `main.py` đang
  chạy mới giữ kết nối sống với EA, nên việc lấy lịch sử phải đi qua chính
  process đó: `POST /api/{account_id}/history/fetch` (`{"symbol","timeframe","count"}`)
  gọi `history_gateway.py` để xin `CopyRates(...)` từ EA của tài khoản đó,
  rồi `history.py` ghi các bar **mới** (so với lần ghi trước, dedupe theo
  `time`) vào `python/history/{account_id}_{symbol}_{timeframe}.csv`.
- **Trigger bằng cron của anh** (đúng như yêu cầu, Python không tự lên lịch):
  thêm `tools/fetch_history_cron.py` vào crontab, chạy mỗi giờ:
  ```
  0 * * * * cd /path/to/PyGay/python && .venv/bin/python3 tools/fetch_history_cron.py >> /tmp/fetch_history.log 2>&1
  ```
  Sửa danh sách `JOBS = [(account_id, symbol, timeframe, count), ...]`
  trong file đó để thêm/bớt tài khoản + symbol cần thu thập. `main.py` phải
  đang chạy (và EA của account đó đang online) khi cron gọi tới, nếu không
  sẽ trả lỗi 404 (chưa từng thấy account_id) hoặc 409 (đang offline).
- `count` nên đủ lớn để phủ hơn khoảng cách giữa 2 lần chạy cron (mặc định
  M1×1000 ≈ 16.6 giờ dữ liệu cho mỗi lần chạy cách nhau 1 giờ) — nhỡ 1 lần
  cron lỗi/máy tắt cũng không bị hở dữ liệu ở lần chạy kế tiếp.
- Lần đầu tiên fetch 1 symbol/timeframe mới, nếu MT5 chưa đồng bộ đủ lịch
  sử từ broker, `CopyRates` có thể trả về ít bar hơn `count` yêu cầu — gọi
  lại (cron lần sau) sẽ có thêm khi terminal đã tải xong.

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
2. Python: thêm `@server.on("account")` trong `handlers.py`, đầu hàm tra
   `session = sessions.get(client.account_id)` (như các handler khác) để
   biết cập nhật đúng `AccountSession` nào.

**Thêm field vào message có sẵn**: chỉ cần thêm một dòng `Add...()` (MQL5)
hoặc thêm key vào dict (Python) — không cần sửa transport layer.

**Thêm nút/hành động mới trên dashboard**: thêm 1 REST endpoint
`/api/{account_id}/...` trong `web.py` (gọi
`session.gateway`/`session.grid_manager`/`session.store` tương ứng, lấy
`session` qua `get_session(account_id)`), rồi thêm nút + hàm `fetch()`
trong `static/index.html` (nhớ chèn `currentAccountId` vào URL).

**Thêm state mới cần tách riêng theo tài khoản**: thêm field vào
`AccountSession.__init__()` trong `session_manager.py` - mọi handler/route
đã tra theo `client.account_id`/`account_id` sẵn, không cần sửa gì khác.

## Giới hạn hiện tại

- `Json.mqh` chỉ hỗ trợ object phẳng (string/number/bool), chưa hỗ trợ
  nested object hoặc array. Đủ dùng cho tick/signal/order/position; nếu cần
  cấu trúc phức tạp hơn, mở rộng `Parse()`/`Serialize()` trong file đó. Danh
  sách vị thế vì vậy được truyền dưới dạng nhiều message `position` liên
  tiếp (đóng khung bởi `positions_begin`/`positions_end`) thay vì 1 mảng JSON.
- Server Python hỗ trợ **nhiều EA/tài khoản kết nối cùng lúc**, phân biệt
  qua `account_id` (= `ACCOUNT_LOGIN`) gắn vào mỗi connection lúc `hello`.
  Chỉ enforce **1 kết nối sống cho mỗi account_id**: nếu 1 account_id cũ
  còn sót lại (EA bị recompile/mất kết nối đột ngột) mà có kết nối mới
  cùng account_id đó tới, kết nối cũ bị đóng ngay - tránh
  `trade_gateway.py` gửi lệnh vào 1 socket đã chết rồi timeout chờ phản
  hồi vô ích (xem `session_manager.py:bind()`).
- `session_manager.py` **giữ session lại sau khi EA disconnect** (đánh dấu
  `connected: false`) thay vì xóa hẳn, để vẫn xem được vị thế/insight lần
  cuối biết được. Hiện chưa có cơ chế dọn session của tài khoản không bao
  giờ kết nối lại - nếu chạy lâu dài với nhiều tài khoản test/tạm thời,
  danh sách tổng quan sẽ tích tụ dần (không ảnh hưởng chức năng, chỉ là
  danh sách dài hơn).
- Dashboard web **chưa có xác thực/đăng nhập** — chỉ nên chạy trên
  `127.0.0.1`, không expose ra ngoài mạng.
- Magic Number chỉ là **giá trị gắn vào lệnh mới mở** qua bridge - EA vẫn
  hiển thị/quản lý **tất cả** vị thế trên tài khoản (kể cả lệnh tay hoặc
  của EA khác), không tự lọc theo magic. Nếu cần chỉ quản lý lệnh của
  chính bridge, lọc thêm theo field `magic` ở phía Python/UI.
- `close_by_threshold` là hành động kiểm tra 1 lần khi bấm nút, không phải
  watcher tự động chạy nền theo dõi ngưỡng liên tục.
- `deal_closed`/`trades.db` chỉ có dữ liệu **từ lúc EA bắt đầu kết nối trở
  đi** — không backfill lịch sử deal cũ hơn. Nếu cần đầy đủ lịch sử, phải tự
  chạy `HistorySelect(0, TimeCurrent())` full range 1 lần và gửi hết (chưa
  làm, vì có thể rất lớn với tài khoản lâu năm).
- Margin trong `tools/fake_ea.py` là số giả lập đơn giản (không tính theo
  đòn bẩy/giá thực) — chỉ để test UI, không phản ánh margin thật.
- `get_history`/`CopyRates` chạy đồng bộ trong `OnTimer` của EA - `count`
  quá lớn (chục nghìn bar trở lên) có thể làm timer bị chậm 1 nhịp. Với vài
  nghìn bar mỗi lần gọi (như cấu hình mặc định) thì không đáng lo.

## Ý tưởng mở rộng thêm (chưa làm)

- Biểu đồ equity curve, lãi/lỗ theo giờ-trong-ngày / ngày-trong-tuần (hiện
  tại insight mới ở dạng bảng + thanh bar, chưa có chart theo thời gian).
- Cảnh báo rủi ro: tổng exposure hiện tại, % rủi ro mỗi lệnh so với balance,
  cảnh báo gần chạm giới hạn lỗ trong ngày hoặc gần margin call.
- Thời gian giữ lệnh trung bình, lãi/lỗ trung bình mỗi lệnh thắng/thua.
