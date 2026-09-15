# Deployment Guide - Website Audit Bot

**Timeline: 30 minutes to live**

## Step 1: Get API Keys (10 min)

### Stripe Account
1. Go to https://dashboard.stripe.com/
2. Create account (if you don't have one)
3. Copy these from Dashboard:
   - **Publishable Key** (starts with `pk_live_...`)
   - **Secret Key** (starts with `sk_live_...`)

4. Go to Developers → Webhooks
5. Click "Add endpoint"
6. Fill in: 
   - URL: `https://[YOUR-DOMAIN]/webhook` (we'll get this after deploy)
   - Events: Look for `payment_intent.succeeded`
7. After creating, copy the **Signing Secret** (starts with `whsec_...`)

### Anthropic Claude
1. Go to https://console.anthropic.com/
2. Click "API Keys" → "Create Key"
3. Copy the **API Key** (starts with `sk-ant-...`)

### SendGrid Email
1. Go to https://sendgrid.com/
2. Create account
3. Go to Settings → API Keys → Create API Key
4. Copy the key (starts with `SG...`)
5. Go to Sender Authentication → Single Sender
6. Add your email (e.g., audits@yourdomain.com or just your personal email for MVP)
7. Verify the email

## Step 2: Push Code to GitHub (5 min)

```bash
# Go to the website-audit-bot folder
cd /home/claude/website-audit-bot

# Initialize git (if not already done)
git init
git add .
git commit -m "Initial website audit bot"

# Create a new repository on GitHub.com
# Then push:
git remote add origin https://github.com/YOUR-USERNAME/website-audit-bot.git
git branch -M main
git push -u origin main
```

## Step 3: Deploy to Render (10 min)

1. Go to https://render.com/
2. Create account with GitHub
3. Click "New +" → "Web Service"
4. Connect your GitHub account
5. Select `website-audit-bot` repository
6. Fill in:
   - **Name**: website-audit-bot
   - **Region**: Any (Frankfurt is good for EU)
   - **Branch**: main
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn main:app`

7. Scroll down to "Environment"
8. Add these variables:

```
STRIPE_PUBLISHABLE_KEY = pk_live_... (from Stripe)
STRIPE_SECRET_KEY = sk_live_... (from Stripe)
STRIPE_WEBHOOK_SECRET = whsec_... (from Stripe Webhooks)
ANTHROPIC_API_KEY = sk-ant-... (from Claude)
SENDGRID_API_KEY = SG... (from SendGrid)
FROM_EMAIL = audits@yourdomain.com (or your personal email)
DOMAIN = https://website-audit-bot.onrender.com (Render will give you this)
FLASK_ENV = production
```

9. Click "Deploy"
10. Wait 2-3 minutes for deployment
11. You'll see a URL like: `https://website-audit-bot.onrender.com`

## Step 4: Configure Stripe Webhook (5 min)

1. Go back to Stripe Dashboard
2. Developers → Webhooks
3. Find the endpoint you created earlier
4. Edit it and change the URL to: `https://website-audit-bot.onrender.com/webhook`
5. Make sure it's listening to `payment_intent.succeeded`
6. Save

## Step 5: Test (Do This Before Going Live!)

1. Open https://website-audit-bot.onrender.com in browser
2. You should see the audit form
3. Fill in:
   - Website URL: https://google.com (or any public website)
   - Email: your email
4. Click "Get Audit Report"
5. You'll be taken to Stripe payment page
6. Use test card: **4242 4242 4242 4242**
7. Any future date, any CVC
8. Check your email in 2-4 minutes for PDF

**If you DON'T get an email:**
- Check spam folder
- Go to Render dashboard → Logs → check for errors
- Go to Stripe → Developers → Webhooks → click the event → see details

## Step 6: Go Live with Real Stripe Keys

Once testing works:

1. Switch Stripe to LIVE mode (not test)
2. Copy LIVE keys (not test keys) into Render environment
3. Redeploy on Render
4. Test one more time with real payment

## Monitoring

Every time someone orders:

**Option A: Passive** (No action needed)
- Money goes to Stripe account
- Bot works automatically
- Customer gets email

**Optional: Monitor**
1. Stripe Dashboard: Shows each payment
2. Render Logs: Shows bot activity
3. Email: Forward audit emails to yourself to verify they're working

## If Bot Fails

**Check Render Logs:**
1. Go to Render Dashboard
2. Click website-audit-bot
3. Logs tab
4. Search for error

**Common issues:**
- `ANTHROPIC_API_KEY missing`: Add to environment
- `SENDGRID_API_KEY missing`: Add to environment
- `Timeout accessing website`: Website was slow or down, that's OK
- `Email send failed`: SendGrid key is wrong, verify

## Success Criteria

✅ Website loads at https://website-audit-bot.onrender.com
✅ Form accepts URL + email
✅ Stripe checkout works (test payment)
✅ Webhook receives payment notification (check Stripe logs)
✅ Bot starts (check Render logs)
✅ PDF email arrives within 4 hours

If all 6 are ✅, you're live. Go get customers.

## Costs

- **Render**: Free (auto-sleeps, but wakes for webhooks)
- **Stripe**: 2.9% + €0.25 per transaction (~€5-10 per 5 audits)
- **SendGrid**: Free (100 emails/day)
- **Anthropic**: ~€1-2 per audit
- **Total**: ~€7 per €200 audit = 96.5% margin

## What Happens When Customer Buys

1. Customer enters website + email
2. Clicks "Get Audit"
3. Pays €200 on Stripe
4. Stripe sends webhook to your bot
5. Bot starts automatically (you do NOTHING)
6. Bot crawls website, analyzes with Claude, generates PDF
7. Bot sends PDF email to customer
8. Done. Money is in your Stripe account.

**Total your effort per €200 order: 0 minutes**

---

**Need help?** Check Render logs first. 99% of issues are missing API keys.
