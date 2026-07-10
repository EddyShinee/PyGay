//+------------------------------------------------------------------+
//|                                              SocketBridgeEA.mq5 |
//| Bridges MT5 <-> Python over TCP (Python runs the server).        |
//| - OnTick():  pushes the latest tick to Python immediately.       |
//| - OnTimer(): polls the socket for incoming messages (non-        |
//|              blocking) and reconnects if the link drops.         |
//| To extend: add a new "type" branch in HandleMessage().           |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>
#include <Socket.mqh>
#include <Json.mqh>

input string InpHost        = "127.0.0.1";
input int    InpPort        = 9090;
input int    InpPollMs      = 20;    // socket poll interval (ms) - lower = lower latency
input int    InpReconnectS  = 3;     // reconnect retry interval (s) while disconnected

CSocket  g_socket;
CTrade   g_trade;
string   g_rx_buffer = "";
datetime g_last_reconnect_attempt = 0;

//+------------------------------------------------------------------+
int OnInit()
{
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
   msg.AddInt("time", (long)tick.time);

   g_socket.Send(msg.Serialize() + "\n");
}

//+------------------------------------------------------------------+
//| Poll the socket for incoming data; reconnect if disconnected     |
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
}

void TryConnect()
{
   g_last_reconnect_attempt = TimeCurrent();
   if(g_socket.Connect(InpHost, InpPort))
      Print("SocketBridgeEA: connected to ", InpHost, ":", InpPort);
   else
      Print("SocketBridgeEA: connect failed, will retry");
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
   {
      HandleSignal(msg);
      return;
   }

   Print("SocketBridgeEA: unknown message type: ", type);
}

//+------------------------------------------------------------------+
//| Example trading action - extend with your own order logic        |
//+------------------------------------------------------------------+
void HandleSignal(CJson &msg)
{
   string action = msg.GetString("action");
   string symbol = msg.GetString("symbol", _Symbol);
   double volume = msg.GetDouble("volume", 0.01);

   if(action == "BUY")
      g_trade.Buy(volume, symbol);
   else if(action == "SELL")
      g_trade.Sell(volume, symbol);
   else if(action == "CLOSE")
      g_trade.PositionClose(symbol);
   else
      Print("SocketBridgeEA: unknown action: ", action);
}
