//+------------------------------------------------------------------+
//|                                              SocketBridgeEA.mq4 |
//| MT4 port of SocketBridgeEA.mq5 — same JSON protocol to Python.  |
//| Requires MT4 build 1280+ (EventSetMillisecondTimer). Windows +   |
//| Allow DLL imports (ws2_32 via Socket.mqh).                       |
//+------------------------------------------------------------------+
#property strict
#include <Socket.mqh>
#include <Json.mqh>

input string InpHost                = "127.0.0.1";
input int    InpPort                = 9090;
input int    InpPollMs              = 20;
input int    InpReconnectS          = 3;
input int    InpPositionsIntervalMs = 1000;
input int    InpMagicNumber         = 123456;
input bool   InpStreamWatchSymbols  = true;  // stream prices for ALL Market Watch symbols
input int    InpPricesIntervalMs    = 500;   // how often to push Market Watch prices

CSocket  g_socket;
string   g_rx_buffer = "";
datetime g_last_reconnect_attempt = 0;
uint     g_last_positions_ms = 0;
uint     g_last_prices_ms = 0;
int      g_last_history_total = 0;
int      g_magic = 0;
int      g_slippage = 3;

string LastOrderError()
{
   return "Error " + IntegerToString(GetLastError());
}

bool IsMarketOrderType(const int type)
{
   return (type == OP_BUY || type == OP_SELL);
}

int CountOpenMarketOrders()
{
   int count = 0;
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(IsMarketOrderType(OrderType()))
         count++;
   }
   return count;
}

bool CloseOrderTicket(const int ticket)
{
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return false;
   RefreshRates();
   string symbol = OrderSymbol();
   double price = (OrderType() == OP_BUY)
                  ? MarketInfo(symbol, MODE_BID)
                  : MarketInfo(symbol, MODE_ASK);
   return OrderClose(ticket, OrderLots(), price, g_slippage, clrNONE);
}

void CloseAllForSymbol(const string symbol)
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderSymbol() != symbol)
         continue;
      if(!IsMarketOrderType(OrderType()))
         continue;
      CloseOrderTicket(OrderTicket());
   }
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_magic = InpMagicNumber;
   EventSetMillisecondTimer(InpPollMs);
   TryConnect();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   g_socket.Close();
}

void OnTick()
{
   if(!g_socket.IsConnected())
      return;

   string symbol = Symbol();
   RefreshRates();
   double bid = MarketInfo(symbol, MODE_BID);
   double ask = MarketInfo(symbol, MODE_ASK);
   if(bid <= 0 || ask <= 0)
      return;

   CJson msg;
   msg.AddString("type", "tick");
   msg.AddString("symbol", symbol);
   msg.AddDouble("bid", bid, Digits);
   msg.AddDouble("ask", ask, Digits);
   msg.AddDouble("point", MarketInfo(symbol, MODE_POINT), 8);
   msg.AddInt("time", (long)TimeCurrent());

   g_socket.Send(msg.Serialize() + "\n");
}

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

   if(InpStreamWatchSymbols && GetTickCount() - g_last_prices_ms >= (uint)InpPricesIntervalMs)
   {
      SendWatchPrices();
      g_last_prices_ms = GetTickCount();
   }

   if(GetTickCount() - g_last_positions_ms >= (uint)InpPositionsIntervalMs)
   {
      SendPositionsSnapshot();
      SendAccountInfo();
      SyncClosedDeals();
      g_last_positions_ms = GetTickCount();
   }
}

//+------------------------------------------------------------------+
//| Push bid/ask for every Market Watch symbol so the server can     |
//| quote & trade any ticker from a single chart.                    |
//+------------------------------------------------------------------+
void SendWatchPrices()
{
   int total = SymbolsTotal(true); // Market Watch symbols only
   for(int i = 0; i < total; i++)
   {
      string sym = SymbolName(i, true);
      double bid = MarketInfo(sym, MODE_BID);
      double ask = MarketInfo(sym, MODE_ASK);
      if(bid <= 0 && ask <= 0)
         continue;

      int digits = (int)MarketInfo(sym, MODE_DIGITS);
      CJson msg;
      msg.AddString("type", "tick");
      msg.AddString("symbol", sym);
      msg.AddDouble("bid", bid, digits);
      msg.AddDouble("ask", ask, digits);
      msg.AddDouble("point", MarketInfo(sym, MODE_POINT), 8);
      msg.AddInt("time", (long)TimeCurrent());
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

void SendHello()
{
   CJson msg;
   msg.AddString("type", "hello");
   msg.AddString("platform", "mt4");
   msg.AddInt("account_id", AccountNumber());
   msg.AddString("broker", AccountCompany());
   msg.AddString("name", AccountName());
   msg.AddString("currency", AccountCurrency());
   g_socket.Send(msg.Serialize() + "\n");
}

void SendSymbolList()
{
   int total = SymbolsTotal(false);
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

void HandleMessage(CJson &msg)
{
   string type = msg.GetString("type");

   if(type == "pong")
      return;
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
//| config typo isn't rejected outright.                              |
//+------------------------------------------------------------------+
double NormalizeVolume(const string symbol, double volume)
{
   double vmin  = MarketInfo(symbol, MODE_MINLOT);
   double vmax  = MarketInfo(symbol, MODE_MAXLOT);
   double vstep = MarketInfo(symbol, MODE_LOTSTEP);
   if(vstep > 0)
      volume = MathRound(volume / vstep) * vstep;
   if(vmin > 0 && volume < vmin)
      volume = vmin;
   if(vmax > 0 && volume > vmax)
      volume = vmax;
   return NormalizeDouble(volume, 8);
}

//+------------------------------------------------------------------+
//| Clamp SL/TP to the broker's minimum stop distance (MODE_STOPLEVEL)|
//| and round to symbol digits, so a too-tight distance is pushed out |
//| instead of the order failing with error 130 (invalid stops).      |
//+------------------------------------------------------------------+
void ClampStops(const string symbol, const string side, double &sl, double &tp)
{
   int    digits   = (int)MarketInfo(symbol, MODE_DIGITS);
   double point    = MarketInfo(symbol, MODE_POINT);
   double min_dist = MarketInfo(symbol, MODE_STOPLEVEL) * point;
   double bid      = MarketInfo(symbol, MODE_BID);
   double ask      = MarketInfo(symbol, MODE_ASK);

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

bool ExecuteMarketOrder(const string side, const string symbol, const double volume,
                         const double sl, const double tp, int &out_ticket,
                         const string comment = "")
{
   // Make sure the symbol is in the Market Watch so quotes are available;
   // lets us trade any ticker from a single chart.
   SymbolSelect(symbol, true);
   RefreshRates();
   int type = (side == "BUY") ? OP_BUY : OP_SELL;
   if(side != "BUY" && side != "SELL")
   {
      Print("SocketBridgeEA: unknown side: ", side);
      return false;
   }

   string order_comment = (comment == "") ? "SocketBridgeEA" : comment;
   double use_volume = NormalizeVolume(symbol, volume);
   double use_sl = sl, use_tp = tp;
   ClampStops(symbol, side, use_sl, use_tp);
   double price = (type == OP_BUY)
                  ? MarketInfo(symbol, MODE_ASK)
                  : MarketInfo(symbol, MODE_BID);
   out_ticket = OrderSend(symbol, type, use_volume, price, g_slippage, use_sl, use_tp,
                          order_comment, g_magic, 0, clrNONE);
   return (out_ticket > 0);
}

void SendOrderResult(const string id, const bool ok, const int ticket, const string error,
                     const int closed_count = -1)
{
   CJson msg;
   msg.AddString("type", "order_result");
   msg.AddString("id", id);
   msg.AddBool("ok", ok);
   msg.AddInt("ticket", ticket);
   msg.AddString("error", error);
   if(closed_count >= 0)
      msg.AddInt("closed_count", closed_count);
   g_socket.Send(msg.Serialize() + "\n");
}

void HandleSignal(CJson &msg)
{
   string action = msg.GetString("action");
   string symbol = msg.GetString("symbol", Symbol());
   double volume = msg.GetDouble("volume", 0.01);
   double sl     = msg.GetDouble("sl", 0.0);
   double tp     = msg.GetDouble("tp", 0.0);

   if(action == "CLOSE")
   {
      CloseAllForSymbol(symbol);
      return;
   }

   int ticket = 0;
   if(action == "BUY" || action == "SELL")
      ExecuteMarketOrder(action, symbol, volume, sl, tp, ticket);
   else
      Print("SocketBridgeEA: unknown action: ", action);
}

void HandleOpenOrder(CJson &msg)
{
   string id      = msg.GetString("id");
   string side    = msg.GetString("side");
   string symbol  = msg.GetString("symbol", Symbol());
   double volume  = msg.GetDouble("volume", 0.01);
   double sl      = msg.GetDouble("sl", 0.0);
   double tp      = msg.GetDouble("tp", 0.0);
   string comment = msg.GetString("comment", "");

   // Preferred: SL/TP as point distances, recomputed from the terminal's
   // current price (fresher than the tick Python calculated from).
   double sl_points = msg.GetDouble("sl_points", 0.0);
   double tp_points = msg.GetDouble("tp_points", 0.0);
   if(sl_points > 0 || tp_points > 0)
   {
      SymbolSelect(symbol, true);
      RefreshRates();
      double point = MarketInfo(symbol, MODE_POINT);
      double base = (side == "BUY") ? MarketInfo(symbol, MODE_ASK)
                                    : MarketInfo(symbol, MODE_BID);
      if(point > 0 && base > 0)
      {
         double sign = (side == "BUY") ? 1.0 : -1.0;
         if(sl_points > 0) sl = base - sign * sl_points * point;
         if(tp_points > 0) tp = base + sign * tp_points * point;
      }
   }

   int ticket = 0;
   bool ok = ExecuteMarketOrder(side, symbol, volume, sl, tp, ticket, comment);
   SendOrderResult(id, ok, ticket, ok ? "" : LastOrderError());
}

void HandleClosePosition(CJson &msg)
{
   string id     = msg.GetString("id");
   int    ticket = (int)msg.GetInt("ticket");

   bool ok = CloseOrderTicket(ticket);
   SendOrderResult(id, ok, ticket, ok ? "" : LastOrderError());
}

void HandleCloseAll(CJson &msg)
{
   string id     = msg.GetString("id");
   string filter = msg.GetString("filter", "all");
   string only_symbol = msg.GetString("symbol", "");
   if(filter != "profit" && filter != "loss")
      filter = "all";

   bool   all_ok = true;
   string last_error = "";
   int    closed_count = 0;

   // Collect tickets first, then close — OrderClose shifts MODE_TRADES indices.
   int tickets[];
   ArrayResize(tickets, 0);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(!IsMarketOrderType(OrderType()))
         continue;
      if(only_symbol != "" && OrderSymbol() != only_symbol)
         continue;

      double profit = OrderProfit() + OrderSwap() + OrderCommission();
      bool matches = (filter == "all") ||
                      (filter == "profit" && profit > 0) ||
                      (filter == "loss"   && profit < 0);
      if(!matches)
         continue;

      int n = ArraySize(tickets);
      ArrayResize(tickets, n + 1);
      tickets[n] = OrderTicket();
   }

   for(int i = 0; i < ArraySize(tickets); i++)
   {
      if(!CloseOrderTicket(tickets[i]))
      {
         all_ok = false;
         last_error = LastOrderError();
      }
      else
         closed_count++;
   }

   Print("SocketBridgeEA: close_all filter=", filter,
         " matched=", ArraySize(tickets),
         " closed=", closed_count);
   SendOrderResult(id, all_ok, 0, last_error, closed_count);
}

void HandleModifyPosition(CJson &msg)
{
   string id     = msg.GetString("id");
   int    ticket = (int)msg.GetInt("ticket");
   double sl     = msg.GetDouble("sl", 0.0);
   double tp     = msg.GetDouble("tp", 0.0);

   bool ok = false;
   if(OrderSelect(ticket, SELECT_BY_TICKET))
      ok = OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrNONE);
   SendOrderResult(id, ok, ticket, ok ? "" : LastOrderError());
}

void HandleSetMagic(CJson &msg)
{
   string id = msg.GetString("id");
   g_magic = (int)msg.GetInt("magic", g_magic);
   SendOrderResult(id, true, 0, "");
}

int StringToPeriod(const string tf)
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

void HandleGetHistory(CJson &msg)
{
   string id      = msg.GetString("id");
   string symbol  = msg.GetString("symbol", Symbol());
   string tf_str  = msg.GetString("timeframe", "M1");
   int    count   = (int)msg.GetInt("count", 1000);
   int    tf      = StringToPeriod(tf_str);

   int available = iBars(symbol, tf);
   if(available < 1)
      available = 0;
   int copied = MathMin(count, available);

   CJson begin;
   begin.AddString("type", "history_begin");
   begin.AddString("id", id);
   begin.AddString("symbol", symbol);
   begin.AddString("timeframe", tf_str);
   begin.AddInt("count", copied);
   g_socket.Send(begin.Serialize() + "\n");

   int digits = (int)MarketInfo(symbol, MODE_DIGITS);
   for(int i = 0; i < copied; i++)
   {
      int shift = copied - 1 - i;
      CJson bar;
      bar.AddString("type", "bar");
      bar.AddString("id", id);
      bar.AddInt("time", (long)iTime(symbol, tf, shift));
      bar.AddDouble("open", iOpen(symbol, tf, shift), digits);
      bar.AddDouble("high", iHigh(symbol, tf, shift), digits);
      bar.AddDouble("low", iLow(symbol, tf, shift), digits);
      bar.AddDouble("close", iClose(symbol, tf, shift), digits);
      bar.AddInt("tick_volume", (long)iVolume(symbol, tf, shift));
      bar.AddInt("spread", 0);
      g_socket.Send(bar.Serialize() + "\n");
   }

   CJson end;
   end.AddString("type", "history_end");
   end.AddString("id", id);
   g_socket.Send(end.Serialize() + "\n");
}

void HandleWatchSymbol(CJson &msg)
{
   string symbol = msg.GetString("symbol", "");
   if(symbol != "")
      SymbolSelect(symbol, true);
}

void SendPositionsSnapshot()
{
   int total = CountOpenMarketOrders();

   CJson begin;
   begin.AddString("type", "positions_begin");
   begin.AddInt("count", total);
   g_socket.Send(begin.Serialize() + "\n");

   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(!IsMarketOrderType(OrderType()))
         continue;

      string symbol = OrderSymbol();
      int    digits = (int)MarketInfo(symbol, MODE_DIGITS);
      bool   is_buy = (OrderType() == OP_BUY);

      CJson pos;
      pos.AddString("type", "position");
      pos.AddInt("ticket", OrderTicket());
      pos.AddString("symbol", symbol);
      pos.AddString("side", is_buy ? "BUY" : "SELL");
      pos.AddDouble("volume", OrderLots(), 2);
      pos.AddDouble("price_open", OrderOpenPrice(), digits);
      pos.AddDouble("sl", OrderStopLoss(), digits);
      pos.AddDouble("tp", OrderTakeProfit(), digits);
      pos.AddDouble("profit", OrderProfit(), 2);
      pos.AddDouble("swap", OrderSwap(), 2);
      pos.AddInt("time_open", (long)OrderOpenTime());
      pos.AddInt("magic", OrderMagicNumber());
      pos.AddString("comment", OrderComment());
      g_socket.Send(pos.Serialize() + "\n");
   }

   CJson end;
   end.AddString("type", "positions_end");
   g_socket.Send(end.Serialize() + "\n");
}

void SendAccountInfo()
{
   double margin = AccountMargin();
   double margin_level = (margin > 0.0) ? (AccountEquity() / margin * 100.0) : 0.0;

   CJson msg;
   msg.AddString("type", "account");
   msg.AddDouble("balance", AccountBalance(), 2);
   msg.AddDouble("equity", AccountEquity(), 2);
   msg.AddDouble("margin", margin, 2);
   msg.AddDouble("margin_free", AccountFreeMargin(), 2);
   msg.AddDouble("margin_level", margin_level, 2);
   msg.AddString("currency", AccountCurrency());
   msg.AddInt("leverage", AccountLeverage());
   msg.AddInt("magic", g_magic);
   g_socket.Send(msg.Serialize() + "\n");
}

void SendClosedOrderFromHistory(const int index)
{
   if(!OrderSelect(index, SELECT_BY_POS, MODE_HISTORY))
      return;
   if(!IsMarketOrderType(OrderType()))
      return;
   if(OrderCloseTime() == 0)
      return;

   string symbol = OrderSymbol();
   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);
   string side = (OrderType() == OP_BUY) ? "BUY" : "SELL";

   CJson msg;
   msg.AddString("type", "deal_closed");
   msg.AddInt("ticket", OrderTicket());
   msg.AddString("symbol", symbol);
   msg.AddString("side", side);
   msg.AddDouble("volume", OrderLots(), 2);
   msg.AddDouble("price_open", OrderOpenPrice(), digits);
   msg.AddDouble("price_close", OrderClosePrice(), digits);
   msg.AddDouble("profit", OrderProfit(), 2);
   msg.AddDouble("swap", OrderSwap(), 2);
   msg.AddDouble("commission", OrderCommission(), 2);
   msg.AddInt("time_open", (long)OrderOpenTime());
   msg.AddInt("time_close", (long)OrderCloseTime());
   g_socket.Send(msg.Serialize() + "\n");
}

void SyncClosedDeals()
{
   int total = OrdersHistoryTotal();
   if(total <= g_last_history_total)
      return;

   for(int i = g_last_history_total; i < total; i++)
      SendClosedOrderFromHistory(i);

   g_last_history_total = total;
}
