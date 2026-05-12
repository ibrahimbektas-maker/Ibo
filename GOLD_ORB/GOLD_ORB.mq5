//+------------------------------------------------------------------+
//|                                               MQL5 Practice1.mq5 |
//|                                              Playground Inc 2021 |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Playground Inc 2021"
#property link      "https://www.mql5.com"
#property version   "1.10"

/*
Author: Ulysses O. Andulte
Date Created: 10/26/2022
*/


//Include Files
#include "Include\Trade.mqh"
#include "Include\TrailingStops.mqh"
#include "Include\price_action.mqh"
#include "Include\Indicators.mqh"
#include "Include\TradeVirtual.mqh"
#include "Include\TrailingStopsVirtual.mqh"
#include "Include\MoneyManagement.mqh"
#include "Include\Math\Stat\Normal.mqh"
#include "Include\RiskManagement.mqh"



//Input Variables
input group  "SymBolInformation"
input int StartOfTradingHour_ServerTime = 1;

input group  "Trade Management"
input int TakeProfit =1200;
input int StopLoss =400;
input int MaxTradePerDay =2;
input bool LongPosition = true;
input bool ShortPosition = true;
input ulong MagicNumber = 20221026;

input group "Trail Management"
input bool EnableTrail=true;

input group "Risk Management"
input double MaxEquityDrawdownPercent = 10;
input double MaxRiskPerTradePercent = 1;
input double FixedVolume = 0.1;


input group "Advanced Equity Monitoring Module"
input bool SlopeDetection = false;
input int LossStreakLimit = 0;

input group "Indicators"
input int PriceActionORB_CandleComposition = 3;


//Global Variables
bool execute_trade;
double capital;
int indicator_2;
bool indicator_3;
double TradeVolume = FixedVolume;


//Class objects

//Trade management Module
//++++++++++++++++++++++++
CTrade trade; //a class for executing orders on the server
CTrailing trail; //a class for trail stop

//Indicators Module
//++++++++++++++++++++++++
Price_Action pa;// an indicator class for price action
CiMA MA100; //an indicator class for moving averages


//Virtual Trading Environment Module
//++++++++++++++++++++++++++++++++++++
CTradeVirtual tradevirtual;// a class for executing orders on virtual trade environment
CTrailingVirtual trailvirtual; //a class for trail stop virtual
VirtualTradeInfo VTrade; //a class for storing virtual information: details on  position, deals and closed trades




//Working Code
//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
// Event handler: Initialization
int OnInit()
  {
   // Hedging mode required: the EA opens long/short on the same symbol without closing.
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      Alert("GOLD_ORB requires a hedging account. Current account is netting.");
      return(INIT_FAILED);
     }

   if(StopLoss <= 0)
     {
      Alert("GOLD_ORB: StopLoss must be > 0 (used by money management and SL placement).");
      return(INIT_PARAMETERS_INCORRECT);
     }

   trade.MagicNumber(MagicNumber);

   //Initialize Price action object for default or user input
   pa.Init();
   pa.candle_composition = PriceActionORB_CandleComposition;
   pa.trades_per_day = MaxTradePerDay;
   pa.StartOfTradinghour_servertime = StartOfTradingHour_ServerTime;

   // Initialize MA once; using PRICE_CLOSE on the current symbol/timeframe.
   MA100.Init(_Symbol,PERIOD_CURRENT,100,0,MODE_SMA,PRICE_CLOSE);

   //Initialize virtual trade environment
   tradevirtual.Init(VTrade);

   //******************************
   //Extra Variables (test cases)
   execute_trade = true;
   capital = AccountInfoDouble(ACCOUNT_EQUITY);
   return(INIT_SUCCEEDED);
  }


//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   MA100.Release();
   ObjectDelete(0,"My Line");
   ObjectDelete(0,"My Line1");
   ObjectDelete(0,"My Line2");
   ObjectDelete(0,"My Line3");
  }


//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
// Event handler: Execute each tick that will arrive from the server
void OnTick()
  {


   RiskManagementModule();
   if(EnableTrail) TrailModule();
   if(pa.new_candle_check2())
     {
      IndicatorModule();
      ExecuteOrders();
     }
  }

//+------------------------------------------------------------------+
//|                                                                  |
//+------------------------------------------------------------------+
//End of Program




/////////////////////////////////////////////////////////////////////
//                     Functions
/////////////////////////////////////////////////////////////////////

//+------------------------------------------------------------------+
//| //Risk Management Module
//|
//|  **MonitoringVirtualPosition - Monitors and Closes virtual position
//|                                and update virtualtrade information
//|

//+------------------------------------------------------------------+
void RiskManagementModule(void)

  {

   //Virtual Equity Monitoring
   MonitorVirtualPostion(VTrade);

   //Equity-based drawdown trail: stops execution once max drawdown is hit (uses equity, not just balance, so floating PnL counts).
   if(MaxEquityDrawdownPercent!=0)
     {
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      if(equity>capital)
         capital = equity;
      double dd_pct = 100.0 * ((equity - capital) / capital);
      if(dd_pct < -MaxEquityDrawdownPercent)
        {
         PrintFormat("Max drawdown hit: %.2f%% (limit %.2f%%). Real trading disabled.", dd_pct, MaxEquityDrawdownPercent);
         execute_trade = false;
        }
     }


   //This module will detect if Lossing streak ended depending on the input integer and if equity is recovering, upward
   if(SlopeDetection || LossStreakLimit!=0)
     {
      bool LossStreak_flag = (LossStreakLimit > 0) ? IsLossStreak(VTrade, LossStreakLimit) : false;
      bool Slope_Equity_Flag = CheckSlope(VTrade,12); // Slope Equity Monitoring

      if(LossStreak_flag)
         execute_trade = false;
      if(Slope_Equity_Flag == true && LossStreak_flag == false)
         execute_trade = true;
     }

   //Dynamic Position Sizing Relative to port size, or FixedVolume for default if no input in MaxRiskPerTradePercent
   TradeVolume = MoneyManagement(_Symbol,FixedVolume,MaxRiskPerTradePercent,StopLoss);


  }



//+------------------------------------------------------------------+
//| //Trail Stop Module                                              |
//+------------------------------------------------------------------+
void TrailModule(void)

  {
   //RealPort Trail Module: loop on positions owned by this EA only (magic + symbol).
   int total_positions = PositionsTotal();
   for(int i = total_positions - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      trail.TrailingStop(ticket,700,100,10); //  set 700 trail stop below sa TP then min profit is  100 for secure profits, 10 is the step size
     }


   //Virtual Port Trail Module, will loop on all open positions to check if trail is hit
   int total_positions_virtual = ArraySize(VTrade.position);
   for(int j = 0; j < total_positions_virtual; j++)
     {
      trailvirtual.TrailingStop(VTrade,j,700,100,10); //  set 700 trail stop below sa TP then min profit is  100 for secure profits, 10 is the step size
     }
  }




//+------------------------------------------------------------------+
//| //Indicators Module                                              |
//+------------------------------------------------------------------+
void IndicatorModule(void)
  {


   //Price action indicator
   //outputs "11" for Long position signal and "10" for Short position signal
   indicator_2 = pa.Open_Range_Breakout();


   // Moving Average filter (currently unused as signal, kept for future strategies)
   double ma = MA100.Main(0);
   indicator_3 = (iClose(_Symbol,_Period,1) > ma);

  }



//+------------------------------------------------------------------+
//| //Trade Execution Module                                          |
//+------------------------------------------------------------------+

void ExecuteOrders(void)

  {

//Buy/Sell Order: //Execute buy/sell orders given the indicators and user inputs if its enabled
   if(indicator_2 == 11 && LongPosition)
     {

      tradevirtual.Buy(VTrade,_Symbol,TradeVolume,StopLoss,TakeProfit);
      if(execute_trade)//if this is false then the equity hits its maximum draw down as input by the user
         trade.Buy(_Symbol,TradeVolume,StopLoss,TakeProfit);
     }


   if(indicator_2 == 10 && ShortPosition)
     {
      tradevirtual.Sell(VTrade,_Symbol,TradeVolume,StopLoss,TakeProfit);
      if(execute_trade)//if this is false then the equity hits its maximum draw down as input by the user
         trade.Sell(_Symbol,TradeVolume,StopLoss,TakeProfit);

     }

  }


//+------------------------------------------------------------------+
