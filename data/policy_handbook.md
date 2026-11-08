# Wacky Wheels Co. Operations Policy Handbook

This is the rulebook your agents must follow. The automated check applies exactly these rules.

## The company in one paragraph

Wacky Wheels Co. builds six models (Pioneer, Summit, Ranger, Nova, Terra, Voyager) in six plants: San Antonio `P_TX`, Lexington `P_KY`, Jackson `P_MS`, Princeton `P_IN`, Huntsville `P_AL` and Greensboro `P_NC`. Plants run lean: **there is no parts inventory**. Each plant builds in daily production orders and has a limited number of build slots per day (`plants.csv`), Monday to Friday. Parts arrive in inbound shipments from about 20 suppliers, and a build can only run once the parts it needs have arrived. Finished vehicles go to about 50 dealers, and many production orders are already promised to customers (customer orders).

Today is Monday 2027-03-08. Working-day index 0 is today. The tables cover the past two weeks (already built) and the next four weeks (the planning window).

## Events

An event is a short message (email, plant alert, quality bulletin, chat, news wire). Each one is one of:

| Kind | What it means |
|---|---|
| Supplier delay | An inbound shipment of a part will arrive later than planned. |
| Plant outage | A plant cannot build for a range of days. |
| Quality hold | A lot of a part is defective. |
| Noise | Not about our operations. |
| Unconfirmed rumor | Possibly about us, but not confirmed or specific. |

Some messages combine several incidents. Every event is handled in its own copy of the world, so nothing you do for one event affects another.

## Rules

1. **Supplier delay.** Builds that need the delayed part cannot run from the original ETA up to (but not including) the new ETA. Those production orders are affected. A production order needs a part if its model's bill of materials (`bom.csv`) includes it, and only at the receiving plant.
2. **Plant outage.** No builds at the plant during the outage (inclusive). Every production order of that plant in the outage is affected, and the plant restarts the next working day.
3. **Rescheduling.** Move each affected production order to the earliest feasible slot. At the same plant the new day must be at or after the end of the disruption (the new ETA, or the day after the outage), and a day only has a limited number of slots (`slots_per_day`). If no same-plant slot is free, transfer the order to another plant that builds the model (`plant_models.csv`) on any day with a free slot. Never move orders that are not affected.
4. **Dealer notification.** Every dealer with at least one affected customer order gets exactly one `dealer.notify` listing those customer order ids, and only those.
5. **Loaners.** For affected customer orders, a customer in tier Platinum or Gold gets one loaner from their dealer's pool. Pools are limited (`crm.loaner_availability`). When there are more entitled customers than loaners, the priority is Platinum before Gold, then the earlier promised date, then the lower customer order id. Standard customers get no loaner (the dealer is told, nothing else).
6. **Quality hold.** Hold the lot (`quality.hold_lot`). The affected vehicles are the finished vehicles built at that plant, on the lot's build dates, from production orders of models that use the part. Unsold vehicles (`in_stock`) are stopped from sale at their dealer (`dealer.stop_sale`). For sold vehicles, contact the owner (`crm.notify_customer`).
7. **Noise.** Do nothing operational. Do not call any write tool.
8. **Unconfirmed or unclear.** Do not take any operational action. Use `escalate`.
9. **Completeness.** A real incident should end with at least three different kinds of action (for example reschedule, notify dealers, assign loaners).
10. **Do not touch what is not affected.** Wrong or extra actions are penalized.

## Tools

`GET /tools` lists them. Every call is `POST /act` with `{"event_id", "tool", "args"}`. Failed calls change nothing but are logged and counted. Read tools (`mes.get_schedule`, `crm.loaner_availability`) are free and never penalized.

## Dates

Dates in events are sometimes written relative to today ("next Wednesday", "last Thursday"). Today is Monday 2027-03-08. Tool dates are always `YYYY-MM-DD`, weekdays only, from today through 2027-04-02.

## Names

People write names in many ways ("the Texas plant", "heavy-duty wiring harness", "Volt"). `aliases.csv` lists the known variants. Resolve names to ids before you act.
