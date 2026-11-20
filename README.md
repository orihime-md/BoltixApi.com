# Boltix API

Menu bot that links users' own Telegram bots to Boltix ff. It is a sales and linking front end. The Boltix ff bot runs the linked bots.

## Menu
- **Boltix ff**: open now. Asks for the user's bot token, then shows **Purchase** and **Connect**.
- **BX ban check**, **Spin the Bottle**, **Hoshigami anime**: locked, show "Pending launch".

## Purchase and Connect
- **Connect**: links the bot with **20 free requests** (granted once per bot).
- **Purchase**: plans 20 = ₦1,000, 40 = ₦2,000, 60 = ₦4,000, 100 = ₦8,000. The buyer sees the account to pay, then taps **I've paid**. That sends the owner a payment claim with the token and the command to run.
- The **Message owner** button opens Telegram's share picker with a prefilled message. The buyer picks the owner's chat and sends it.

## Owner steps
1. Receive the payment claim. The owner must start this bot once to receive claims.
2. Confirm the money, then send in the **Boltix ff bot** (your private chat with it): `/20 BOT_TOKEN`. Use `/40`, `/60` or `/100` for other plans.
3. The linked bot starts within about 30 seconds. Its members must join the Boltix channels and can add their own channel.

## Deploy on Render
1. Push these files to the root of a GitHub repo.
2. Render → **New → Blueprint** → pick the repo. This creates `boltix-api-db` and `boltix-api-bot`.
3. Fill in `BOT_TOKEN` and `OWNER_ID`.
4. Open `boltix-api-db` → copy its **Internal Database URL**. Paste it as `DATABASE_URL_MAIN` in the **Boltix ff** service so both bots share one database. Both services must be in the same region.
5. Send `/start` to the bot.

## Notes
- Sessions are kept in memory. A restart asks the user to send their token again. Linked bots are saved in the database and are not affected.
- Bot tokens are stored in the database as plain text. Keep database access restricted.
- Free Render Postgres expires after 30 days unless upgraded. Back up before it expires.
