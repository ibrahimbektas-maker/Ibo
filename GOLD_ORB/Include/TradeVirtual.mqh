//+------------------------------------------------------------------+
//|                                                 TradeVirtual.mqh |
//|                                              Playground Inc 2021 |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Playground Inc 2021"
#property link      "https://www.mql5.com"
#property version   "1.10"

#define MAX_RETRIES 5  // Max retries on error
#define RETRY_DELAY 3000 // Retry delay in ms

#include "errordescription.mqh"


struct CandleInfo
  {
   double            body_high;
   double            body_low;
   double            wick_high;
   double            wick_low;
   bool              direction;  // 1 - for bullish , 0 - for bearish

  };



struct PositionInfo
  {
   string            symbol;
   int               ticket;
   datetime          time;
   string            type;
   double            volume;
   double            price;
   double            sl;
   double            tp;
   double            profit;
   string            comment;

  };


struct DealsInfo
  {

   datetime          time;
   int               dealno;
   string            symbol;
   string            type;
   string            direction;
   double            volume;
   double            price;
   double            commission;
   double            swap;
   double            profit;
   double            balance;
   double            realportbalance;
   string            comment;
   double            container;

  };


struct CloseTradeInfo
  {
   datetime          timeopen;
   datetime          timeclose;
   string            symbol;
   string            type;
   double            priceopen;
   double            priceclose;
   double            volume;
   double            commission;
   double            swap;
   double            profit;
   string            result;
   double            balance;


  };



struct VirtualTradeInfo

  {


   PositionInfo      position[];
   DealsInfo         deals[];
   CloseTradeInfo    closetrades[];




  };




// Compute realized P&L in account currency using tick value/size.
// (The previous formula multiplied by ACCOUNT_LEVERAGE, which has nothing to do
// with profit conversion and overstated the virtual P&L by orders of magnitude.)
double VirtualProfit(string pSymbol, string posType, double openPrice, double closePrice, double volume)
  {
   double tickSize  = SymbolInfoDouble(pSymbol, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(pSymbol, SYMBOL_TRADE_TICK_VALUE);
   if(tickSize <= 0.0) return(0.0);

   double priceDiff = (posType == "long") ? (closePrice - openPrice) : (openPrice - closePrice);
   return(priceDiff / tickSize * tickValue * volume);
  }



//+------------------------------------------------------------------+
//| CTrade Class - Open, Close and Modify Orders                                                           |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
class CTradeVirtual
  {
protected:

   //create an object "request" inheriting the class MqlTradeRequest
   MqlTradeRequest   request;

   bool              OpenPosition(VirtualTradeInfo &vTrade,string pSymbol, ENUM_ORDER_TYPE pType, double pVolume, double pStop = 0, double pProfit = 0, string pComment = NULL);
   bool              OpenPending(VirtualTradeInfo &vTrade,int index, string pType);

public:
   MqlTradeResult    result;

   bool              Buy(VirtualTradeInfo &vTrade, string pSymbol, double pVolume, double pStop = 0, double pProfit = 0, string pComment = NULL);
   bool              Sell(VirtualTradeInfo &vTrade, string pSymbol, double pVolume, double pStop = 0, double pProfit = 0, string pComment = NULL);
   bool              StopLoss(VirtualTradeInfo &vTrade,int index);
   bool              TakeProfit(VirtualTradeInfo &vTrade,int index);
   void              Init(VirtualTradeInfo &VTrade);
  };





// Open position
// 110 - 120 Code explanation on "Expert Advisor Programming for MetaTrader5""
bool CTradeVirtual::OpenPosition(VirtualTradeInfo &vTrade, string pSymbol, ENUM_ORDER_TYPE pType, double pVolume, double pStop = 0, double pProfit = 0, string pComment = NULL)
  {

   //Position Info
   //Construct Array for new position
   ArrayResize(vTrade.position,ArraySize(vTrade.position)+1);
   int position_index = ArraySize(vTrade.position)-1;

   vTrade.position[position_index].symbol = pSymbol;
   vTrade.position[position_index].time = TimeCurrent();
   vTrade.position[position_index].volume = pVolume;



   if(pType == ORDER_TYPE_BUY)
     {
      vTrade.position[position_index].type = "long";
      vTrade.position[position_index].price = SymbolInfoDouble(pSymbol,SYMBOL_ASK);
      if(pStop > 0)
         vTrade.position[position_index].sl = vTrade.position[position_index].price - (pStop * _Point);
      if(pProfit > 0)
         vTrade.position[position_index].tp = vTrade.position[position_index].price + (pProfit * _Point);

     }


   if(pType == ORDER_TYPE_SELL)

     {
      vTrade.position[position_index].type = "short";
      vTrade.position[position_index].price = SymbolInfoDouble(pSymbol,SYMBOL_BID);
      if(pStop > 0)
         vTrade.position[position_index].sl = vTrade.position[position_index].price + (pStop * _Point);
      if(pProfit > 0)
         vTrade.position[position_index].tp = vTrade.position[position_index].price - (pProfit * _Point);

     }




//Deal Info
//Construct Array for new deal
   ArrayResize(vTrade.deals,ArraySize(vTrade.deals)+1);
   int deal_index = ArraySize(vTrade.deals)-1;

   vTrade.deals[deal_index].dealno = deal_index +1;
   vTrade.deals[deal_index].symbol = pSymbol;
   vTrade.deals[deal_index].time = TimeCurrent();
   vTrade.deals[deal_index].volume = pVolume;

   vTrade.deals[deal_index].commission = 0.0;
   vTrade.deals[deal_index].swap = 0.0;
   vTrade.deals[deal_index].profit = 0.0;

   vTrade.deals[deal_index].balance =vTrade.deals[deal_index-1].balance + ((vTrade.deals[deal_index].commission)+(vTrade.deals[deal_index].swap)+(vTrade.deals[deal_index].profit));



   if(pType == ORDER_TYPE_BUY)
     {
      vTrade.deals[deal_index].type = "buy";
      vTrade.deals[deal_index].direction = "in";
      vTrade.deals[deal_index].price = SymbolInfoDouble(pSymbol,SYMBOL_ASK);


     }


   if(pType == ORDER_TYPE_SELL)

     {
      vTrade.deals[deal_index].type = "sell";
      vTrade.deals[deal_index].direction = "in";
      vTrade.deals[deal_index].price = SymbolInfoDouble(pSymbol,SYMBOL_BID);

     }



   vTrade.deals[deal_index].realportbalance = AccountInfoDouble(ACCOUNT_BALANCE);
   return (true);


  }


// Close a virtual position at its SL or TP price (not at the live market quote).
bool CTradeVirtual::OpenPending(VirtualTradeInfo &vTrade,int index, string pType)
  {
   string pSymbol    = vTrade.position[index].symbol;
   string posType    = vTrade.position[index].type;
   double openPrice  = vTrade.position[index].price;
   double volume     = vTrade.position[index].volume;
   double closePrice = (pType == "tp") ? vTrade.position[index].tp : vTrade.position[index].sl;
   double profit     = VirtualProfit(pSymbol, posType, openPrice, closePrice, volume);

   // Deals row
   ArrayResize(vTrade.deals, ArraySize(vTrade.deals) + 1);
   int deal_index = ArraySize(vTrade.deals) - 1;

   vTrade.deals[deal_index].dealno     = deal_index + 1;
   vTrade.deals[deal_index].symbol     = pSymbol;
   vTrade.deals[deal_index].time       = TimeCurrent();
   vTrade.deals[deal_index].volume     = volume;
   vTrade.deals[deal_index].commission = 0.0;
   vTrade.deals[deal_index].swap       = 0.0;
   vTrade.deals[deal_index].profit     = profit;
   vTrade.deals[deal_index].price      = closePrice;
   vTrade.deals[deal_index].type       = (posType == "long") ? "sell" : "buy";
   vTrade.deals[deal_index].direction  = "out";
   vTrade.deals[deal_index].balance    = vTrade.deals[deal_index-1].balance + profit;

   // Closed trade row
   ArrayResize(vTrade.closetrades, ArraySize(vTrade.closetrades) + 1);
   int closedtrade_index = ArraySize(vTrade.closetrades) - 1;

   vTrade.closetrades[closedtrade_index].timeopen   = vTrade.position[index].time;
   vTrade.closetrades[closedtrade_index].timeclose  = TimeCurrent();
   vTrade.closetrades[closedtrade_index].priceopen  = openPrice;
   vTrade.closetrades[closedtrade_index].priceclose = closePrice;
   vTrade.closetrades[closedtrade_index].symbol     = pSymbol;
   vTrade.closetrades[closedtrade_index].volume     = volume;
   vTrade.closetrades[closedtrade_index].commission = 0.0;
   vTrade.closetrades[closedtrade_index].swap       = 0.0;
   vTrade.closetrades[closedtrade_index].profit     = profit;
   vTrade.closetrades[closedtrade_index].type       = posType;
   vTrade.closetrades[closedtrade_index].result     = (profit > 0) ? "WIN" : (profit < 0 ? "LOSS" : "BE");
   vTrade.closetrades[closedtrade_index].balance    = vTrade.closetrades[closedtrade_index-1].balance + profit;

   vTrade.deals[deal_index].realportbalance = AccountInfoDouble(ACCOUNT_BALANCE);
   return(true);
  }





//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+


// Trade opening shortcuts
bool CTradeVirtual::Buy(VirtualTradeInfo &vTrade, string pSymbol,double pVolume,double pStop=0.000000,double pProfit=0.000000,string pComment=NULL)
  {
   bool success = OpenPosition(vTrade, pSymbol,ORDER_TYPE_BUY,pVolume,pStop,pProfit,pComment);
   return(success);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool CTradeVirtual::Sell(VirtualTradeInfo &vTrade, string pSymbol,double pVolume,double pStop=0.000000,double pProfit=0.000000,string pComment=NULL)
  {
   bool success = OpenPosition(vTrade, pSymbol,ORDER_TYPE_SELL,pVolume,pStop,pProfit,pComment);
   return(success);
  }




//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool CTradeVirtual::StopLoss(VirtualTradeInfo &vTrade,int index)
  {
   bool success = OpenPending(vTrade, index, "sl");
   return(success);
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool CTradeVirtual::TakeProfit(VirtualTradeInfo &vTrade,int index)
  {
   bool success = OpenPending(vTrade, index, "tp");
   return(success);
  }
//+------------------------------------------------------------------+



//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void CTradeVirtual::Init(VirtualTradeInfo &VTrade)
  {

//Initialize Deals Container..
//Array Constructor for VTrade.deals...
   ArrayResize(VTrade.deals,ArraySize(VTrade.deals)+1);
   ArrayResize(VTrade.closetrades,ArraySize(VTrade.closetrades)+1);

//ArrayResize(VTrade.position,ArraySize(VTrade.position)+1);
//Populate Vtrade.deals Container
   VTrade.deals[0].time = TimeCurrent();
   VTrade.deals[0].dealno =  1;
   VTrade.deals[0].commission = 0.00;
   VTrade.deals[0].swap = 0.00;
   VTrade.deals[0].profit = AccountInfoDouble(ACCOUNT_BALANCE);
   VTrade.deals[0].balance = AccountInfoDouble(ACCOUNT_BALANCE);
   VTrade.deals[0].type = "balance";
   VTrade.closetrades[0].balance = AccountInfoDouble(ACCOUNT_BALANCE);



  }
//+------------------------------------------------------------------+
