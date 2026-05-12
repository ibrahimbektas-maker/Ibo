//+------------------------------------------------------------------+
//|                                                 TradeVirtual.mqh |
//|                                              Playground Inc 2021 |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Playground Inc 2021"
#property link      "https://www.mql5.com"
#property version   "1.00"






void MonitorVirtualPostion(VirtualTradeInfo &VTrade)

  {

   int i = 0;
   int total_positions = ArraySize(VTrade.position);
//printf(ArraySize(VTrade.position));


   if(total_positions!=0)
     {

      for(i=0 ; i <= total_positions -1; i++)

        {


         if(VTrade.position[i].type == "long" && SymbolInfoDouble(_Symbol,SYMBOL_BID) >= VTrade.position[i].tp &&VTrade.position[i].tp > 0)

           {

            //update deals
            tradevirtual.TakeProfit(VTrade,i);

            //update close trade
            //Update position
            VTrade.position[i].comment = "closed";
           }

         //Print("Stoploss = ",  VTrade.position[i].sl);
         //Print("Current_Price = ", SymbolInfoDouble(_Symbol,SYMBOL_BID));
         if(VTrade.position[i].type == "long" && SymbolInfoDouble(_Symbol,SYMBOL_BID) <= VTrade.position[i].sl)

           {

            //update deals
            tradevirtual.StopLoss(VTrade,i);

            //update close trade
            //Update position
            VTrade.position[i].comment = "closed";

           }


         if(VTrade.position[i].type == "short" && SymbolInfoDouble(_Symbol,SYMBOL_ASK) <= VTrade.position[i].tp && VTrade.position[i].tp > 0)

           {
            //update deals
            tradevirtual.TakeProfit(VTrade,i);

            //update close trade
            //Update position
            VTrade.position[i].comment = "closed";

           }


         if(VTrade.position[i].type == "short" && SymbolInfoDouble(_Symbol,SYMBOL_ASK) >= VTrade.position[i].sl)

           {

            //update deals
            tradevirtual.StopLoss(VTrade,i);


            //update close trade
            //Update position
            VTrade.position[i].comment = "closed";

           }

        }


      // Remove all positions marked "closed" in a single backward sweep.
      // (The previous do/while could infinite-loop when the array became empty.)
      for(int k = ArraySize(VTrade.position) - 1; k >= 0; k--)
        {
         if(VTrade.position[k].comment == "closed")
            ArrayRemove(VTrade.position, k, 1);
        }

     }

  }
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
bool CheckSlope(VirtualTradeInfo &VTrade, int offset_length)
//+------------------------------------------------------------------+
  {


   int deal_size = ArraySize(VTrade.deals);
   int index = deal_size -1;
//int offset_length = 5;

   if(deal_size > offset_length)
     {

      if((VTrade.deals[index].balance - VTrade.deals[index-(offset_length)].balance)>0)

        {


         return(true);
        }
      else
         return(false);
     }

   else
      return(false);

  }
//+------------------------------------------------------------------+
// Renamed from LossStreakCounter to avoid name collision with the input variable.
// Returns true if the last 'streak' closed trades were all losses.
// closetrades[0] is a sentinel populated by Init() with no result, so it is skipped.
bool IsLossStreak(VirtualTradeInfo &VTrade, int streak)
  {
   if(streak <= 0) return(false);

   int total = ArraySize(VTrade.closetrades);
   if(total - 1 < streak) return(false);  // not enough real closed trades

   for(int i = total - 1; i > total - 1 - streak; i--)
     {
      if(VTrade.closetrades[i].result != "LOSS")
         return(false);
     }
   return(true);
  }
//+------------------------------------------------------------------+

