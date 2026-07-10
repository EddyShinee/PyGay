//+------------------------------------------------------------------+
//|                                                         Json.mqh |
//| Minimal flat JSON object: string/number/bool values, no nesting. |
//| Enough for tick/signal/order messages. If you need arrays or     |
//| nested objects later, extend Parse()/Serialize() - the key/value |
//| storage below stays the same.                                    |
//+------------------------------------------------------------------+
#property strict

class CJson
{
private:
   string m_keys[];
   string m_values[]; // raw text as it appears in JSON (quotes kept for strings)

   int    KeyIndex(const string key) const
   {
      for(int i = 0; i < ArraySize(m_keys); i++)
         if(m_keys[i] == key)
            return i;
      return -1;
   }

   void   Put(const string key, const string raw_value)
   {
      int idx = KeyIndex(key);
      if(idx < 0)
      {
         idx = ArraySize(m_keys);
         ArrayResize(m_keys, idx + 1);
         ArrayResize(m_values, idx + 1);
         m_keys[idx] = key;
      }
      m_values[idx] = raw_value;
   }

   static string Trim(const string s)
   {
      string r = s;
      StringTrimLeft(r);
      StringTrimRight(r);
      return r;
   }

   static string Unquote(const string s)
   {
      string r = Trim(s);
      int len = StringLen(r);
      if(len >= 2 && StringGetCharacter(r, 0) == '"' && StringGetCharacter(r, len - 1) == '"')
      {
         r = StringSubstr(r, 1, len - 2);
         StringReplace(r, "\\\"", "\"");
         StringReplace(r, "\\\\", "\\");
         StringReplace(r, "\\n", "\n");
      }
      return r;
   }

public:
   void Clear()
   {
      ArrayResize(m_keys, 0);
      ArrayResize(m_values, 0);
   }

   //--- building ------------------------------------------------------
   void AddString(const string key, const string value)
   {
      string escaped = value;
      StringReplace(escaped, "\\", "\\\\");
      StringReplace(escaped, "\"", "\\\"");
      Put(key, "\"" + escaped + "\"");
   }

   void AddDouble(const string key, const double value, const int digits = 5)
   {
      Put(key, DoubleToString(value, digits));
   }

   void AddInt(const string key, const long value)
   {
      Put(key, IntegerToString(value));
   }

   void AddBool(const string key, const bool value)
   {
      Put(key, value ? "true" : "false");
   }

   string Serialize() const
   {
      string out = "{";
      for(int i = 0; i < ArraySize(m_keys); i++)
      {
         if(i > 0)
            out += ",";
         out += "\"" + m_keys[i] + "\":" + m_values[i];
      }
      out += "}";
      return out;
   }

   //--- parsing ---------------------------------------------------------
   // Splits the top-level "key":value pairs of a flat JSON object,
   // respecting quoted strings so commas/braces inside strings don't
   // confuse the splitter.
   bool Parse(const string json_text)
   {
      Clear();
      string body = Trim(json_text);
      int len = StringLen(body);
      if(len < 2 || StringGetCharacter(body, 0) != '{' || StringGetCharacter(body, len - 1) != '}')
         return false;
      body = StringSubstr(body, 1, len - 2);

      int depth = 0;
      bool in_string = false;
      int start = 0;
      string parts[];
      int part_count = 0;
      int body_len = StringLen(body);

      for(int i = 0; i < body_len; i++)
      {
         ushort c = StringGetCharacter(body, i);
         if(c == '"' && (i == 0 || StringGetCharacter(body, i - 1) != '\\'))
            in_string = !in_string;
         else if(!in_string && (c == '{' || c == '['))
            depth++;
         else if(!in_string && (c == '}' || c == ']'))
            depth--;
         else if(!in_string && depth == 0 && c == ',')
         {
            ArrayResize(parts, part_count + 1);
            parts[part_count++] = StringSubstr(body, start, i - start);
            start = i + 1;
         }
      }
      ArrayResize(parts, part_count + 1);
      parts[part_count++] = StringSubstr(body, start, body_len - start);

      for(int i = 0; i < part_count; i++)
      {
         string pair = Trim(parts[i]);
         if(pair == "")
            continue;
         int colon = StringFind(pair, ":");
         if(colon < 0)
            continue;
         string key = Unquote(StringSubstr(pair, 0, colon));
         string value = Trim(StringSubstr(pair, colon + 1));
         Put(key, value);
      }
      return true;
   }

   bool Has(const string key) const { return KeyIndex(key) >= 0; }

   string GetString(const string key, const string def = "") const
   {
      int idx = KeyIndex(key);
      if(idx < 0)
         return def;
      return Unquote(m_values[idx]);
   }

   double GetDouble(const string key, const double def = 0.0) const
   {
      int idx = KeyIndex(key);
      if(idx < 0)
         return def;
      return StringToDouble(m_values[idx]);
   }

   long GetInt(const string key, const long def = 0) const
   {
      int idx = KeyIndex(key);
      if(idx < 0)
         return def;
      return StringToInteger(m_values[idx]);
   }

   bool GetBool(const string key, const bool def = false) const
   {
      int idx = KeyIndex(key);
      if(idx < 0)
         return def;
      return m_values[idx] == "true";
   }
};
