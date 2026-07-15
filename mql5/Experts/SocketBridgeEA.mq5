//+------------------------------------------------------------------+
//|                                              SocketBridgeEA.mq5 |
//| Bridges MT5 <-> Python over TCP (Python runs the server).        |
//| - OnTick():  pushes the latest tick to Python immediately.       |
//| - OnTimer(): polls the socket for incoming messages (non-        |
//|              blocking), reconnects if the link drops, and pushes |
//|              a full position snapshot every InpPositionsIntervalMs.|
//| To extend: add a new "type" branch in HandleMessage().           |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>
#include <Socket.mqh>
#include <Json.mqh>

input string InpHost               = "127.0.0.1";
input int    InpPort               = 9090;
input int    InpPollMs             = 20;     // socket poll interval (ms) - lower = lower latency
input int    InpReconnectS         = 3;      // reconnect retry interval (s) while disconnected
input int    InpPositionsIntervalMs = 1000;  // how often to push a full positions snapshot
input long   InpMagicNumber         = 123456; // magic number tagged on every order this EA opens
input bool   InpStreamWatchSymbols  = true;  // stream prices for ALL Market Watch symbols (trade any ticker from one chart)
input int    InpPricesIntervalMs    = 500;   // how often to push Market Watch prices
input int    InpDeviationPoints     = 10;    // max slippage (points) accepted on market orders

CSocket  g_socket;
CTrade   g_trade;
string   g_rx_buffer = "";
datetime g_last_reconnect_attempt = 0;
ulong    g_last_positions_ms = 0;
ulong    g_last_prices_ms = 0;
int      g_last_history_total = 0;
long     g_magic = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   g_magic = InpMagicNumber;
   g_trade.SetExpertMagicNumber(g_magic);
   EventSetMillisecondTimer(InpPollMs);
   TryConnect();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   g_socket.Close();
}

//+------------------------------------------------------------------+
//| Push the current tick to Python as soon as it arrives            |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!g_socket.IsConnected())
      return;

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   CJson msg;
   msg.AddString("type", "tick");
   msg.AddString("symbol", _Symbol);
   msg.AddDouble("bid", tick.bid, _Digits);
   msg.AddDouble("ask", tick.ask, _Digits);
   msg.AddDouble("point", SymbolInfoDouble(_Symbol, SYMBOL_POINT), 8);
   msg.AddInt("time", (long)tick.time);

   g_socket.Send(msg.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Poll the socket for incoming data; reconnect if disconnected;    |
//| push a positions snapshot on its own interval                   |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_socket.IsConnected())
   {
      if(TimeCurrent() - g_last_reconnect_attempt >= InpReconnectS)
         TryConnect();
      return;
   }

   if(!g_socket.Receive(g_rx_buffer))
   {
      Print("SocketBridgeEA: connection lost");
      return;
   }

   DrainMessages();

   if(InpStreamWatchSymbols && GetTickCount64() - g_last_prices_ms >= (ulong)InpPricesIntervalMs)
   {
      SendWatchPrices();
      g_last_prices_ms = GetTickCount64();
   }

   if(GetTickCount64() - g_last_positions_ms >= (ulong)InpPositionsIntervalMs)
   {
      SendPositionsSnapshot();
      SendAccountInfo();
      SyncClosedDeals();
      g_last_positions_ms = GetTickCount64();
   }
}

//+------------------------------------------------------------------+
//| Push the latest bid/ask for every symbol in the Market Watch so  |
//| the server can quote & trade ANY ticker from a single chart.     |
//| The user curates which tickers are tradable by adding them to    |
//| the Market Watch window.                                          |
//+------------------------------------------------------------------+
void SendWatchPrices()
{
   int total = SymbolsTotal(true); // true = Market Watch symbols only
   for(int i = 0; i < total; i++)
   {
      string sym = SymbolName(i, true);
      MqlTick tick;
      if(!SymbolInfoTick(sym, tick))
         continue;
      if(tick.bid <= 0 && tick.ask <= 0)
         continue;

      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      CJson msg;
      msg.AddString("type", "tick");
      msg.AddString("symbol", sym);
      msg.AddDouble("bid", tick.bid, digits);
      msg.AddDouble("ask", tick.ask, digits);
      msg.AddDouble("point", SymbolInfoDouble(sym, SYMBOL_POINT), 8);
      msg.AddInt("time", (long)tick.time);
      g_socket.Send(msg.Serialize() + "\n");
   }
}

void TryConnect()
{
   g_last_reconnect_attempt = TimeCurrent();
   if(g_socket.Connect(InpHost, InpPort))
   {
      Print("SocketBridgeEA: connected to ", InpHost, ":", InpPort);
      SendHello();
      SendSymbolList();
   }
   else
      Print("SocketBridgeEA: connect failed, will retry");
}

//+------------------------------------------------------------------+
//| Identify which MT5 account this connection belongs to - sent      |
//| once, first, right after connecting. Everything else in the       |
//| protocol is unchanged; Python routes by account_id from here on   |
//| using the socket connection itself, not per-message fields.       |
//+------------------------------------------------------------------+
void SendHello()
{
   CJson msg;
   msg.AddString("type", "hello");
   msg.AddString("platform", "mt5");
   msg.AddInt("account_id", AccountInfoInteger(ACCOUNT_LOGIN));
   msg.AddString("broker", AccountInfoString(ACCOUNT_COMPANY));
   msg.AddString("name", AccountInfoString(ACCOUNT_NAME));
   msg.AddString("currency", AccountInfoString(ACCOUNT_CURRENCY));
   g_socket.Send(msg.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Full broker symbol list, sent once per connection (rarely changes|
//| mid-session) as a single comma-joined string - no need for the   |
//| begin/position/end framing used for things that repeat.          |
//+------------------------------------------------------------------+
void SendSymbolList()
{
   int total = SymbolsTotal(false); // false = every symbol the broker offers
   string list = "";
   for(int i = 0; i < total; i++)
   {
      if(i > 0)
         list += ",";
      list += SymbolName(i, false);
   }

   CJson msg;
   msg.AddString("type", "symbols");
   msg.AddString("list", list);
   g_socket.Send(msg.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Split g_rx_buffer on newlines and dispatch each complete message |
//+------------------------------------------------------------------+
void DrainMessages()
{
   int pos;
   while((pos = StringFind(g_rx_buffer, "\n")) >= 0)
   {
      string line = StringSubstr(g_rx_buffer, 0, pos);
      g_rx_buffer = StringSubstr(g_rx_buffer, pos + 1);
      if(StringLen(line) == 0)
         continue;

      CJson msg;
      if(!msg.Parse(line))
      {
         Print("SocketBridgeEA: failed to parse: ", line);
         continue;
      }
      HandleMessage(msg);
   }
}

//+------------------------------------------------------------------+
//| Message dispatch - add new "type" values here to extend          |
//+------------------------------------------------------------------+
void HandleMessage(CJson &msg)
{
   string type = msg.GetString("type");

   if(type == "pong")
      return; // heartbeat reply, nothing to do
   if(type == "signal")
      { HandleSignal(msg); return; }
   if(type == "get_positions")
      { SendPositionsSnapshot(); return; }
   if(type == "open_order")
      { HandleOpenOrder(msg); return; }
   if(type == "close_position")
      { HandleClosePosition(msg); return; }
   if(type == "close_all")
      { HandleCloseAll(msg); return; }
   if(type == "modify_position")
      { HandleModifyPosition(msg); return; }
   if(type == "set_magic")
      { HandleSetMagic(msg); return; }
   if(type == "get_history")
      { HandleGetHistory(msg); return; }
   if(type == "watch_symbol")
      { HandleWatchSymbol(msg); return; }

   Print("SocketBridgeEA: unknown message type: ", type);
}

//+------------------------------------------------------------------+
//| Round the requested lot size to the broker's min/step/max so a    |
//| config typo (e.g. 0.015 on a 0.01-step symbol) isn't rejected.    |
//+------------------------------------------------------------------+
double NormalizeVolume(const string symbol, double volume)
{
   double vmin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(vstep > 0)
      volume = MathRound(volume / vstep) * vstep;
   if(vmin > 0 && volume < vmin)
      volume = vmin;
   if(vmax > 0 && volume > vmax)
      volume = vmax;
   return NormalizeDouble(volume, 8);
}

//+------------------------------------------------------------------+
//| Clamp SL/TP to the broker's minimum stop distance                 |
//| (SYMBOL_TRADE_STOPS_LEVEL) and round to symbol digits, so a       |
//| too-tight distance is pushed out instead of the order rejected    |
//| with "invalid stops" over and over.                               |
//+------------------------------------------------------------------+
void ClampStops(const string symbol, const string side, double &sl, double &tp)
{
   int    digits   = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point    = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double min_dist = (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   double bid      = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask      = SymbolInfoDouble(symbol, SYMBOL_ASK);

   if(side == "BUY")
   {
      if(sl > 0 && bid - sl < min_dist) sl = bid - min_dist;
      if(tp > 0 && tp - bid < min_dist) tp = bid + min_dist;
   }
   else
   {
      if(sl > 0 && sl - ask < min_dist) sl = ask + min_dist;
      if(tp > 0 && ask - tp < min_dist) tp = ask - min_dist;
   }
   if(sl > 0) sl = NormalizeDouble(sl, digits);
   if(tp > 0) tp = NormalizeDouble(tp, digits);
}

//+------------------------------------------------------------------+
//| Shared market-order execution, used by both the automatic        |
//| "signal" path and the manual/web "open_order" path                |
//+------------------------------------------------------------------+
bool ExecuteMarketOrder(const string side, const string symbol, const double volume,
                         const double sl, const double tp, ulong &out_ticket,
                         const string comment = "")
{
   // Ensure the symbol is in Market Watch so the terminal has quotes for it;
   // lets us trade any ticker even though the EA sits on one chart.
   SymbolSelect(symbol, true);

   // Brokers differ on filling mode (FOK/IOC/...) - set it per symbol like
   // the close path already does, or some brokers reject every open.
   g_trade.SetTypeFillingBySymbol(symbol);
   g_trade.SetDeviationInPoints(InpDeviationPoints);

   out_ticket = 0;
   if(side != "BUY" && side != "SELL")
   {
      Print("SocketBridgeEA: unknown side: ", side);
      return false;
   }

   double use_volume = NormalizeVolume(symbol, volume);

   // Mirror the MT4 open path: a market order can be rejected by the same
   // transient conditions as a close (requote, off-quotes, trade-context-busy,
   // stale price) - re-clamp against a fresh price and retry a few times before
   // giving up. CTrade runs synchronously (SetAsyncMode default false), so a
   // failed attempt did NOT open a position - retrying can't double-fill.
   for(int attempt = 0; attempt < 3; attempt++)
   {
      if(attempt > 0)
         Sleep(300);
      double use_sl = sl, use_tp = tp;
      ClampStops(symbol, side, use_sl, use_tp);
      bool ok = (side == "BUY")
                ? g_trade.Buy(use_volume, symbol, 0.0, use_sl, use_tp, comment)
                : g_trade.Sell(use_volume, symbol, 0.0, use_sl, use_tp, comment);
      if(ok)
      {
         out_ticket = g_trade.ResultOrder();
         return true;
      }
      Print("SocketBridgeEA: OrderSend ", side, " ", symbol, " attempt ", attempt + 1,
            "/3 failed - ", g_trade.ResultRetcode(), " ",
            g_trade.ResultRetcodeDescription());
   }
   return false;
}

void SendOrderResult(const string id, const bool ok, const ulong ticket, const string error,
                     const int closed_count = -1)
{
   CJson msg;
   msg.AddString("type", "order_result");
   msg.AddString("id", id);
   msg.AddBool("ok", ok);
   msg.AddInt("ticket", (long)ticket);
   msg.AddString("error", error);
   if(closed_count >= 0)
      msg.AddInt("closed_count", closed_count);
   g_socket.Send(msg.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Automatic order from the ML-model/strategy path (Python "signal")|
//+------------------------------------------------------------------+
void HandleSignal(CJson &msg)
{
   string action = msg.GetString("action");
   string symbol = msg.GetString("symbol", _Symbol);
   double volume = msg.GetDouble("volume", 0.01);
   double sl     = msg.GetDouble("sl", 0.0);
   double tp     = msg.GetDouble("tp", 0.0);

   if(action == "CLOSE")
   {
      CloseAllForSymbol(symbol);
      return;
   }

   ulong ticket = 0;
   if(action == "BUY" || action == "SELL")
      ExecuteMarketOrder(action, symbol, volume, sl, tp, ticket);
   else
      Print("SocketBridgeEA: unknown action: ", action);
}

//+------------------------------------------------------------------+
//| Manual/web order placement (Python "open_order")                  |
//+------------------------------------------------------------------+
void HandleOpenOrder(CJson &msg)
{
   string id      = msg.GetString("id");
   string side    = msg.GetString("side");
   string symbol  = msg.GetString("symbol", _Symbol);
   double volume  = msg.GetDouble("volume", 0.01);
   double sl      = msg.GetDouble("sl", 0.0);
   double tp      = msg.GetDouble("tp", 0.0);
   string comment = msg.GetString("comment", "");

   // Preferred: SL/TP as point distances. Recomputed here from the
   // terminal's CURRENT price - fresher than the tick Python calculated
   // its absolute prices from (those stay as fallback for old servers).
   double sl_points = msg.GetDouble("sl_points", 0.0);
   double tp_points = msg.GetDouble("tp_points", 0.0);
   if(sl_points > 0 || tp_points > 0)
   {
      SymbolSelect(symbol, true);
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      double base = (side == "BUY") ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                                    : SymbolInfoDouble(symbol, SYMBOL_BID);
      if(point > 0 && base > 0)
      {
         double sign = (side == "BUY") ? 1.0 : -1.0;
         if(sl_points > 0) sl = base - sign * sl_points * point;
         if(tp_points > 0) tp = base + sign * tp_points * point;
      }
   }

   ulong ticket = 0;
   bool ok = ExecuteMarketOrder(side, symbol, volume, sl, tp, ticket, comment);
   SendOrderResult(id, ok, ticket, ok ? "" : g_trade.ResultRetcodeDescription());
   // Push the new state right away so the dashboard reflects it instantly,
   // instead of waiting up to InpPositionsIntervalMs for the periodic snapshot
   // (matters most for Telegram-initiated orders, which don't ask Python to
   // refresh). CTrade runs synchronously, so the position is already live here.
   if(ok)
      SendPositionsSnapshot();
}

bool CloseTicketReliable(const ulong ticket)
{
   // Mirror the MT4 close path: closing several positions back-to-back
   // (close_all / close-by-symbol) makes transient broker rejections (requote,
   // off-quotes, trade-context-busy, stale price) far more likely, so re-select
   // and retry a few times before giving up. A ticket that is no longer open is
   // treated as success: Python's snapshot is up to ~1s stale, so it can send a
   // close for a position the terminal already closed (SL/TP/manual) - that must
   // not surface a hard error or spam retries, exactly the MT4 4108 case.
   for(int attempt = 0; attempt < 3; attempt++)
   {
      if(attempt > 0)
         Sleep(300);
      if(!PositionSelectByTicket(ticket))
         return true;   // already closed / gone - goal achieved
      string symbol = PositionGetString(POSITION_SYMBOL);
      // Brokers differ on filling mode; set from the symbol before each close.
      g_trade.SetExpertMagicNumber(g_magic);
      g_trade.SetTypeFillingBySymbol(symbol);
      if(g_trade.PositionClose(ticket))
         return true;
      Print("SocketBridgeEA: PositionClose #", ticket, " attempt ", attempt + 1,
            "/3 failed - ", g_trade.ResultRetcode(), " ",
            g_trade.ResultRetcodeDescription());
   }
   return false;
}

//+------------------------------------------------------------------+
//| Close every open position on a symbol (MT4 parity for a "CLOSE"  |
//| signal, which can face multiple hedged positions). Collect first,|
//| then close - closing while iterating PositionsTotal is unsafe.   |
//+------------------------------------------------------------------+
void CloseAllForSymbol(const string symbol)
{
   ulong tickets[];
   ArrayResize(tickets, 0);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol)
         continue;
      int n = ArraySize(tickets);
      ArrayResize(tickets, n + 1);
      tickets[n] = ticket;
   }
   for(int i = 0; i < ArraySize(tickets); i++)
      CloseTicketReliable(tickets[i]);
}

void HandleClosePosition(CJson &msg)
{
   string id     = msg.GetString("id");
   ulong  ticket = (ulong)msg.GetInt("ticket");

   bool ok = CloseTicketReliable(ticket);
   SendOrderResult(id, ok, ticket, ok ? "" : g_trade.ResultRetcodeDescription());
   SendPositionsSnapshot();  // reflect the close on the dashboard immediately
}

void HandleCloseAll(CJson &msg)
{
   string id     = msg.GetString("id");
   string filter = msg.GetString("filter", "all");
   string only_symbol = msg.GetString("symbol", "");
   // Treat unknown / empty filter as close-everything so a parse miss never
   // silently matches zero tickets.
   if(filter != "profit" && filter != "loss")
      filter = "all";

   bool   all_ok = true;
   string last_error = "";
   int    closed_count = 0;

   // Collect tickets first — closing while iterating PositionsTotal is unsafe.
   ulong tickets[];
   ArrayResize(tickets, 0);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!PositionSelectByTicket(ticket))
         continue;

      if(only_symbol != "" && PositionGetString(POSITION_SYMBOL) != only_symbol)
         continue;

      double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      bool matches = (filter == "all") ||
                      (filter == "profit" && profit > 0) ||
                      (filter == "loss"   && profit < 0);
      if(!matches)
         continue;

      int n = ArraySize(tickets);
      ArrayResize(tickets, n + 1);
      tickets[n] = ticket;
   }

   for(int i = 0; i < ArraySize(tickets); i++)
   {
      if(!CloseTicketReliable(tickets[i]))
      {
         all_ok = false;
         last_error = g_trade.ResultRetcodeDescription();
      }
      else
         closed_count++;
   }

   Print("SocketBridgeEA: close_all filter=", filter,
         " matched=", ArraySize(tickets),
         " closed=", closed_count,
         " ok=", all_ok);
   SendOrderResult(id, all_ok, 0, last_error, closed_count);
   SendPositionsSnapshot();  // reflect the closes on the dashboard immediately
}

void HandleModifyPosition(CJson &msg)
{
   string id     = msg.GetString("id");
   ulong  ticket = (ulong)msg.GetInt("ticket");
   double sl     = msg.GetDouble("sl", 0.0);
   double tp     = msg.GetDouble("tp", 0.0);

   bool ok = g_trade.PositionModify(ticket, sl, tp);
   SendOrderResult(id, ok, ticket, ok ? "" : g_trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| Change the magic number tagged on future orders, at runtime -    |
//| no need to touch inputs/recompile in MetaEditor.                  |
//+------------------------------------------------------------------+
void HandleSetMagic(CJson &msg)
{
   string id = msg.GetString("id");
   g_magic = msg.GetInt("magic", g_magic);
   g_trade.SetExpertMagicNumber(g_magic);
   SendOrderResult(id, true, 0, "");
}

ENUM_TIMEFRAMES StringToTimeframe(const string tf)
{
   if(tf == "M1")  return PERIOD_M1;
   if(tf == "M5")  return PERIOD_M5;
   if(tf == "M15") return PERIOD_M15;
   if(tf == "M30") return PERIOD_M30;
   if(tf == "H1")  return PERIOD_H1;
   if(tf == "H4")  return PERIOD_H4;
   if(tf == "D1")  return PERIOD_D1;
   if(tf == "W1")  return PERIOD_W1;
   if(tf == "MN1") return PERIOD_MN1;
   return PERIOD_M1;
}

//+------------------------------------------------------------------+
//| Historical OHLC bars for one symbol/timeframe, framed by          |
//| history_begin/bar/history_end. Triggered on demand (Python's      |
//| /api/history/fetch), not sent automatically.                      |
//+------------------------------------------------------------------+
void HandleGetHistory(CJson &msg)
{
   string id      = msg.GetString("id");
   string symbol  = msg.GetString("symbol", _Symbol);
   string tf_str  = msg.GetString("timeframe", "M1");
   int    count   = (int)msg.GetInt("count", 1000);
   ENUM_TIMEFRAMES tf = StringToTimeframe(tf_str);

   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int copied = CopyRates(symbol, tf, 0, count, rates);
   if(copied < 0)
      copied = 0;

   CJson begin;
   begin.AddString("type", "history_begin");
   begin.AddString("id", id);
   begin.AddString("symbol", symbol);
   begin.AddString("timeframe", tf_str);
   begin.AddInt("count", copied);
   g_socket.Send(begin.Serialize() + "\n");

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   for(int i = 0; i < copied; i++)
   {
      CJson bar;
      bar.AddString("type", "bar");
      bar.AddString("id", id);
      bar.AddInt("time", (long)rates[i].time);
      bar.AddDouble("open", rates[i].open, digits);
      bar.AddDouble("high", rates[i].high, digits);
      bar.AddDouble("low", rates[i].low, digits);
      bar.AddDouble("close", rates[i].close, digits);
      bar.AddInt("tick_volume", (long)rates[i].tick_volume);
      bar.AddInt("spread", rates[i].spread);
      g_socket.Send(bar.Serialize() + "\n");
   }

   CJson end;
   end.AddString("type", "history_end");
   end.AddString("id", id);
   g_socket.Send(end.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Add a symbol to the Market Watch on demand so the server can      |
//| start quoting it (used when a ticker isn't streamed yet).         |
//+------------------------------------------------------------------+
void HandleWatchSymbol(CJson &msg)
{
   string symbol = msg.GetString("symbol", "");
   if(symbol != "")
      SymbolSelect(symbol, true);
}

//+------------------------------------------------------------------+
//| Full snapshot of open positions, framed by positions_begin/end   |
//+------------------------------------------------------------------+
void SendPositionsSnapshot()
{
   int total = PositionsTotal();

   CJson begin;
   begin.AddString("type", "positions_begin");
   begin.AddInt("count", total);
   g_socket.Send(begin.Serialize() + "\n");

   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      bool   is_buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);

      CJson msg;
      msg.AddString("type", "position");
      msg.AddInt("ticket", (long)ticket);
      msg.AddString("symbol", symbol);
      msg.AddString("side", is_buy ? "BUY" : "SELL");
      msg.AddDouble("volume", PositionGetDouble(POSITION_VOLUME), 2);
      msg.AddDouble("price_open", PositionGetDouble(POSITION_PRICE_OPEN), digits);
      msg.AddDouble("sl", PositionGetDouble(POSITION_SL), digits);
      msg.AddDouble("tp", PositionGetDouble(POSITION_TP), digits);
      msg.AddDouble("profit", PositionGetDouble(POSITION_PROFIT), 2);
      msg.AddDouble("swap", PositionGetDouble(POSITION_SWAP), 2);
      msg.AddInt("time_open", (long)PositionGetInteger(POSITION_TIME));
      msg.AddInt("magic", (long)PositionGetInteger(POSITION_MAGIC));
      msg.AddString("comment", PositionGetString(POSITION_COMMENT));
      g_socket.Send(msg.Serialize() + "\n");
   }

   CJson end;
   end.AddString("type", "positions_end");
   g_socket.Send(end.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Account snapshot (balance/equity/margin/...)                     |
//+------------------------------------------------------------------+
void SendAccountInfo()
{
   CJson msg;
   msg.AddString("type", "account");
   msg.AddDouble("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2);
   msg.AddDouble("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2);
   msg.AddDouble("margin", AccountInfoDouble(ACCOUNT_MARGIN), 2);
   msg.AddDouble("margin_free", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2);
   msg.AddDouble("margin_level", AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2);
   msg.AddString("currency", AccountInfoString(ACCOUNT_CURRENCY));
   msg.AddInt("leverage", AccountInfoInteger(ACCOUNT_LEVERAGE));
   msg.AddInt("magic", g_magic);
   g_socket.Send(msg.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Detect newly closed positions since the last check and report    |
//| each one as a "deal_closed" message (only deals closed after the |
//| EA connected - no retroactive backfill of old history).          |
//+------------------------------------------------------------------+
void SyncClosedDeals()
{
   if(!HistorySelect(0, TimeCurrent()))
      return;

   int total = HistoryDealsTotal();
   if(total <= g_last_history_total)
      return;

   long new_position_ids[];
   int  new_count = 0;
   for(int i = g_last_history_total; i < total; i++)
   {
      ulong deal_ticket = HistoryDealGetTicket(i);
      if(deal_ticket == 0)
         continue;
      if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT)
         continue;

      ArrayResize(new_position_ids, new_count + 1);
      new_position_ids[new_count++] = (long)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
   }
   g_last_history_total = total;

   for(int i = 0; i < new_count; i++)
      SendClosedDeal(new_position_ids[i]);

   // HistorySelectByPosition (inside SendClosedDeal) replaces the selected
   // history set, so restore the general one for the next call.
   HistorySelect(0, TimeCurrent());
}

void SendClosedDeal(const long position_id)
{
   if(!HistorySelectByPosition(position_id))
      return;

   int   total     = HistoryDealsTotal();
   ulong in_ticket  = 0;
   ulong out_ticket = 0;
   double profit = 0, swap = 0, commission = 0, volume = 0;

   for(int i = 0; i < total; i++)
   {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0)
         continue;
      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(t, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN && in_ticket == 0)
         in_ticket = t;
      if(entry == DEAL_ENTRY_OUT)
      {
         out_ticket = t; // covers partial closes: keep the latest, sum totals below
         profit     += HistoryDealGetDouble(t, DEAL_PROFIT);
         swap       += HistoryDealGetDouble(t, DEAL_SWAP);
         commission += HistoryDealGetDouble(t, DEAL_COMMISSION);
         volume     += HistoryDealGetDouble(t, DEAL_VOLUME);
      }
   }
   if(in_ticket == 0 || out_ticket == 0)
      return;

   string symbol = HistoryDealGetString(out_ticket, DEAL_SYMBOL);
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   // Closing a BUY position produces a SELL deal and vice versa.
   ENUM_DEAL_TYPE out_type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(out_ticket, DEAL_TYPE);
   string side = (out_type == DEAL_TYPE_SELL) ? "BUY" : "SELL";

   CJson msg;
   msg.AddString("type", "deal_closed");
   msg.AddInt("ticket", position_id);
   msg.AddString("symbol", symbol);
   msg.AddString("side", side);
   msg.AddDouble("volume", volume, 2);
   msg.AddDouble("price_open", HistoryDealGetDouble(in_ticket, DEAL_PRICE), digits);
   msg.AddDouble("price_close", HistoryDealGetDouble(out_ticket, DEAL_PRICE), digits);
   msg.AddDouble("profit", profit, 2);
   msg.AddDouble("swap", swap, 2);
   msg.AddDouble("commission", commission, 2);
   msg.AddInt("time_open", (long)HistoryDealGetInteger(in_ticket, DEAL_TIME));
   msg.AddInt("time_close", (long)HistoryDealGetInteger(out_ticket, DEAL_TIME));
   g_socket.Send(msg.Serialize() + "\n");
}
