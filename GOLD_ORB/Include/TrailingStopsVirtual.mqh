//+------------------------------------------------------------------+
//|                                            TradeStopsVirtual.mqh |
//|                                              Playground Inc 2021 |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Playground Inc 2021"
#property link      "https://www.mql5.com"
#property version   "1.00"



#include "errordescription.mqh"
#include "TradeVirtual.mqh"


//+------------------------------------------------------------------+
//| Trailing Stop Class                                              |
//+------------------------------------------------------------------+


//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
class CTrailingVirtual
  {
protected:
   MqlTradeRequest   request;

public:
   MqlTradeResult    result;



   bool              TrailingStop(VirtualTradeInfo &vTrade, int index,  int pTrailPoints, int pMinProfit = 0, int pStep = 10);
   bool              TrailingStop(VirtualTradeInfo &vTrade, int index,  double pTrailPrice, int pMinProfit = 0, int pStep = 10);


  };




// Trailing stop (points, hedging orders)
bool CTrailingVirtual::TrailingStop(VirtualTradeInfo &vTrade,int index, int pTrailPoints,int pMinProfit=0,int pStep=10)
  {
   if(pTrailPoints > 0)
     {


      string posType = vTrade.position[index].type;
      double currentStop = vTrade.position[index].sl;
      double openPrice = vTrade.position[index].price;
      string symbol = vTrade.position[index].symbol;

      double point = SymbolInfoDouble(symbol,SYMBOL_POINT);
      int digits = (int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);

      if(pStep < 10)
         pStep = 10;
      double step = pStep * point;

      double minProfit = pMinProfit * point;
      double trailStop = pTrailPoints * point;
      currentStop = NormalizeDouble(currentStop,digits);

      double trailStopPrice;
      double currentProfit;





      if(posType == "long")
        {
         trailStopPrice = SymbolInfoDouble(symbol,SYMBOL_BID) - trailStop;
         trailStopPrice = NormalizeDouble(trailStopPrice,digits);
         currentProfit = SymbolInfoDouble(symbol,SYMBOL_BID) - openPrice;

         if(trailStopPrice > currentStop + step && currentProfit >= minProfit)
           {
            vTrade.position[index].sl = trailStopPrice;
            vTrade.position[index].tp = 0;
            return(true);
           }
         else

            return(false);


        }


      else
         if(posType == "short")
           {
            trailStopPrice = SymbolInfoDouble(symbol,SYMBOL_ASK) + trailStop;
            trailStopPrice = NormalizeDouble(trailStopPrice,digits);
            currentProfit = openPrice - SymbolInfoDouble(symbol,SYMBOL_ASK);

            if((trailStopPrice < currentStop - step || currentStop == 0) && currentProfit >= minProfit)
              {
               vTrade.position[index].sl = trailStopPrice;
               vTrade.position[index].tp = 0;
               return(true);
              }
            else
               return(false);

           }
         else
            return(false);
     }

   else
      return(false);


  }


// Trailing stop (absolute price target, virtual positions).
// Reads state from vTrade.position[index] (the previous version read PositionGetXxx,
// which referred to the real account and made this overload unusable).
bool CTrailingVirtual::TrailingStop(VirtualTradeInfo &vTrade,int index, double pTrailPrice,int pMinProfit=0,int pStep=10)
  {
   if(pTrailPrice <= 0) return(false);

   string posType    = vTrade.position[index].type;
   double currentStop = vTrade.position[index].sl;
   double openPrice  = vTrade.position[index].price;
   string symbol     = vTrade.position[index].symbol;

   double point  = SymbolInfoDouble(symbol,SYMBOL_POINT);
   int    digits = (int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);

   if(pStep < 10) pStep = 10;
   double step      = pStep * point;
   double minProfit = pMinProfit * point;

   currentStop = NormalizeDouble(currentStop,digits);
   pTrailPrice = NormalizeDouble(pTrailPrice,digits);

   if(posType == "long")
     {
      double bid = SymbolInfoDouble(symbol,SYMBOL_BID);
      double currentProfit = bid - openPrice;
      if(pTrailPrice > currentStop + step && currentProfit >= minProfit)
        {
         vTrade.position[index].sl = pTrailPrice;
         vTrade.position[index].tp = 0;
         return(true);
        }
      return(false);
     }

   if(posType == "short")
     {
      double ask = SymbolInfoDouble(symbol,SYMBOL_ASK);
      double currentProfit = openPrice - ask;
      if((pTrailPrice < currentStop - step || currentStop == 0) && currentProfit >= minProfit)
        {
         vTrade.position[index].sl = pTrailPrice;
         vTrade.position[index].tp = 0;
         return(true);
        }
      return(false);
     }

   return(false);
  }




//+------------------------------------------------------------------+

//+------------------------------------------------------------------+



//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
