# Website Security & Performance Audit Bot

Fully automated website audit service. Customers pay €200, bot analyzes their website using Claude AI, generates a professional PDF report, and emails it automatically.

**€1,000/month target = 5 audits/month at €200 each**

## Architecture

```
Customer Payment → Stripe Webhook → Bot Starts
    ↓
Website Crawling (requests) → Claude Analysis → PDF Generation → Email Send
```

## Setup Instructions

### 1. Get API Keys

You need these three API keys:

**Stripe**
- Go to https://dashboard.stripe.com/
- Copy: Publishable Key (pk_...) and Secret Key (sk_...)
- Create a webhook endpoint for this service URL + `/webhook`
- Copy: Webhook Signing Secret (whsec_...)

**Anthropic (Claude)**
- Go to https://console.anthropic.com/
- Create API key (sk-ant-...)

**SendGrid (Email)**
- Go to https://sendgrid.com/
- Create API key (SG...)
- Create sender email address (e.g., audits@example.com)

### 2. Environment Setup

```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Install & Run Locally

```bash
pip install -r requirements.txt
python main.py
# Open http://localhost:5000
```

### 4. Deploy to Render (Free)

1. Create GitHub repository with this code
2. Go to https://render.com/
3. Click "New +" → "Web Service"
4. Connect your GitHub repo
5. Set Environment Variables (paste from .env)
6. Deploy

**Render free tier:** 
- 0.5 CPU, 512 MB RAM
- Auto-sleep after 15 min inactivity
- But wakes up when webhook arrives
- Perfect for 5-10 audits/month

### 5. Configure Stripe Webhook

In Stripe Dashboard:
- Settings → Webhooks
- Add endpoint: `https://[your-render-domain]/webhook`
- Listen to: `payment_intent.succeeded`
- Copy signing secret to .env

## How It Works

### For Customers
1. Enter website URL + email
2. Pay €200 via Stripe
3. Get PDF report in 2-4 hours

### For You
1. Payment triggers webhook
2. Bot automatically:
   - Crawls website
   - Analyzes with Claude
   - Generates PDF
   - Sends email
3. You do nothing

## Workflow File

For tracking, create this in your Obsidian vault:

```
---
title: Website Audit Bot - Revenue Tracking
typ: project
stand: active
tags: [revenue, automation, jarvis]
---

# Website Audit Bot

## Measurements
- Customers ordered: [X]
- Revenue: €[X]
- Avg time per audit: 2.5h
- Bot success rate: [X]%

## Settings
- Price: €200
- Service: Website Security & Performance Audit
- Delivery: Max 24h
- Domain: auditor.xy.app
- Platform: Render (free tier)

## Not in scope
- Multiple audits per customer
- Dashboard/account system
- Mobile app
```

## Testing

### Local Test
```bash
# Terminal 1
python main.py

# Terminal 2 (simulate webhook)
curl -X POST http://localhost:5000/webhook \
  -H "stripe-signature: test" \
  -d '{"type":"payment_intent.succeeded","data":{"object":{"id":"pi_test","metadata":{"website_url":"https://google.com","customer_email":"test@example.com"}}}}'
```

### Production Test
1. Go to your live domain
2. Enter test website + real email
3. Use Stripe test card: 4242 4242 4242 4242
4. Check email for PDF

## Costs

**Monthly (assuming 5 audits/month):**
- Render: €0 (free tier)
- Stripe: €10 (2% + €0.25 per transaction)
- SendGrid: €0 (100 emails/day free)
- Claude API: ~€5 (0.5 analysis per audit)
- **Total: €15/month**

**Revenue: €1,000/month**
**Net: €985/month**

## Troubleshooting

**Bot doesn't run after payment:**
- Check webhook in Stripe Dashboard (should show attempt)
- Check application logs on Render
- Verify ANTHROPIC_API_KEY is set

**Emails not sending:**
- Verify SendGrid API key is correct
- Check email reputation (might be in spam)
- Look for SendGrid bounces in dashboard

**Website crawl fails:**
- Some websites block bots
- Check if website has IP restrictions
- Some need User-Agent header (already set)

**PDF looks bad:**
- ReportLab font limitations
- For production, consider WeasyPrint or wkhtmltopdf
- Current version works for MVP

## Next Steps for Scaling

**After 5 customers:**
1. Add multiple service tiers (€100, €300, €500)
2. Build dashboard for customers to track orders
3. Add retry mechanism for failed crawls
4. Implement rate limiting

**For €10k/month:**
1. Upwork/Fiverr integration (automated order pickup)
2. API for agencies to integrate
3. White-label PDF branding
4. Advanced reporting (screenshots, lighthouse scores)

---

**Status:** Ready to deploy
**Target:** €1,000/month in 30 days
**Dependencies:** Stripe, SendGrid, Anthropic, Render
# Render Configuration Fix - Testing Procfile only
