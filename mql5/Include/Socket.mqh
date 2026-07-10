//+------------------------------------------------------------------+
//|                                                       Socket.mqh |
//| Thin non-blocking TCP client wrapper around Winsock (ws2_32.dll).|
//| MQL5 has no built-in socket API, so this is the standard way EAs |
//| talk raw TCP. Windows only. Requires "Allow DLL imports" enabled |
//| for the EA (Tools > Options > Expert Advisors, and the checkbox  |
//| in the EA's own properties dialog).                              |
//+------------------------------------------------------------------+
#property strict

#import "ws2_32.dll"
int    WSAStartup(ushort wVersionRequested, uchar &lpWSAData[]);
int    WSACleanup();
int    WSAGetLastError();
int    socket(int af, int type, int protocol);
int    connect(int s, uchar &name[], int namelen);
int    send(int s, uchar &buf[], int len, int flags);
int    recv(int s, uchar &buf[], int len, int flags);
int    closesocket(int s);
uint   inet_addr(uchar &cp[]);
ushort htons(ushort hostshort);
int    ioctlsocket(int s, uint cmd, uint &argp[]);
#import

#define AF_INET        2
#define SOCK_STREAM    1
#define IPPROTO_TCP    6
#define FIONBIO        0x8004667E
#define WSAEWOULDBLOCK 10035

class CSocket
{
private:
   int         m_handle;
   bool        m_connected;
   static int  s_wsa_refcount;

   static bool WsaStartup()
   {
      if(s_wsa_refcount == 0)
      {
         uchar wsa_data[512];
         ArrayInitialize(wsa_data, 0);
         if(WSAStartup(0x0202, wsa_data) != 0)
            return false;
      }
      s_wsa_refcount++;
      return true;
   }

   static void WsaCleanupRef()
   {
      s_wsa_refcount--;
      if(s_wsa_refcount <= 0)
      {
         s_wsa_refcount = 0;
         WSACleanup();
      }
   }

   void PackSockAddr(const string host, const int port, uchar &addr[])
   {
      ArrayResize(addr, 16);
      ArrayInitialize(addr, 0);
      addr[0] = AF_INET;
      addr[1] = 0;

      ushort net_port = htons((ushort)port);
      addr[2] = (uchar)(net_port & 0xFF);
      addr[3] = (uchar)((net_port >> 8) & 0xFF);

      uchar host_bytes[];
      StringToCharArray(host, host_bytes); // ANSI, null-terminated
      uint ip = inet_addr(host_bytes);
      addr[4] = (uchar)(ip & 0xFF);
      addr[5] = (uchar)((ip >> 8) & 0xFF);
      addr[6] = (uchar)((ip >> 16) & 0xFF);
      addr[7] = (uchar)((ip >> 24) & 0xFF);
   }

public:
            CSocket() : m_handle(-1), m_connected(false) {}
           ~CSocket() { Close(); }

   bool     IsConnected() const { return m_connected; }

   bool     Connect(const string host, const int port)
   {
      if(!WsaStartup())
      {
         Print("CSocket: WSAStartup failed");
         return false;
      }

      m_handle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
      if(m_handle < 0)
      {
         Print("CSocket: socket() failed, error=", WSAGetLastError());
         WsaCleanupRef();
         return false;
      }

      uchar addr[];
      PackSockAddr(host, port, addr);
      if(connect(m_handle, addr, ArraySize(addr)) != 0)
      {
         Print("CSocket: connect() failed, error=", WSAGetLastError());
         closesocket(m_handle);
         m_handle = -1;
         WsaCleanupRef();
         return false;
      }

      SetNonBlocking(true);
      m_connected = true;
      return true;
   }

   void     SetNonBlocking(const bool enable)
   {
      uint mode[1];
      mode[0] = enable ? 1 : 0;
      ioctlsocket(m_handle, FIONBIO, mode);
   }

   //--- returns false only on a real error/close; WOULDBLOCK is not an error
   bool     Send(const string text)
   {
      if(!m_connected)
         return false;
      uchar buf[];
      int total = StringToCharArray(text, buf, 0, -1, CP_UTF8) - 1; // drop the trailing \0
      int offset = 0;
      while(offset < total)
      {
         uchar chunk[];
         ArrayCopy(chunk, buf, 0, offset, total - offset);
         int sent = send(m_handle, chunk, total - offset, 0);
         if(sent < 0)
         {
            int err = WSAGetLastError();
            if(err == WSAEWOULDBLOCK)
               continue; // socket buffer momentarily full, keep trying
            m_connected = false;
            return false;
         }
         offset += sent;
      }
      return true;
   }

   //--- appends any available bytes to 'out'; returns false only on disconnect
   bool     Receive(string &out)
   {
      if(!m_connected)
         return false;
      uchar buf[4096];
      int received = recv(m_handle, buf, 4096, 0);
      if(received > 0)
      {
         out += CharArrayToString(buf, 0, received, CP_UTF8);
         return true;
      }
      if(received == 0)
      {
         m_connected = false; // remote closed the connection
         return false;
      }
      int err = WSAGetLastError();
      if(err == WSAEWOULDBLOCK)
         return true; // no data available right now, not an error
      m_connected = false;
      return false;
   }

   void     Close()
   {
      if(m_handle >= 0)
      {
         closesocket(m_handle);
         m_handle = -1;
         WsaCleanupRef();
      }
      m_connected = false;
   }
};

int CSocket::s_wsa_refcount = 0;
